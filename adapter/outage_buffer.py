#!/usr/bin/env python3
"""Durable outage buffering supervisor -- see OUTAGE.md for the design and the evidence.

While AWS is reachable, MediaMTX records a rolling ~2 minute window to the USB stick for
every camera whose producer is running and whose registry row asks for it. When AWS
becomes unreachable, this process stops deleting and starts moving completed segments
aside, so nothing is lost. On recovery it hands the captured tree to the uploader.

Three design choices that are not obvious, all argued in OUTAGE.md:

1. **No MediaMTX API call happens at T0.** Patching any record field makes MediaMTX tear
   down and rebuild the recorder, and the next fMP4 segment can only start at a keyframe
   -- up to ~2 s of video, placed exactly at the moment the outage begins. Arming is done
   once, with deletion disabled; the outage transition is purely local bookkeeping.

2. **This process owns retention, not MediaMTX's cleaner.** `recordDeleteAfter` is `0s`
   forever. Handing retention to the cleaner would mean one raced tick could delete the
   captured outage -- the footage the feature exists to save.

3. **Reconcile every tick; never trust what we last sent.** MediaMTX does not persist API
   config changes to mediamtx.yml, so a restart silently reverts every armed path. Only
   comparing against live config catches that.
"""
import json
import os
import shutil
import socket
import threading
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aws_state
import camera_control
import mediamtx_api

# --- tunables -------------------------------------------------------------------------
TICK_SEC = 5
REGISTRY_POLL_SEC = 60           # same cadence as event_watcher.py
PREROLL_SEC = 120                # must exceed worst-case detection latency; see OUTAGE.md 2.1
SEGMENT_DURATION = "30s"
MIN_OUTAGE_SEC = 120             # below this KVS loses nothing; a clip would duplicate it
PRODUCER_DISARM_GRACE_SEC = 60   # hysteresis, so a crash-looping producer doesn't disarm us
PROBE_HOST = "a3dp4umq4qv6ul-ats.iot.eu-central-1.amazonaws.com"
PROBE_PORT = 443
PROBE_TIMEOUT = 2          # per address
PROBE_BUDGET_SEC = 4       # for the whole probe, however many addresses DNS returns
PROBE_FAILS_FOR_OUTAGE = 2
# Recovery needs MORE evidence than failure, not less. Declaring recovery on a single
# successful probe made the supervisor flap: measured 3 finalise/reopen cycles in one
# outage, because the IoT endpoint's DNS rotates across AWS ranges and one rotation
# briefly landed on a reachable address. Each flap finalises a capture and opens another,
# fragmenting one outage into several clips. Failure is cheap to act on (start recording);
# recovery is expensive to get wrong (stop recording), so the thresholds are asymmetric.
PROBE_OKS_FOR_RECOVERY = 3

BUFFER_ROOT = Path("/mnt/vms-buffer")
SENTINEL = BUFFER_ROOT / ".vms-buffer-ok"
LIVE_DIR = BUFFER_ROOT / "live"
OUTAGE_DIR = BUFFER_ROOT / "outage"
RECORD_PATH = str(LIVE_DIR / "%path" / "%Y-%m-%d_%H-%M-%S-%f")

STATE_DIR = Path.home() / ".local" / "state" / "vms"
REGISTRY_CACHE = STATE_DIR / "cameras-cache.json"

DISK_FLOOR_BYTES = 4 * 1024 ** 3
DISK_FLOOR_FRACTION = 0.15

VALID_LIMITS = {30, 120, 300, 600, 1800, 3600, 18000, 43200, 86400}


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def utc_now():
    return datetime.now(timezone.utc)


# --- storage --------------------------------------------------------------------------

def buffer_ready() -> tuple[bool, str]:
    """Is the USB stick actually mounted and writable?

    `ismount` AND a sentinel file. The sentinel is not belt-and-braces: if the stick is
    unplugged but /mnt/vms-buffer still exists as a plain directory, MediaMTX writes onto
    the SD card -- and with 25 GB free a long outage *fits*, which is worse than failing,
    because it puts exactly the write load the stick exists to absorb onto the card.
    """
    if not os.path.ismount(BUFFER_ROOT):
        return False, "not mounted"
    if not SENTINEL.exists():
        return False, "sentinel missing (wrong filesystem mounted?)"
    if not os.access(BUFFER_ROOT, os.W_OK):
        return False, "not writable"
    return True, ""


def disk_ok() -> tuple[bool, str]:
    st = os.statvfs(BUFFER_ROOT)
    free = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    floor = max(DISK_FLOOR_BYTES, int(total * DISK_FLOOR_FRACTION))
    if free < floor:
        return False, f"free {free/1024**3:.1f} GB below floor {floor/1024**3:.1f} GB"
    return True, ""


# --- registry -------------------------------------------------------------------------

def read_registry_cache() -> dict:
    try:
        return json.loads(REGISTRY_CACHE.read_text())
    except Exception:
        return {}


def _fetch_registry() -> dict:
    """One DynamoDB scan, with timeouts short enough to fail fast.

    boto3's defaults are 60s connect / 60s read with retries. That is catastrophic here:
    an unreachable AWS is exactly the condition this process exists to handle, and the
    first version blocked inside this call for 45 minutes -- never reaching the
    connectivity check, never detecting the outage it was watching for. Measured, not
    theorised.
    """
    from botocore.config import Config
    from aws_device_creds import get_session
    cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    ddb = get_session("eu-central-1").resource("dynamodb", config=cfg)
    items = ddb.Table("cameras").scan()["Items"]
    return {i["cameraId"]: int(i.get("outageBufferSec", 0) or 0) for i in items}


def registry_refresher(shared: dict, online_flag: dict):
    """Refresh the registry off the tick path, forever.

    Even with short timeouts, a network call has no business on the loop that has to
    notice an outage within seconds. The tick loop only ever reads `shared`; this thread
    is the only thing that touches AWS. While offline it does not even try -- the cached
    value is by definition the right one, since nobody could have changed the registry
    from a device that cannot reach it.
    """
    while True:
        try:
            if online_flag.get("online", True):
                cams = _fetch_registry()
                if cams != shared.get("cameras"):
                    log(f"registry: {cams}")
                shared["cameras"] = cams
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                REGISTRY_CACHE.write_text(json.dumps(cams))
        except Exception as e:
            # Same posture as event_watcher.py:206 -- keep the current set, say so once.
            if not shared.get("warned"):
                log(f"registry read failed ({type(e).__name__}), using cache "
                    f"({len(shared.get('cameras', {}))} cameras)")
                shared["warned"] = True
        else:
            shared["warned"] = False
        time.sleep(REGISTRY_POLL_SEC)


# --- connectivity ---------------------------------------------------------------------

def probe_aws() -> bool:
    """Independent TCP reachability check, outbound-only like everything else here.

    Not `socket.create_connection`: that applies its timeout **per resolved address**, and
    this host has both an A and an AAAA record, so a 4 s timeout cost 8 s per probe --
    measured. Two probes are needed to declare an outage, so every tick inflated and
    detection took 86 s against a 120 s pre-roll, leaving far less margin than the design
    assumed. The budget below caps the whole probe regardless of how many addresses DNS
    returns, which keeps detection latency a property of the config rather than of DNS.
    """
    try:
        infos = socket.getaddrinfo(PROBE_HOST, PROBE_PORT, type=socket.SOCK_STREAM)
    except Exception:
        return False                      # cannot even resolve -- treat as unreachable
    deadline = time.monotonic() + PROBE_BUDGET_SEC
    for fam, stype, proto, _canon, addr in infos:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        s = socket.socket(fam, stype, proto)
        s.settimeout(min(PROBE_TIMEOUT, remaining))
        try:
            s.connect(addr)
            return True
        except Exception:
            continue
        finally:
            s.close()
    return False


class Connectivity:
    """Combines agent.py's MQTT callbacks with an independent probe.

    MQTT alone is wrong in both directions: it fires on duplicate-client-id takeover or a
    keepalive miss under load, and stays silent when kvssink fails on expired credentials
    or a crash-looping producer. And if agent.py dies its heartbeat goes stale -- which
    must read as *unknown*, never as an outage, or a crashed agent would silently record
    forever.
    """

    def __init__(self):
        self.fail_streak = 0
        self.ok_streak = 0
        self.online = True

    def poll(self) -> tuple[bool, str]:
        st = aws_state.read_state()
        probe = probe_aws()
        if probe:
            self.fail_streak = 0
            self.ok_streak += 1
        else:
            self.ok_streak = 0
            self.fail_streak += 1

        agent_says = st["online"]          # True / False / None(stale)
        probe_out = self.fail_streak >= PROBE_FAILS_FOR_OUTAGE

        if self.online:
            # Go offline on either signal -- whichever notices first. Acting early is
            # cheap: the worst case is recording footage that turned out not to be needed.
            if agent_says is False or probe_out:
                self.online = False
                why = "mqtt interrupted" if agent_says is False else f"probe failed x{self.fail_streak}"
                if agent_says is None and probe_out:
                    why = f"probe failed x{self.fail_streak} (agent heartbeat stale)"
                return False, why
        else:
            # Both signals must agree, AND reachability must have held for several
            # consecutive probes. One lucky probe is not a recovery -- see
            # PROBE_OKS_FOR_RECOVERY for the flapping this prevents.
            if self.ok_streak >= PROBE_OKS_FOR_RECOVERY and agent_says is not False:
                self.online = True
                return True, (f"probe ok x{self.ok_streak}"
                              + ("" if agent_says else " (agent heartbeat stale)"))
        return self.online, ""


# --- producer state -------------------------------------------------------------------

class ProducerLatch:
    """Producer-active tracking with hysteresis.

    `systemctl is-active` collapses everything to one word; a producer that is
    crash-looping reads `activating`/`failed` and a naive `== "active"` test would disarm
    buffering at exactly the moment it matters. Treat any non-inactive state as running,
    and require a sustained absence before disarming.
    """

    def __init__(self):
        self.last_active = {}

    def active(self, camera_id: str) -> bool:
        unit = camera_control.unit_name(camera_id)
        state = _systemd_active_state(unit)
        running = state in ("active", "activating", "reloading", "deactivating")
        now = time.monotonic()
        if running:
            self.last_active[camera_id] = now
            return True
        seen = self.last_active.get(camera_id)
        if seen is None:
            return False
        return (now - seen) < PRODUCER_DISARM_GRACE_SEC


def _systemd_active_state(unit: str) -> str:
    import subprocess
    r = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", unit],
                       capture_output=True, text=True)
    return r.stdout.strip()


# --- segments -------------------------------------------------------------------------

def completed_segments(d: Path) -> list[Path]:
    """Segments safe to touch.

    MediaMTX keeps exactly one open segment per path and writes into the final filename,
    patching the moov duration on close -- there is no .part suffix to look for. So a
    segment is complete iff a later-starting one exists beside it. Simpler than the
    runOnRecordSegmentComplete hook, and it survives a missed invocation; the hook is also
    not live-patchable, so setting it would recreate the path (OUTAGE.md 3.2).
    """
    if not d.is_dir():
        return []
    segs = sorted(d.glob("*.mp4"))
    return segs[:-1] if segs else []


def segment_start(p: Path) -> datetime | None:
    """Start time from the filename recordPath's %Y-%m-%d_%H-%M-%S-%f produced.

    **Local time, not UTC.** MediaMTX formats those placeholders in the machine's local
    zone -- its own docs offer a separate %z for the offset. Tagging the parsed value as
    UTC put every segment two hours in the future on a CEST box, so the retention cutoff
    never matched and the rolling window grew without bound: measured 9 segments where 4-5
    were expected, i.e. the stick would have filled silently.

    `astimezone()` on a naive datetime interprets it as local and attaches the offset,
    which is exactly the intended reading.
    """
    try:
        return datetime.strptime(p.stem, "%Y-%m-%d_%H-%M-%S-%f").astimezone()
    except ValueError:
        # Not one of ours (or a renamed/partial file) -- fall back to mtime rather than
        # returning None, so an unparseable name can never become un-prunable.
        try:
            return datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        except OSError:
            return None


# --- the outage capture ---------------------------------------------------------------

class Capture:
    """One outage, on disk, resumable across a reboot."""

    def __init__(self, outage_id: str, limit_by_cam: dict):
        self.id = outage_id
        self.dir = OUTAGE_DIR / outage_id
        self.limit_by_cam = limit_by_cam
        self.started_wall = utc_now()
        self.started_mono = time.monotonic()
        self.frozen = set()            # cameras whose limit or guard has been reached
        self.dir.mkdir(parents=True, exist_ok=True)
        self.write_journal("capturing")

    def elapsed(self) -> float:
        return time.monotonic() - self.started_mono

    def write_journal(self, status: str, extra: dict = None):
        # (wall, monotonic) both recorded: a Pi 4 has no RTC, so after a power-cut outage
        # the wall clock may be wrong with no NTP to fix it, and segment filenames feed
        # startTs -- the clips table sort key.
        payload = {
            "outageId": self.id,
            "status": status,
            "startedWall": self.started_wall.isoformat(),
            "startedMono": self.started_mono,
            "elapsedSec": round(self.elapsed(), 1),
            "limits": self.limit_by_cam,
            "frozen": sorted(self.frozen),
            "updated": utc_now().isoformat(),
        }
        if extra:
            payload.update(extra)
        tmp = self.dir / ".state.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.dir / "state.json")

    def sweep(self, cameras: list[str]):
        """Move completed segments out of live/ and into the capture."""
        for cam in cameras:
            path = camera_control.mediamtx_path_name(cam)
            if cam in self.frozen:
                continue
            limit = self.limit_by_cam.get(cam, 0)
            if limit and self.elapsed() > limit:
                self.freeze(cam, "limit reached")
                continue
            dest = self.dir / path
            dest.mkdir(parents=True, exist_ok=True)
            for seg in completed_segments(LIVE_DIR / path):
                try:
                    # Same filesystem, so this is atomic and instant -- and it takes the
                    # footage out of any directory a cleaner would scan.
                    seg.rename(dest / seg.name)
                except OSError as e:
                    log(f"  move failed {seg.name}: {e}")

    def freeze(self, cam: str, why: str):
        if cam in self.frozen:
            return
        self.frozen.add(cam)
        log(f"  {cam}: capture frozen ({why}) -- rolling window resumes")
        self.write_journal("capturing")

    def collect_tail(self):
        """At recovery, take whatever the rolling window still holds.

        For a camera that ran to its limit, the capture stops at `T0 + limit` while the
        outage kept going; the rolling window meanwhile holds the last ~2 minutes before
        recovery. That tail is worth keeping -- kvssink's rollback is capped at
        `replayDuration` (40 s, gstkvssink.cpp:100), so ~80 s of it would otherwise reach
        nobody. It is a separate group, not a continuation: for a long outage there is a
        real gap between head and tail, and merging them into one clip would imply
        continuous footage that does not exist.
        """
        for cam in self.limit_by_cam:
            path = camera_control.mediamtx_path_name(cam)
            segs = completed_segments(LIVE_DIR / path)
            if not segs:
                continue
            dest = self.dir / "tail" / path
            dest.mkdir(parents=True, exist_ok=True)
            moved = 0
            for seg in segs:
                # Skip anything already in the head -- for a short outage that never hit
                # its limit, sweep() has these already and they must not be duplicated.
                if (self.dir / path / seg.name).exists():
                    continue
                try:
                    seg.rename(dest / seg.name)
                    moved += 1
                except OSError as e:
                    log(f"  tail move failed {seg.name}: {e}")
            if moved:
                log(f"  {cam}: kept {moved} tail segment(s) from before recovery")
            else:
                dest.rmdir() if not any(dest.iterdir()) else None

    def finalize(self, status: str):
        self.write_journal(status, {"endedWall": utc_now().isoformat()})


def prune_live(path: str, keep_sec: int):
    """Rolling retention, owned here rather than by MediaMTX's cleaner."""
    d = LIVE_DIR / path
    cutoff = utc_now().timestamp() - keep_sec
    for seg in completed_segments(d):
        st = segment_start(seg)
        if st and st.timestamp() < cutoff:
            seg.unlink(missing_ok=True)


# --- main loop ------------------------------------------------------------------------

def main():
    log("outage buffer supervisor starting")
    log(f"  buffer={BUFFER_ROOT} preroll={PREROLL_SEC}s segment={SEGMENT_DURATION} "
        f"min-outage={MIN_OUTAGE_SEC}s")

    conn = Connectivity()
    latch = ProducerLatch()
    armed: set[str] = set()
    capture: Capture | None = None

    # The tick loop must never make a network call: see _fetch_registry(). A thread owns
    # all AWS access; this loop only reads what it publishes.
    shared = {"cameras": read_registry_cache()}
    online_flag = {"online": True}
    threading.Thread(target=registry_refresher, args=(shared, online_flag), daemon=True).start()
    if shared["cameras"]:
        log(f"registry (from cache): {shared['cameras']}")

    OUTAGE_DIR.mkdir(parents=True, exist_ok=True)
    for orphan in sorted(OUTAGE_DIR.glob("*/state.json")):
        try:
            j = json.loads(orphan.read_text())
            if j.get("status") not in ("uploaded", "done"):
                log(f"orphan capture from a previous run: {j['outageId']} ({j.get('status')})")
        except Exception:
            pass

    while True:
        try:
            registry = shared.get("cameras", {})

            ok, why = buffer_ready()
            space_ok, space_why = (disk_ok() if ok else (False, "buffer unavailable"))

            online, reason = conn.poll()
            online_flag["online"] = online

            # --- arming (reconciled, not remembered) ---------------------------------
            want = {
                cam for cam, limit in registry.items()
                if limit > 0 and latch.active(cam) and ok and space_ok
            }
            for cam in sorted(set(registry) | armed):
                path = camera_control.mediamtx_path_name(cam)
                should = cam in want
                try:
                    if not mediamtx_api.recording_conf_matches(
                            path, should, RECORD_PATH, SEGMENT_DURATION):
                        mediamtx_api.ensure_path_conf(path, {})
                        mediamtx_api.set_recording(path, should, RECORD_PATH, SEGMENT_DURATION)
                        log(f"{cam}: recording {'ARMED' if should else 'disarmed'}"
                            + ("" if should else f" ({why or space_why or 'not wanted'})"))
                    if should:
                        armed.add(cam)
                    else:
                        armed.discard(cam)
                except mediamtx_api.MediaMTXError as e:
                    log(f"{cam}: mediamtx error: {e}")

            # --- outage lifecycle ----------------------------------------------------
            if not online and capture is None and armed:
                outage_id = utc_now().strftime("%Y%m%dT%H%M%SZ")
                limits = {c: registry.get(c, 0) for c in armed}
                capture = Capture(outage_id, limits)
                log(f"OUTAGE detected ({reason}) -- capture {outage_id} for {sorted(armed)}")
                # The preroll is whatever live/ already holds; sweeping picks it up.

            if capture is not None:
                if not space_ok:
                    for cam in list(capture.limit_by_cam):
                        capture.freeze(cam, space_why)
                capture.sweep(list(capture.limit_by_cam))
                capture.write_journal("capturing")

                if online:
                    dur = capture.elapsed()
                    capture.collect_tail()
                    if dur < MIN_OUTAGE_SEC:
                        log(f"RECOVERED after {dur:.0f}s -- below {MIN_OUTAGE_SEC}s minimum, "
                            f"discarding capture (KVS loses nothing this short)")
                        shutil.rmtree(capture.dir, ignore_errors=True)
                    else:
                        capture.finalize("pending-upload")
                        log(f"RECOVERED after {dur:.0f}s -- capture {capture.id} "
                            f"ready for upload at {capture.dir}")
                    capture = None

            # --- rolling retention while online --------------------------------------
            # A frozen camera (limit reached) goes back to the rolling window rather than
            # stopping: it costs nothing and keeps the ~2 min before recovery, which is
            # ~80 s more than kvssink's 40 s replay would deliver (OUTAGE.md 6.2).
            if ok:
                for cam in armed:
                    if capture is None or cam in capture.frozen:
                        prune_live(camera_control.mediamtx_path_name(cam), PREROLL_SEC)

            time.sleep(TICK_SEC)
        except KeyboardInterrupt:
            log("stopping")
            return 0
        except Exception as e:
            log(f"tick failed ({type(e).__name__}: {e}) -- continuing")
            time.sleep(TICK_SEC)


if __name__ == "__main__":
    sys.exit(main())
