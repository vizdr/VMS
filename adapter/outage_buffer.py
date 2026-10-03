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
   for the ring on the stick, forever. Handing retention to the cleaner would mean one raced
   tick could delete the captured outage -- the footage the feature exists to save. The PIR
   ring in RAM gets the cleaner only as a backstop at 10 min (RAM_BACKSTOP), five times this
   process's own retention, so that a dead supervisor can't fill the runtime tmpfs.

3. **Reconcile every tick; never trust what we last sent.** MediaMTX does not persist API
   config changes to mediamtx.yml, so a restart silently reverts every armed path. Only
   comparing against live config catches that.

It is also the single owner of MediaMTX recording for the PIR trigger (PIR-MQTT-VMS-PI4.md
§3.6): a camera whose registry row has pirRecording on is armed regardless of its producer,
and the footage of each PIR session the watcher journals under pir/<sessionId>/ is linked or
copied out of the ring here. One owner, because two processes setting `record` on the same
path would fight, and every flip rebuilds the recorder and leaves a keyframe seam.

**Where the ring lives (Phase 12, D7).** A camera armed for PIR records into RAM
($XDG_RUNTIME_DIR/vms/ring/), because PIR recording runs around the clock and a ring on the
stick would write ~11 GB a day to it; the stick then receives only the footage that is kept.
A camera armed only for the outage keeps its ring on the stick, unchanged: its producer gate
already bounds those writes. Everything that reads the ring looks in both places, and moving
a segment out of RAM is a copy (move_segment), so the outage path still needs no MediaMTX call
at T0. A PIR session's footage is staged in RAM too, as hard links into the ring
($XDG_RUNTIME_DIR/vms/pir/<sessionId>/): the watcher merges it from there, and the stick
receives only the finished clip. Copying whole 30 s segments to the stick and then writing the
trimmed clip again measured 3.3x the kept footage (PIR-MQTT-VMS-PI4.md Phase 12).
"""
import errno
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

import config

import aws_state
import camera_control
import mediamtx_api
from pir_session import MAX_SEC as PIR_MAX_SEC, PRE_ROLL_SEC as PIR_PRE_ROLL_SEC

# --- tunables -------------------------------------------------------------------------
TICK_SEC = 5
REGISTRY_POLL_SEC = 60           # same cadence as event_watcher.py
PREROLL_SEC = 120                # must exceed worst-case detection latency; see OUTAGE.md 2.1
SEGMENT_DURATION = "30s"
MIN_OUTAGE_SEC = 120             # below this KVS loses nothing; a clip would duplicate it
PRODUCER_DISARM_GRACE_SEC = 60   # hysteresis, so a crash-looping producer doesn't disarm us
PROBE_HOST = config.IOT_DATA_ENDPOINT
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
PIR_DIR = BUFFER_ROOT / "pir"         # one directory per PIR session (PIR-MQTT-VMS-PI4.md §3.6)
RECORD_PATH = str(LIVE_DIR / "%path" / "%Y-%m-%d_%H-%M-%S-%f")
# The PIR ring, in RAM (Phase 12). ~24 MB per camera: 120 s plus one open 30 s segment.
RAM_RING_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "vms" / "ring"
RAM_RECORD_PATH = str(RAM_RING_DIR / "%path" / "%Y-%m-%d_%H-%M-%S-%f")
RING_DIRS = (LIVE_DIR, RAM_RING_DIR)
# MediaMTX's cleaner for the RAM ring only: if this process dies, the ring would otherwise
# grow ~9.5 MB a minute until the 374 MB runtime tmpfs -- shared with every heartbeat file and
# systemd's own runtime state -- is full (~40 min). 10 min caps it near 95 MB per camera.
RAM_BACKSTOP = "10m"
RAM_FREE_MIN_BYTES = 64 * 1024 ** 2   # below this, the PIR ring falls back to the stick
# PIR session footage staged in RAM: hard links into the ring, so staging costs no copy and no
# stick write. A session lives here from capture until the watcher merges it -- seconds after
# it closes. If it is still unmerged STAGE_MAX_SEC after capture (the watcher stalled), it is
# moved to the stick, which keeps RAM bounded. A reboot before the merge loses it.
RAM_PIR_DIR = RAM_RING_DIR.parent / "pir"
STAGE_MAX_SEC = 600
# A session journal still open this long after its start outlived its own cap -- the
# watcher died mid-session. Capture up to the cap, then stop linking.
PIR_OPEN_LIMIT_SEC = PIR_PRE_ROLL_SEC + PIR_MAX_SEC + 60

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

def _normalise(cams: dict) -> dict:
    """{cameraId: {"outageBufferSec": int, "pirRecording": bool, "pirTopic": str|None}}.

    pirTopic is carried for kvs-pir-watcher, which reads this cache instead of calling AWS,
    so local PIR recording keeps working with the internet down.

    Caches written before the PIR switch existed hold {cameraId: outageBufferSec}; a
    supervisor restarting with AWS unreachable must still read those.
    """
    out = {}
    for cam, row in cams.items():
        if not isinstance(row, dict):
            row = {"outageBufferSec": row}
        out[cam] = {"outageBufferSec": int(row.get("outageBufferSec") or 0),
                    "pirRecording": bool(row.get("pirRecording")),
                    "pirTopic": row.get("pirTopic") or None}
    return out


def read_registry_cache() -> dict:
    try:
        return _normalise(json.loads(REGISTRY_CACHE.read_text()))
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
    ddb = get_session(config.AWS_REGION).resource("dynamodb", config=cfg)
    items = ddb.Table("cameras").scan()["Items"]
    return _normalise({i["cameraId"]: i for i in items})


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


def ring_segments(path: str) -> list[Path]:
    """Every file of this path's ring, in both places a ring can live, oldest first.

    The names are start times, so sorting by name is chronological across both directories,
    and the newest is the one MediaMTX is writing. Both places, because a camera's ring moves
    when it is re-armed for a different reason, and segments left in the old place still
    count: a PIR session or an outage may need them.
    """
    found = []
    for root in RING_DIRS:
        d = root / path
        if d.is_dir():
            found.extend(d.glob("*.mp4"))
    return sorted(found, key=lambda p: p.name)


def completed_ring_segments(path: str) -> list[Path]:
    """All but the newest: the same rule as completed_segments(), across both ring places."""
    return ring_segments(path)[:-1]


def _copy_durable(src: Path, dst: Path) -> None:
    """Copy under a temporary name, fsync, then rename. A crash mid-copy leaves `.name.part`,
    which no `*.mp4` glob picks up -- never a truncated segment that looks complete."""
    tmp = dst.with_name(f".{dst.name}.part")
    shutil.copy2(src, tmp)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dst)


def move_segment(src: Path, dst: Path) -> None:
    """Out of the ring. On the same filesystem a rename: atomic and instant. From the RAM ring
    to the stick a durable copy, then the RAM copy is deleted -- until then the source still
    counts as ring footage, so nothing is ever in neither place."""
    try:
        src.rename(dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy_durable(src, dst)
        src.unlink(missing_ok=True)


def _dev(p: Path):
    try:
        return p.stat().st_dev
    except OSError:
        return None


def ram_ok() -> tuple[bool, str]:
    try:
        RAM_RING_DIR.mkdir(parents=True, exist_ok=True)
        st = os.statvfs(RAM_RING_DIR)
    except OSError as e:
        return False, f"RAM ring unavailable ({e})"
    free = st.f_bavail * st.f_frsize
    if free < RAM_FREE_MIN_BYTES:
        return False, f"only {free / 1024 ** 2:.0f} MB free in {RAM_RING_DIR.parent}"
    return True, ""


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
        """Move completed segments out of the ring (stick or RAM) and into the capture."""
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
            for seg in completed_ring_segments(path):
                try:
                    # From the stick ring a rename, atomic and instant; from the RAM ring a
                    # durable copy. Either way the footage leaves any directory a cleaner
                    # would scan.
                    move_segment(seg, dest / seg.name)
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
            segs = completed_ring_segments(path)
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
                    move_segment(seg, dest / seg.name)
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
    """Rolling retention, owned here rather than by MediaMTX's cleaner -- in both ring places."""
    cutoff = utc_now().timestamp() - keep_sec
    for seg in completed_ring_segments(path):
        st = segment_start(seg)
        if st and st.timestamp() < cutoff:
            seg.unlink(missing_ok=True)


def drop_disarmed_ram_ring(path: str, keep_sec: int):
    """A disarmed camera's RAM ring, including its last segment, which prune_live() never
    treats as complete: nothing records into it once `record` is off, and with no cleaner on a
    disarmed path it would otherwise hold RAM until the next arming or reboot. A session that
    needed it keeps its own staged links."""
    d = RAM_RING_DIR / path
    cutoff = time.time() - keep_sec
    for seg in d.glob("*.mp4") if d.is_dir() else []:
        try:
            if seg.stat().st_mtime < cutoff:
                seg.unlink()
        except FileNotFoundError:
            pass


# --- PIR sessions (PIR-MQTT-VMS-PI4.md §3.6) ------------------------------------------------
#
# The watcher writes pir/<sessionId>/state.json:
#     {"sessionId", "cameraId", "from": <epoch s, t0 - pre-roll>, "to": <epoch s or null>, ...}
# `to` stays null while the session records. This supervisor -- the only writer of the ring --
# hard-links every completed segment overlapping [from, to] into the session: a segment in the
# RAM ring into RAM_PIR_DIR/<sessionId>/<path>/ (staged, Phase 12), one on the stick into
# pir/<sessionId>/<path>/. It reports progress in pir/<sessionId>/sweep.json. One writer per
# file: the watcher never writes sweep.json, this process never writes state.json.

def _segments_with_ends(path: str) -> list:
    """Every completed segment of this path still kept anywhere, as (start, end, name, file).

    Looks in the ring (stick and RAM) AND in outage captures: during an outage, completed
    segments leave the ring within one tick, so a PIR session's pre-roll may already sit in
    outage/. Captures are listed first, so a segment that has both a ring and a stick copy is
    linked from the stick, not copied a second time. A completed segment ends where the next
    one starts, the same rule as completed_segments(); the newest ring file is still being
    written and is left out.
    """
    found = {}
    ring = ring_segments(path)
    dirs = [*OUTAGE_DIR.glob(f"*/{path}"), *OUTAGE_DIR.glob(f"*/tail/{path}"), *(r / path for r in RING_DIRS)]
    for d in dirs:
        if d.is_dir():
            for seg in d.glob("*.mp4"):
                found.setdefault(seg.name, seg)
    timed = sorted((segment_start(f).timestamp(), name, f)
                   for name, f in found.items() if segment_start(f))
    out = []
    for (start, name, f), (nxt, _, _) in zip(timed, timed[1:]):
        out.append((start, nxt, name, f))
    # The last one has no successor yet: complete only if it isn't the file being written.
    return [s for s in out if not (ring and s[2] == ring[-1].name)]


def _link_or_copy(src: Path, dst: Path) -> bool:
    """Hard link on the same filesystem; a durable copy from the RAM ring (Phase 12, D7).
    A link, not a move: one segment can serve two adjacent sessions and an outage capture."""
    try:
        os.link(src, dst)
    except FileExistsError:
        return False
    except FileNotFoundError:
        return False                      # pruned or moved between listing and linking
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        try:
            _copy_durable(src, dst)
        except FileNotFoundError:
            return False                  # pruned or moved while being copied
    return True


def sweep_pir_sessions(state: dict):
    """One pass over the open PIR sessions. Cheap enough for every tick: finished sessions
    are remembered in state["done"], and sweep.json is only rewritten when it changes."""
    if not PIR_DIR.is_dir():
        return
    now = utc_now().timestamp()
    for journal in sorted(PIR_DIR.glob("*/state.json")):
        sdir = journal.parent
        sid = sdir.name
        if sid in state["done"]:
            continue
        try:
            j = json.loads(journal.read_text())
            # Finished before this process started (a restart): the watcher may already
            # have merged it and removed the segment folder, which linking would recreate.
            if j.get("status") in ("merged", "failed") or (
                    (sdir / "sweep.json").exists()
                    and json.loads((sdir / "sweep.json").read_text()).get("complete")):
                state["done"].add(sid)
                continue
            frm, to, cam = float(j["from"]), j.get("to"), j["cameraId"]
        except Exception as e:
            if sid not in state["warned"]:
                log(f"pir session {sid}: unreadable journal ({type(e).__name__}) -- skipped")
                state["warned"].add(sid)
            continue
        capped = False
        if to is None and now - frm > PIR_OPEN_LIMIT_SEC:
            to, capped = frm + PIR_PRE_ROLL_SEC + PIR_MAX_SEC, True
        path = camera_control.mediamtx_path_name(cam)
        dest = sdir / path                      # on the stick: footage that is already there
        stage = RAM_PIR_DIR / sid / path        # in RAM: footage still in the RAM ring
        dest.mkdir(exist_ok=True)
        ram_dev = _dev(RAM_RING_DIR)

        linked_now = 0
        covered = []
        for start, end, name, f in _segments_with_ends(path):
            if end <= frm or (to is not None and start >= float(to)):
                continue
            covered.append((start, end))
            if (dest / name).exists() or (stage / name).exists():
                continue
            if ram_dev is not None and _dev(f) == ram_dev:
                stage.mkdir(parents=True, exist_ok=True)
                target = stage / name           # a link inside the tmpfs: nothing is copied
            else:
                target = dest / name
            if _link_or_copy(f, target):
                linked_now += 1
        staged = sorted(p.name for p in stage.glob("*.mp4")) if stage.is_dir() else []
        have = sorted({p.name for p in dest.glob("*.mp4")} | set(staged))
        through = max((e for _, e in covered), default=state["through"].get(sid))
        complete = to is not None and through is not None and through >= float(to)
        sweep = {
            "sessionId": sid,
            "through": through,
            "coverageStart": min((s for s, _ in covered), default=None),
            "segments": have,
            "stagedInRam": staged,
            "complete": complete,
            "cappedBySupervisor": capped,
        }
        if sweep != state["last"].get(sid):
            tmp = sdir / ".sweep.json.tmp"
            tmp.write_text(json.dumps({**sweep, "updated": utc_now().isoformat()}, indent=2))
            os.replace(tmp, sdir / "sweep.json")
            state["last"][sid] = sweep
            if linked_now or complete:
                log(f"pir session {sid}: {len(have)} segment(s) captured"
                    + (", complete" if complete else "") + (" (capped: journal never closed)" if capped else ""))
        state["through"][sid] = through
        if complete:
            state["done"].add(sid)


def tend_pir_staging():
    """Keep the RAM staging bounded and never let it outlive its session.

    - the session was merged, failed or deleted: its staging goes (the watcher removes it after
      a merge; this catches a crash in between);
    - captured, but still unmerged STAGE_MAX_SEC later (the watcher is down or stuck): the
      footage moves to the stick session folder, where the watcher's merge also looks.
    """
    if not RAM_PIR_DIR.is_dir():
        return
    now = time.time()
    for staged in RAM_PIR_DIR.iterdir():
        sdir = PIR_DIR / staged.name
        try:
            j = json.loads((sdir / "state.json").read_text())
        except FileNotFoundError:
            j = None
        except Exception:
            continue                            # half-written; look again next tick
        if j is None or j.get("status") in ("merged", "failed"):
            shutil.rmtree(staged, ignore_errors=True)
            continue
        try:
            sweep = sdir / "sweep.json"
            # sweep.json stops changing once complete, so its mtime is the capture's end
            if not json.loads(sweep.read_text()).get("complete") or now - sweep.stat().st_mtime < STAGE_MAX_SEC:
                continue
        except Exception:
            continue
        for f in staged.glob("*/*.mp4"):
            target = sdir / f.parent.name / f.name
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                _copy_durable(f, target)
        shutil.rmtree(staged, ignore_errors=True)
        log(f"pir session {staged.name}: still unmerged {STAGE_MAX_SEC // 60} min after capture "
            "-- its footage moved from RAM to the stick")


# --- main loop ------------------------------------------------------------------------

def main():
    log("outage buffer supervisor starting")
    log(f"  buffer={BUFFER_ROOT} preroll={PREROLL_SEC}s segment={SEGMENT_DURATION} "
        f"min-outage={MIN_OUTAGE_SEC}s")

    conn = Connectivity()
    latch = ProducerLatch()
    armed: set[str] = set()
    armed_for: dict[str, str] = {}       # cam -> "outage", "pir" or "outage+pir"
    capture: Capture | None = None
    pir_state = {"done": set(), "warned": set(), "last": {}, "through": {}}
    ram_was_good = None

    # The tick loop must never make a network call: see _fetch_registry(). A thread owns
    # all AWS access; this loop only reads what it publishes.
    shared = {"cameras": read_registry_cache()}
    online_flag = {"online": True}
    threading.Thread(target=registry_refresher, args=(shared, online_flag), daemon=True).start()
    if shared["cameras"]:
        log(f"registry (from cache): {shared['cameras']}")

    # NOTHING touches the buffer before buffer_ready() passes. This block used to run
    # unconditionally at startup, and on a Pi where the USB stick has not been set up
    # /mnt/vms-buffer does not exist -- creating it needs root, so mkdir raised
    # PermissionError, the process exited, Restart=on-failure fired, and the unit sat at
    # `activating` through 80 restarts. The design says a missing stick means *idle and
    # disarmed*, which is exactly what the sentinel check already implements; one
    # unguarded line ran ahead of it. `is-active` would not have shown it either: the
    # state word was `activating`, not `failed` (FoundAndFixed.md #39).
    scanned_orphans = False

    def scan_orphans():
        for orphan in sorted(OUTAGE_DIR.glob("*/state.json")):
            try:
                j = json.loads(orphan.read_text())
                if j.get("status") not in ("uploaded", "done"):
                    log(f"orphan capture from a previous run: {j['outageId']} ({j.get('status')})")
            except Exception:
                pass

    was_ready = None
    while True:
        try:
            registry = shared.get("cameras", {})

            ok, why = buffer_ready()
            # Log readiness transitions, so "idle because there is no stick" is visible
            # in the journal rather than being indistinguishable from "nothing to do".
            if ok != was_ready:
                log("buffer ready" if ok else f"buffer unavailable ({why}) -- idle, disarmed")
                was_ready = ok
            if ok and not scanned_orphans:
                OUTAGE_DIR.mkdir(parents=True, exist_ok=True)
                scan_orphans()
                scanned_orphans = True
            space_ok, space_why = (disk_ok() if ok else (False, "buffer unavailable"))
            ram_good, ram_why = ram_ok()
            if ram_good != ram_was_good:
                if not ram_good:
                    log(f"PIR ring falls back to the stick: {ram_why}")
                elif ram_was_good is False:
                    log("PIR ring back in RAM")
                ram_was_good = ram_good

            online, reason = conn.poll()
            online_flag["online"] = online

            # --- arming (reconciled, not remembered) ---------------------------------
            # Two reasons to record, one recorder per path:
            #   outage -- outageBufferSec > 0 AND the producer is active (the producer gate
            #             keeps the pre-roll off the flash 24/7, OUTAGE.md 3.5);
            #   pir    -- pirRecording on, producer or not (PIR-MQTT-VMS-PI4.md 3.6). This one
            #             IS ungated, which is why D7 moves the ring to RAM before PIR mode is
            #             left on unattended.
            want = {}
            for cam, row in registry.items():
                reasons = []
                if row["outageBufferSec"] > 0 and latch.active(cam):
                    reasons.append("outage")
                if row["pirRecording"]:
                    reasons.append("pir")
                if reasons and ok and space_ok:
                    want[cam] = "+".join(reasons)
            for cam in sorted(set(registry) | armed):
                path = camera_control.mediamtx_path_name(cam)
                should = cam in want
                # PIR records around the clock, so its ring goes to RAM (D7); outage-only stays
                # on the stick, where the producer gate already limits it.
                in_ram = should and "pir" in want[cam] and ram_good
                record_path, delete_after = (RAM_RECORD_PATH, RAM_BACKSTOP) if in_ram else (RECORD_PATH, "0s")
                try:
                    if not mediamtx_api.recording_conf_matches(
                            path, should, record_path, SEGMENT_DURATION, delete_after):
                        mediamtx_api.ensure_path_conf(path, {})
                        mediamtx_api.set_recording(path, should, record_path, SEGMENT_DURATION,
                                                   delete_after=delete_after)
                        log(f"{cam}: recording {'ARMED (' + want[cam] + ')' if should else 'disarmed'}"
                            + ((" in RAM" if in_ram else " on the stick") if should
                               else f" ({why or space_why or 'not wanted'})"))
                    elif should and armed_for.get(cam) != want[cam]:
                        log(f"{cam}: recording stays armed, now for {want[cam]}")
                    if should:
                        armed.add(cam)
                        armed_for[cam] = want[cam]
                    else:
                        armed.discard(cam)
                        armed_for.pop(cam, None)
                except mediamtx_api.MediaMTXError as e:
                    log(f"{cam}: mediamtx error: {e}")

            # --- PIR sessions ----------------------------------------------------------
            # BEFORE the outage sweep: during an outage, completed segments are renamed out of
            # live/ every tick, and linking first means a PIR session never misses one. (It
            # looks in outage/ too, for footage moved before the session opened.)
            if ok:
                try:
                    sweep_pir_sessions(pir_state)
                    tend_pir_staging()
                except Exception as e:
                    log(f"pir session sweep failed ({type(e).__name__}: {e}) -- continuing")

            # --- outage lifecycle ----------------------------------------------------
            # Only cameras armed FOR the outage reason. A PIR-only camera has limit 0, which
            # Capture reads as "no limit": capturing it would keep every segment for the
            # whole outage, the opposite of what outageBufferSec = 0 means.
            outage_armed = sorted(c for c, r in armed_for.items() if "outage" in r)
            if not online and capture is None and outage_armed:
                outage_id = utc_now().strftime("%Y%m%dT%H%M%SZ")
                limits = {c: registry[c]["outageBufferSec"] for c in outage_armed}
                capture = Capture(outage_id, limits)
                log(f"OUTAGE detected ({reason}) -- capture {outage_id} for {outage_armed}")
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
            # A camera armed only for PIR isn't part of the outage capture, so it keeps its
            # rolling window during an outage too -- otherwise its ring would grow unbounded
            # for as long as AWS stays unreachable.
            # Disarmed cameras too: a ring left behind when a camera is disarmed or moves to the
            # other place is pruned like any other, after the PIR sweep has had its chance.
            if ok:
                for cam in sorted(set(registry) | armed):
                    path = camera_control.mediamtx_path_name(cam)
                    if capture is None or cam not in capture.limit_by_cam or cam in capture.frozen:
                        prune_live(path, PREROLL_SEC)
                    if cam not in armed:
                        drop_disarmed_ram_ring(path, PREROLL_SEC)

            time.sleep(TICK_SEC)
        except KeyboardInterrupt:
            log("stopping")
            return 0
        except Exception as e:
            log(f"tick failed ({type(e).__name__}: {e}) -- continuing")
            time.sleep(TICK_SEC)


if __name__ == "__main__":
    sys.exit(main())
