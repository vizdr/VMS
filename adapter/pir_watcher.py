#!/usr/bin/env python3
"""PIR watcher -- turns the Pico's motion events into local clips (PIR-MQTT-VMS-PI4.md Phases 6, 10).

Subscribes to each PIR camera's topics on the local broker, runs the session logic from
pir_session.py (exactly the code replay-pir.py tests), journals every session on the USB
stick, and once the outage supervisor has captured a session's footage, merges and trims it
into pir/<sessionId>/clip.mp4 with a thumbnail.

One writer per file (§3.6):
  this process      -> pir/<id>/state.json, clip.mp4, thumb-1.jpg; local retention (D8)
  kvs-outage-buffer -> MediaMTX recording, the ring, pir/<id>/<path>/ links, sweep.json

**Local recording makes no AWS call**: the registry comes from the supervisor's cache, so it
keeps working with the internet down. The cloud side -- the status item and the clip index in
pir-local (§3.10-§3.11) -- is pir_cloud.py's thread: best effort, short timeouts, and it
catches up after an outage on its own. A persistent MQTT session (fixed client id,
clean_session=False) means events published while this process is down are delivered when
it returns; pir_session ages them and drops any older than STALE_SEC.

Usage: pir_watcher.py [--dry-run]     # --dry-run: log sessions, write no journals or clips
"""
import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera_control
from outage_buffer import (DISK_FLOOR_BYTES, DISK_FLOOR_FRACTION, PIR_DIR, RAM_PIR_DIR,
                           buffer_ready, read_registry_cache, ring_segments, segment_start)
from outage_uploader import ffprobe, merge, partition_runs
from pir_cloud import CloudSync
from pir_session import PRE_ROLL_SEC, PicoClock, PirTrigger

CLIENT_ID = "vms-pir-watcher"           # fixed: the broker keeps this session's queue
HEARTBEAT_SEC = 10
REGISTRY_POLL_SEC = 60                  # same cadence as event_watcher.py and the supervisor
RING_FRESH_SEC = 60                     # §3.3: newest segment older than 2 x 30 s = not recording
NTP_WAIT_SEC = 300                      # then carry on: offline, the saved clock is still consistent
CLOCK_JUMP_SEC = 2
ASSEMBLY_GIVE_UP_SEC = 600              # merge whatever was captured if sweep never completes
# D8: 14 days. PIR_RETENTION_DAYS overrides it, for Phase 12's soak (one day) -- never in normal use.
RETENTION_DAYS = float(os.environ.get("PIR_RETENTION_DAYS") or 14)
RETENTION_EVERY_SEC = 600
DISK_MARGIN_BYTES = 2 * 1024 ** 3       # act before the supervisor's floor disarms recording
THUMB_AFTER_T0_SEC = 2                  # D13: one frame just after motion began
SUFFIXES = (("/pir/event", "event"), ("/pir/state", "state"), ("/status", "status"),
            ("/pir/cmd/retrigger", "cmd"))   # recorded for the admin panel; the Pico reports no mode
RECENT_EVENTS = 12

STATE_DIR = Path.home() / ".local" / "state" / "vms"
CLOCK_FILE = STATE_DIR / "pir-watcher-clocks.json"
HEARTBEAT_FILE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "vms" / "pir-state.json"

_stop = False


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def write_json_atomic(path: Path, data, fsync=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def ntp_synced():
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "yes"
    except Exception:
        return None


def topic_base(pir_topic):
    """'home/pico2w-01/pir/event' -> 'home/pico2w-01'; None if it isn't that shape."""
    return pir_topic[:-len("/pir/event")] if pir_topic and pir_topic.endswith("/pir/event") else None


class Camera:
    def __init__(self, camera_id, base, clock):
        self.id = camera_id
        self.base = base
        self.path = camera_control.mediamtx_path_name(camera_id)
        self.trigger = PirTrigger(clock=clock)
        self.enabled = False
        self.journal = None          # sessionId of the open session
        self.journal_status = None
        self.last_event_at = self.last_clip_at = None
        self.clips_today = 0

    def ring_recording(self):
        """§3.3's ring check: the supervisor armed this camera AND footage is arriving, in RAM
        or on the stick (Phase 12). A hung publisher (FoundAndFixed.md #51) leaves the ring
        armed and empty -- this sees it."""
        try:
            newest = ring_segments(self.path)[-1]
            return time.time() - newest.stat().st_mtime < RING_FRESH_SEC
        except (IndexError, OSError):
            return False


class Watcher:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.q = queue.Queue()
        self.cams = {}               # cameraId -> Camera
        self.picos = {}              # topic base -> what the Pico last said
        self.pico_clocks = {}        # topic base -> a display-only clock: lateness in the panel
        self.recent = {}             # topic base -> the last RECENT_EVENTS events, PIR on or off
        self.subscribed = set()
        self.connected = False
        self.client = None
        self.clock_jumps = 0
        self.saved_clocks = self._load_clocks()
        self.assembling = set()
        self.assembly_q = queue.Queue()
        self.cloud = None            # pir_cloud.CloudSync, unless --dry-run or no AWS config
        # Refreshed by heartbeat(), read on every status build: the glob, statvfs-free mount
        # check and timedatectl call are too slow for the once-a-second path.
        self.ring = {}               # cameraId -> ring_recording()
        self.buffer = (True, "")
        self.ntp = None
        self.sessions_today = {}     # cameraId -> session folders started today (local date)

    # --- registry ---------------------------------------------------------------------------
    def reload_registry(self, now):
        registry = read_registry_cache()
        wanted = {}
        for cam, row in registry.items():
            base = topic_base(row.get("pirTopic"))
            if row.get("pirTopic") and not base:
                log(f"{cam}: pirTopic {row['pirTopic']!r} doesn't end in /pir/event -- ignored")
            if base:
                wanted[cam] = (base, row.get("pirRecording"))
        for cam_id, (base, enabled) in wanted.items():
            cam = self.cams.get(cam_id)
            if cam is None:
                cam = Camera(cam_id, base, PicoClock.from_dict(self.saved_clocks.get(cam_id)))
                self.cams[cam_id] = cam
                self._restore_open_session(cam)
                log(f"{cam_id}: watching {base}/# ({'PIR on' if enabled else 'PIR off'})")
            if cam.enabled and not enabled and cam.trigger.session.seq is not None:
                self._close_now(cam, now, "switched off")
            if cam.enabled != bool(enabled):
                log(f"{cam_id}: PIR trigger {'ON' if enabled else 'OFF'}")
            cam.enabled = bool(enabled)
        for cam_id in list(self.cams):
            if cam_id not in wanted:
                cam = self.cams.pop(cam_id)
                if cam.trigger.session.seq is not None:
                    self._close_now(cam, now, "camera removed")
                log(f"{cam_id}: no longer has a PIR sensor")
        self._subscribe_all()

    def _subscribe_all(self):
        if not self.connected:
            return
        for base in {c.base for c in self.cams.values()} - self.subscribed:
            for suffix, _ in SUFFIXES:
                self.client.subscribe(base + suffix, qos=1)
            self.subscribed.add(base)

    # --- MQTT -------------------------------------------------------------------------------
    def start_mqtt(self):
        import config
        import paho.mqtt.client as mqtt
        host, port = config.get("MQTT_HOST"), int(config.get("MQTT_PORT"))
        user, pw_file = config.get("MQTT_USER"), config.get("MQTT_PASSWORD_FILE")
        password = Path(pw_file).read_text().strip()

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                log(f"broker refused the connection: {reason_code}")
                return
            self.q.put(("connected", time.time()))

        def on_disconnect(client, userdata, flags, reason_code, properties):
            self.q.put(("disconnected", time.time(), str(reason_code)))

        def on_message(client, userdata, msg):
            self.q.put(("msg", msg.topic, msg.payload, bool(msg.retain), time.time()))

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID, clean_session=False)
        c.username_pw_set(user, password)
        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.connect_async(host, port, keepalive=30)
        c.loop_start()
        self.client = c
        log(f"connecting to {host}:{port} as {user} (persistent session {CLIENT_ID})")

    def handle(self, item):
        kind = item[0]
        if kind == "connected":
            self.connected, now = True, item[1]
            self.subscribed.clear()          # re-subscribe: idempotent, and covers a new session
            for cam in self.cams.values():
                cam.trigger.connected(now)   # its persistent session may now deliver backlog
            self._subscribe_all()
            log("connected to the broker")
        elif kind == "disconnected":
            if self.connected:
                log(f"disconnected from the broker ({item[2]})")
            self.connected = False
        elif kind == "msg":
            self.on_message(*item[1:])

    def on_message(self, topic, raw, retained, recv):
        kind = next((k for s, k in SUFFIXES if topic.endswith(s)), None)
        if kind is None:
            return
        base = topic[:-len(next(s for s, k in SUFFIXES if k == kind))]
        try:
            text = raw.decode()
            try:
                payload = json.loads(text)
            except ValueError:
                payload = text
        except UnicodeDecodeError:
            return
        pico = self.picos.setdefault(base, {})
        if kind == "cmd":
            pico.update(retriggerCmd=str(payload), retriggerCmdAt=recv)
            return                           # a command TO the Pico, never a trigger
        if not retained and isinstance(payload, dict) and isinstance(payload.get("boot_ms"), int):
            clock = self.pico_clocks.setdefault(base, PicoClock())
            clock.observe("state" if kind == "state" else "event", payload["boot_ms"], recv)
            if kind == "event":
                t = clock.event_time(payload["boot_ms"])
                self.recent.setdefault(base, deque(maxlen=RECENT_EVENTS)).append({
                    "at": iso(recv), "event": payload.get("event"), "seq": payload.get("seq"),
                    "lateSec": round(recv - t, 1) if t else None,
                    "durationMs": payload.get("duration_ms")})
        if kind == "status":
            pico.update(online=payload == "online", statusAt=recv)
        elif kind == "state" and isinstance(payload, dict):
            pico.update(lastStateAt=recv, motion=payload.get("motion"), count=payload.get("count"),
                        warmingUp=bool(payload.get("warming_up")), dropped=payload.get("dropped"),
                        bootMs=payload.get("boot_ms"))
        if retained:
            return                           # a replay at subscribe time: never a trigger
        for cam in self.cams.values():
            if cam.base != base or not cam.enabled:
                continue
            clips = cam.trigger.message(kind, payload, recv, ring_ok=cam.ring_recording())
            if kind == "event":
                cam.last_event_at = recv
            self._after_trigger(cam, clips, recv)

    # --- sessions and journals ----------------------------------------------------------------
    def _after_trigger(self, cam, clips, now):
        for clip in clips:
            self._close_journal(cam, clip)
        s = cam.trigger.session
        if s.seq is not None and cam.journal is None:
            self._open_journal(cam)
        elif s.state == "post-roll" and cam.journal_status == "recording":
            self._update_journal(cam, status="post-roll", end=s.end)

    def _open_journal(self, cam):
        s = cam.trigger.session
        sid = f"{cam.id}-{datetime.fromtimestamp(s.t0, timezone.utc):%Y%m%dT%H%M%SZ}"
        n = 2
        while (PIR_DIR / sid).exists():
            sid = f"{cam.id}-{datetime.fromtimestamp(s.t0, timezone.utc):%Y%m%dT%H%M%SZ}-{n}"
            n += 1
        cam.journal, cam.journal_status = sid, "recording"
        log(f"{cam.id}: session {sid} opened (seq {s.seq}, motion at "
            f"{datetime.fromtimestamp(s.t0).strftime('%H:%M:%S')})" + (" [dry-run]" if self.dry_run else ""))
        if self.dry_run:
            return
        write_json_atomic(PIR_DIR / sid / "state.json", {
            "sessionId": sid, "cameraId": cam.id, "seq": s.seq, "t0": s.t0,
            "from": s.t0 - PRE_ROLL_SEC, "to": None, "status": "recording",
            "openedAt": iso(time.time())})

    def _update_journal(self, cam, **fields):
        cam.journal_status = fields.get("status", cam.journal_status)
        if self.dry_run or cam.journal is None:
            return
        path = PIR_DIR / cam.journal / "state.json"
        try:
            j = json.loads(path.read_text())
        except Exception:
            return
        j.update(fields)
        write_json_atomic(path, j)

    def _close_journal(self, cam, clip):
        cam.last_clip_at, cam.clips_today = time.time(), cam.clips_today + 1
        log(f"{cam.id}: session {cam.journal} closed by {clip.reason}: clip "
            f"{datetime.fromtimestamp(clip.start).strftime('%H:%M:%S')} + {clip.duration:.1f} s"
            + (" [dry-run]" if self.dry_run else ""))
        self._update_journal(cam, status="closed", to=clip.end, reason=clip.reason,
                             closedAt=iso(time.time()))
        cam.journal, cam.journal_status = None, None

    def _close_now(self, cam, now, reason):
        clip = cam.trigger.session.finish(now)
        if clip:
            clip.reason = reason
            self._close_journal(cam, clip)

    def _restore_open_session(self, cam):
        """After a restart: re-open the session this camera had open, so the stop that the
        broker kept for us closes it at its real time instead of the cap."""
        if not PIR_DIR.is_dir():
            return
        for path in sorted(PIR_DIR.glob(f"{cam.id}-*/state.json")):
            try:
                j = json.loads(path.read_text())
            except Exception:
                continue
            if j.get("status") in ("recording", "post-roll") and j.get("to") is None:
                s = cam.trigger.session
                s.seq, s.t0, s.end = j["seq"], j["t0"], j.get("end")
                cam.journal, cam.journal_status = j["sessionId"], j["status"]
                log(f"{cam.id}: resumed open session {j['sessionId']} ({j['status']})")
                return

    # --- assembly -------------------------------------------------------------------------------
    def schedule_assembly(self, now):
        if self.dry_run or not PIR_DIR.is_dir():
            return
        for path in PIR_DIR.glob("*/state.json"):
            sid = path.parent.name
            if sid in self.assembling:
                continue
            try:
                j = json.loads(path.read_text())
            except Exception:
                continue
            if j.get("status") != "closed":
                continue
            try:
                sweep = json.loads((path.parent / "sweep.json").read_text())
            except Exception:
                sweep = {}
            if sweep.get("complete") or now - float(j["to"]) > ASSEMBLY_GIVE_UP_SEC:
                self.assembling.add(sid)
                self.assembly_q.put(sid)

    def assembly_worker(self):
        while True:
            sid = self.assembly_q.get()
            try:
                assemble(sid)
            except Exception as e:
                log(f"session {sid}: assembly crashed ({type(e).__name__}: {e})")
            finally:
                self.assembling.discard(sid)

    # --- housekeeping ---------------------------------------------------------------------------
    def check_clock_jump(self, last):
        """The Pi has no RTC: an NTP sync after an offline boot steps the clock. Event times and
        segment names would then straddle two clocks, so close any open session at the last
        good time and start a fresh clock reference."""
        wall, mono = time.time(), time.monotonic()
        if last and abs((wall - last[0]) - (mono - last[1])) > CLOCK_JUMP_SEC:
            jump = (wall - last[0]) - (mono - last[1])
            self.clock_jumps += 1
            log(f"wall clock jumped {jump:+.1f} s -- closing open sessions, resetting clock references")
            for cam in self.cams.values():
                if cam.trigger.session.seq is not None:
                    self._close_now(cam, last[0], "clock jump")
                cam.trigger.clock = PicoClock()
        return wall, mono

    def heartbeat(self):
        self.buffer, self.ntp = buffer_ready(), ntp_synced()
        today = datetime.now().date()
        started = Counter()
        for d in (PIR_DIR.iterdir() if self.buffer[0] and PIR_DIR.is_dir() else []):
            try:      # cam-01-20261003T144926Z[-2]: the session's start, in UTC
                t0 = datetime.strptime(d.name.split("-")[2], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (IndexError, ValueError):
                continue
            if t0.astimezone().date() == today:
                started["-".join(d.name.split("-")[:2])] += 1
        self.sessions_today = dict(started)
        cams = {}
        for c in self.cams.values():
            s = c.trigger.session
            self.ring[c.id] = c.ring_recording()
            cams[c.id] = {
                "pirTopic": c.base + "/pir/event", "pirRecording": c.enabled,
                "effective": self.effective(c),
                "ringRecording": self.ring[c.id], "session": s.state, "sessionId": c.journal,
                "lastEventAt": iso(c.last_event_at), "lastClipAt": iso(c.last_clip_at),
                "clipsSinceStart": c.clips_today, "clockValid": c.trigger.clock.valid,
                "picoReboots": c.trigger.clock.reboots, "counts": dict(c.trigger.counts()),
            }
        picos = {b: {**{k: (iso(v) if k.endswith("At") else v) for k, v in p.items()},
                     "recentEvents": list(reversed(self.recent.get(b, [])))}
                 for b, p in self.picos.items()}
        write_json_atomic(HEARTBEAT_FILE, {
            "ts": iso(time.time()), "pid": os.getpid(), "dryRun": self.dry_run,
            "ntpSynced": ntp_synced(), "brokerConnected": self.connected,
            "clockJumps": self.clock_jumps, "picos": picos, "cameras": cams,
            "cloud": self.cloud.health() if self.cloud else None}, fsync=True)
        if not self.dry_run:
            clocks = {c.id: c.trigger.clock.to_dict() for c in self.cams.values()
                      if c.trigger.clock.to_dict()}
            if clocks and clocks != self.saved_clocks:
                write_json_atomic(CLOCK_FILE, clocks)
                self.saved_clocks = clocks

    def _load_clocks(self):
        try:
            return json.loads(CLOCK_FILE.read_text())
        except Exception:
            return {}

    # --- the cloud status item (§3.10) ----------------------------------------------------------
    def effective(self, cam):
        """The actual state, not just the setting: what a viewer of the cloud page needs to know
        when 'PIR on' is not producing clips."""
        if not cam.enabled:
            return "off"
        if not self.connected:
            return "enabled, but the local broker is unreachable"
        if self.picos.get(cam.base, {}).get("online") is False:
            return "enabled, but the Pico is offline"
        if not self.buffer[0]:
            return f"enabled, but the USB stick isn't usable ({self.buffer[1]})"
        if not self.ring.get(cam.id):
            return "enabled, but the ring is not recording"
        return "armed"

    def cloud_status(self, cam):
        """Only values that change when something happens: a timestamp that moves every second
        would defeat the change detection and write every 5 s."""
        pico, s = self.picos.get(cam.base, {}), cam.trigger.session
        recent = self.recent.get(cam.base)
        return {
            "pirTopic": cam.base + "/pir/event",
            "pirRecording": cam.enabled, "effective": self.effective(cam),
            "ringRecording": self.ring.get(cam.id), "picoOnline": pico.get("online"),
            "motion": pico.get("motion"), "brokerConnected": self.connected,
            "session": s.state, "sessionId": cam.journal,
            "lastEvent": recent[-1] if recent else None, "lastClipAt": iso(cam.last_clip_at),
            "sessionsToday": self.sessions_today.get(cam.id, 0),
            "counts": dict(cam.trigger.counts()), "clockValid": cam.trigger.clock.valid,
            "picoReboots": cam.trigger.clock.reboots, "ntpSynced": self.ntp,
            "clockJumps": self.clock_jumps,
        }

    def publish_status(self):
        if self.cloud:
            for cam in self.cams.values():
                self.cloud.set_status(cam.id, self.cloud_status(cam))

    def start_cloud(self):
        if self.dry_run:
            return
        try:
            import config
            region = config.get("AWS_REGION")
        except Exception as e:  # noqa: BLE001 -- local recording must not depend on it
            log(f"cloud status disabled ({e}); local recording is unaffected")
            return
        self.cloud = CloudSync(PIR_DIR, region, log)
        self.publish_status()        # so the first pass already knows the cameras
        self.cloud.start()

    # --- main loop ---------------------------------------------------------------------------
    def run(self):
        log(f"pir watcher starting (dry_run={self.dry_run})"
            + (f" -- retention {RETENTION_DAYS:g} days (PIR_RETENTION_DAYS)" if RETENTION_DAYS != 14 else ""))
        deadline = time.monotonic() + NTP_WAIT_SEC
        while not _stop and ntp_synced() is False and time.monotonic() < deadline:
            log("waiting for NTP: the Pi has no RTC, and clips must share the segments' clock")
            time.sleep(15)
        if ntp_synced() is False:
            log(f"no NTP sync after {NTP_WAIT_SEC} s -- going on with the saved clock "
                "(event and segment times stay consistent; a later sync is handled as a jump)")
        self.reload_registry(time.time())
        self.heartbeat()
        self.start_mqtt()
        self.start_cloud()
        threading.Thread(target=self.assembly_worker, daemon=True).start()
        last_reg = last_beat = last_ret = 0.0
        last_clock = None
        while not _stop:
            try:
                self.handle(self.q.get(timeout=1))
                while not self.q.empty():
                    self.handle(self.q.get_nowait())
            except queue.Empty:
                pass
            now = time.time()
            last_clock = self.check_clock_jump(last_clock)
            for cam in self.cams.values():
                self._after_trigger(cam, cam.trigger.tick(now), now)
            mono = time.monotonic()
            if mono - last_reg >= REGISTRY_POLL_SEC:
                self.reload_registry(now)
                last_reg = mono
            if mono - last_beat >= HEARTBEAT_SEC:
                self.schedule_assembly(now)
                self.heartbeat()
                last_beat = mono
            if not self.dry_run and mono - last_ret >= RETENTION_EVERY_SEC:
                retention()
                last_ret = mono
            self.publish_status()            # cheap: the thread writes only what changed
        log("stopping -- open sessions stay journalled and resume on restart")
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        self.heartbeat()


# --- assembly: merge + trim + thumbnail ----------------------------------------------------------

def assemble(sid):
    """Merge the session's segments -- staged in RAM, or on the stick (from an outage capture, or
    moved there by the supervisor) -- into clip.mp4 on the stick. The clip and its thumbnail are
    the only things a session writes to the stick (Phase 12)."""
    sdir = PIR_DIR / sid
    j = json.loads((sdir / "state.json").read_text())
    path = camera_control.mediamtx_path_name(j["cameraId"])
    staged = RAM_PIR_DIR / sid
    segs = sorted({p.name: p for d in (sdir / path, staged / path) if d.is_dir()
                   for p in d.glob("*.mp4")}.values(), key=lambda p: p.name)
    if not segs:
        try:
            listed = json.loads((sdir / "sweep.json").read_text()).get("segments")
        except Exception:
            listed = None
        _set(sdir, j, status="failed", error=(
            "its footage was staged in RAM and is gone -- the Pi restarted before the merge" if listed
            else "no footage captured -- was the ring recording?"))
        log(f"session {sid}: FAILED, " + ("footage lost from RAM" if listed else "no footage"))
        return
    runs = partition_runs(segs)
    if not runs:
        _set(sdir, j, status="failed", error="no readable segment")
        log(f"session {sid}: FAILED, no readable segment")
        return
    run = max(runs, key=len)                 # a layout change mid-session keeps the longest part
    frm, to = float(j["from"]), float(j["to"])
    first, last = segment_start(run[0][0]).timestamp(), segment_start(run[-1][0]).timestamp()
    inpoint = max(0.0, frm - first)
    outpoint = to - last if to > last else None
    tmp = sdir / ".clip-tmp.mp4"
    if not merge(run, tmp, inpoint=inpoint or None, outpoint=outpoint):
        _set(sdir, j, status="failed", error="merge failed")
        return
    probe = ffprobe(tmp)
    if probe is None:
        tmp.unlink(missing_ok=True)
        _set(sdir, j, status="failed", error="merged clip unreadable")
        return
    duration = float(probe["format"]["duration"])
    vcodec = next((s["codec_name"] for s in probe["streams"] if s["codec_type"] == "video"), None)
    tmp.rename(sdir / "clip.mp4")
    thumbs = []
    at = min(max(0.0, PRE_ROLL_SEC + THUMB_AFTER_T0_SEC), max(0.0, duration - 0.5))
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at:.2f}", "-i",
                        str(sdir / "clip.mp4"), "-frames:v", "1", "-vf", "scale=160:-2",
                        "-q:v", "5", "-y", str(sdir / "thumb-1.jpg")], capture_output=True)
    if r.returncode == 0:
        thumbs.append("thumb-1.jpg")
    shutil.rmtree(sdir / path, ignore_errors=True)     # the links; the clip replaces them
    shutil.rmtree(staged, ignore_errors=True)
    _set(sdir, j, status="merged", clip="clip.mp4", thumbnails=thumbs, durationSec=round(duration, 2),
         videoCodec={"hevc": "h265"}.get(vcodec, vcodec), sizeBytes=(sdir / "clip.mp4").stat().st_size,
         mergedAt=iso(time.time()), window=round(to - frm, 2))
    log(f"session {sid}: clip ready, {duration:.1f} s for a {to - frm:.1f} s window "
        f"({len(run)} segment(s), {vcodec})")


def _set(sdir, j, **fields):
    j.update(fields)
    write_json_atomic(sdir / "state.json", j)


# --- local retention (D8) ------------------------------------------------------------------------

def retention():
    ok, _ = buffer_ready()
    if not ok or not PIR_DIR.is_dir():
        return
    done = []
    for path in PIR_DIR.glob("*/state.json"):
        try:
            j = json.loads(path.read_text())
        except Exception:
            continue
        if j.get("status") not in ("merged", "failed"):
            continue                          # open, closed or assembling: never touched
        sdir = path.parent
        # A request from either GUI: the admin app's marker, or the uploader's record of a cloud
        # request (outage_uploader.py writes cloud-request.json when it picks one up).
        pending = (((sdir / "upload-requested").exists() or (sdir / "cloud-request.json").exists())
                   and not (sdir / "uploaded.json").exists())
        done.append((float(j.get("to") or j.get("from") or 0), sdir, (sdir / "keep").exists(), pending))
    done.sort()
    cutoff = time.time() - RETENTION_DAYS * 86400
    for when, sdir, kept, pending in list(done):
        if when < cutoff and not kept and not pending:
            shutil.rmtree(sdir, ignore_errors=True)
            done.remove((when, sdir, kept, pending))
            log(f"retention: {sdir.name} deleted (older than {RETENTION_DAYS:g} days)")
    st = os.statvfs(PIR_DIR)
    floor = max(DISK_FLOOR_BYTES, int(st.f_blocks * st.f_frsize * DISK_FLOOR_FRACTION)) + DISK_MARGIN_BYTES
    # Near the supervisor's floor -- below it, recording is disarmed. Oldest unkept first,
    # pending uploads last, kept clips never.
    for pass_pending in (False, True):
        for when, sdir, kept, pending in list(done):
            if os.statvfs(PIR_DIR).f_bavail * st.f_frsize >= floor:
                return
            if kept or pending != pass_pending:
                continue
            shutil.rmtree(sdir, ignore_errors=True)
            done.remove((when, sdir, kept, pending))
            log(f"retention: {sdir.name} deleted to keep free space above the recording floor")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    def _sig(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    Watcher(args.dry_run).run()


if __name__ == "__main__":
    main()
