#!/usr/bin/env python3
"""PIR clip sessions -- the decision logic of the planned kvs-pir-watcher, as pure code.

PIR-MQTT-VMS-PI4.md §3.3 (the session) and §3.5 (event ageing). No MQTT, no files and no
clock reads: every method is told the time it acts at. That is what lets
adapter/bin/replay-pir.py run a recorded observe_pir.py log through exactly the logic the
watcher will run, the way replay-gate.py does for event_watcher's ClipGate.

Three pieces, each with one job:

* PicoClock  -- places an event on the Pi's clock from the Pico's boot_ms (§3.5);
* ClipSession -- one camera's IDLE -> RECORDING -> POST_ROLL machine, which ignores
  everything that arrives while a clip is running (§3.3);
* PirTrigger -- the glue the watcher calls per message: dedupe, ageing, the stale limit.

Run this file directly to check the scenarios the design depends on.
"""
from collections import Counter, deque
from dataclasses import dataclass

PRE_ROLL_SEC = 12
POST_SEC = 5                 # Appendix A: ~4-5 s until Phase 2 measures it
MAX_SEC = 180                # §3.4: a product cap, ~23 MB at cam-01's bitrate
STALE_SEC = 60               # §3.5 invariant: 60 + 12 + 30 + 5 < 120 s ring
CLOCK_WINDOW_SEC = 600       # follows crystal drift (<= 30 ppm ~ 18 ms per 10 min)
SETTLE_SEC = 5               # a message this soon after a (re)connect may be broker backlog
REBOOT_TOLERANCE_MS = 1000


class PicoClock:
    """Maps the Pico's boot_ms onto the Pi's wall clock (§3.5).

    offset = min(receive_time - boot_ms) over recent messages. Delay only ever *adds* to
    that difference, so the minimum comes from the least-delayed message, and a late or
    replayed message can't drag it the wrong way. The one danger is having no live sample
    at all -- then the minimum is a backlog message and every age is underestimated. So
    the reference only counts as valid once a message arrives at least SETTLE_SEC after a
    connect (it can't be backlog), or once it was restored for the same Pico boot.

    Reboots are detected from pir/state ALONE: the Pico publishes it in order and stamps it
    at publish time. An event's boot_ms is its edge time, so a queued event legitimately
    carries an older value than the latest state -- treating that as a reboot misread
    every late event when this rule was first tested (observe_pir.py, 2026-10-03).
    """

    def __init__(self):
        self.samples = deque()           # (receive time, receive_ms - boot_ms)
        self.last_state_ms = None        # boot_ms of the latest pir/state
        self.connected_at = None
        self.valid = False
        self.restored = False            # reference came from to_dict(), not yet confirmed
        self.reboots = 0

    def connected(self, now):
        self.connected_at = now

    def observe(self, kind, boot_ms, recv):
        """Feed every live message that carries boot_ms. kind: 'state' or 'event'."""
        if kind == "state":
            if self.last_state_ms is not None and boot_ms < self.last_state_ms - REBOOT_TOLERANCE_MS:
                self._reset()
                self.reboots += 1
            self.last_state_ms = boot_ms
            self.restored = False        # a state of the current boot confirms the reference
        self.samples.append((recv, recv * 1000 - boot_ms))
        while len(self.samples) > 1 and self.samples[0][0] < recv - CLOCK_WINDOW_SEC:
            self.samples.popleft()
        if self.connected_at is None or recv - self.connected_at >= SETTLE_SEC:
            self.valid = True

    def event_time(self, boot_ms):
        """Pi-clock time of an event, or None when no trustworthy reference exists."""
        if not self.valid or not self.samples:
            return None
        # Restored after a watcher restart, and no state seen yet: an event older than the
        # last state we knew can only come from a new boot -- the backlog holds only what
        # was published after we went away.
        if self.restored and boot_ms < self.last_state_ms - REBOOT_TOLERANCE_MS:
            return None
        return (min(d for _, d in self.samples) + boot_ms) / 1000

    def to_dict(self):
        """What the watcher persists, so a restart keeps the reference (§3.5)."""
        if not self.valid or not self.samples:
            return None
        recv, d = min(self.samples, key=lambda s: s[1])
        return {"offset_ms": d, "last_state_ms": self.last_state_ms, "saved_at": recv}

    @classmethod
    def from_dict(cls, data):
        clock = cls()
        if data:
            clock.samples.append((data["saved_at"], data["offset_ms"]))
            clock.last_state_ms = data["last_state_ms"]
            clock.valid = clock.restored = True
        return clock

    def _reset(self):
        self.samples.clear()
        self.valid = False
        self.restored = False


@dataclass
class Clip:
    seq: int
    start: float                 # t0 - PRE_ROLL_SEC, Pi clock
    end: float                   # stop + POST_SEC, or the cap, or when the Pico went offline
    reason: str                  # "stop", "max", "offline", "end of log"

    @property
    def duration(self):
        return self.end - self.start


class ClipSession:
    """One camera's session machine (§3.3). All times are Pi-clock seconds.

        IDLE --start(N)--> RECORDING(N, t0) --stop(N) at t1--> POST_ROLL until t1 + POST_SEC
        RECORDING/POST_ROLL: any other start or stop is ignored
        t0 + MAX_SEC, or the Pico going offline, closes the session at that moment

    Call tick(now) before every input: it closes a session whose deadline has passed, so
    a start that arrives after the post-roll opens a new clip instead of being ignored.
    """

    def __init__(self, post_sec=POST_SEC, max_sec=MAX_SEC, pre_roll_sec=PRE_ROLL_SEC):
        self.post_sec, self.max_sec, self.pre_roll_sec = post_sec, max_sec, pre_roll_sec
        self.seq = self.t0 = self.end = None
        self.counts = Counter()

    @property
    def state(self):
        if self.seq is None:
            return "idle"
        return "recording" if self.end is None else "post-roll"

    def deadline(self):
        if self.seq is None:
            return None
        cap = self.t0 + self.max_sec
        return cap if self.end is None else min(self.end, cap)

    def tick(self, now):
        if self.seq is not None and now >= self.deadline():
            stopped = self.end is not None and self.end <= self.t0 + self.max_sec
            return self._close(self.deadline(), "stop" if stopped else "max")
        return None

    def start(self, seq, t, ring_ok=True):
        if self.seq is not None:
            self.counts["ignored start"] += 1
            return "ignored"
        if not ring_ok:
            # Publishing anyway would make a clip with no footage -- the silent failure
            # this project keeps documenting. Stay idle and say why.
            self.counts["skipped: ring not recording"] += 1
            return "skipped"
        self.seq, self.t0, self.end = seq, t, None
        return "opened"

    def stop(self, seq, t):
        if self.seq is None:
            self.counts["stray stop"] += 1           # its start was stale, skipped or capped
        elif seq != self.seq or self.end is not None:
            self.counts["ignored stop"] += 1
        else:
            self.end = min(t + self.post_sec, self.t0 + self.max_sec)

    def offline(self, t):
        if self.seq is None:
            return None
        return self._close(min(t, self.deadline()), "offline")

    def finish(self, t):
        """End of input (a replay's last record): close whatever is open."""
        if self.seq is None:
            return None
        return self._close(min(t, self.deadline()), "end of log")

    def _close(self, at, reason):
        clip = Clip(self.seq, self.t0 - self.pre_roll_sec, at, reason)
        self.seq = self.t0 = self.end = None
        self.counts[f"clips closed by {reason}"] += 1
        return clip


class PirTrigger:
    """Everything between one camera's MQTT messages and its clips (§3.3, §3.5).

    message() takes a live (non-retained) message as the watcher receives it. Retained
    messages are a replay at subscribe time and must not reach it: a retained pir/state
    would otherwise count as a clock sample, and only pir/event may ever start a clip.
    """

    def __init__(self, post_sec=POST_SEC, max_sec=MAX_SEC, stale_sec=STALE_SEC,
                 pre_roll_sec=PRE_ROLL_SEC, clock=None):
        self.clock = clock or PicoClock()
        self.session = ClipSession(post_sec, max_sec, pre_roll_sec)
        self.stale_sec = stale_sec
        self._seen = deque(maxlen=512)       # QoS 1 may deliver a message twice
        self._counts = Counter()

    def connected(self, now):
        self.clock.connected(now)

    def tick(self, now):
        clip = self.session.tick(now)
        return [clip] if clip else []

    def message(self, kind, payload, recv, ring_ok=True):
        """kind: 'event', 'state' or 'status'. Returns the clips this message closed."""
        clips = self.tick(recv)
        if kind == "status":
            if payload == "offline":
                clip = self.session.offline(recv)
                clips += [clip] if clip else []
            return clips
        if not isinstance(payload, dict) or not isinstance(payload.get("boot_ms"), int):
            self._counts["unusable message"] += 1
            return clips
        if kind == "state":
            self.clock.observe("state", payload["boot_ms"], recv)
            return clips

        event, seq = payload.get("event"), payload.get("seq")
        if event not in ("start", "stop"):
            self._counts["unusable message"] += 1
            return clips
        key = (self.clock.reboots, seq, event)   # seq restarts when the Pico reboots
        if key in self._seen:
            self._counts["duplicate"] += 1
            return clips
        self._seen.append(key)

        self.clock.observe("event", payload["boot_ms"], recv)
        t = self.clock.event_time(payload["boot_ms"])
        if t is None:
            self._counts[f"unageable {event}"] += 1
            return clips
        if recv - t > self.stale_sec:
            if event == "start":
                self._counts["stale start (dropped)"] += 1
                return clips
            self._counts["stale stop (applied)"] += 1  # it still closes its session
        if event == "start":
            self.session.start(seq, t, ring_ok)
        else:
            self.session.stop(seq, t)
        return clips

    def finish(self, t):
        clip = self.session.finish(t)
        return [clip] if clip else []

    def counts(self):
        return self._counts + self.session.counts


# --- the scenarios the design depends on ------------------------------------------------------

def _self_test():
    """Each case is a rule from §3.3/§3.5 that a regression would break silently."""
    BASE = 1_000_000.0                         # Pi time when the Pico booted (boot_ms = 0)
    bm = lambda pi_t: int((pi_t - BASE) * 1000)

    def feed(trig, kind, payload_time, recv, **p):
        payload = {"boot_ms": bm(payload_time), **p}
        return trig.message(kind, payload, recv)

    # 1. A start/stop pair makes one clip: [t0 - 12, stop + 5], closed once the post-roll ends.
    tr = PirTrigger()
    tr.connected(BASE)
    feed(tr, "state", BASE + 100, BASE + 100.01)
    feed(tr, "event", BASE + 110, BASE + 110.01, seq=1, event="start")
    feed(tr, "event", BASE + 114, BASE + 114.01, seq=1, event="stop")
    assert tr.session.state == "post-roll"
    (c,) = tr.tick(BASE + 119.5)
    # (every message here has 10 ms of LAN delay, which the reference absorbs -- §3.5 says the
    # clock is accurate to the LAN latency, so compare to 0.1 s)
    assert (round(c.start - BASE, 1), round(c.end - BASE, 1), c.reason) == (98.0, 119.0, "stop"), c

    # 2. While recording or in post-roll, other starts and stops are ignored.
    tr = PirTrigger()
    feed(tr, "event", BASE + 10, BASE + 10, seq=1, event="start")
    feed(tr, "event", BASE + 11, BASE + 11, seq=2, event="start")        # ignored
    feed(tr, "event", BASE + 12, BASE + 12, seq=1, event="stop")
    feed(tr, "event", BASE + 13, BASE + 13, seq=3, event="start")        # ignored, post-roll
    (c,) = tr.tick(BASE + 20)
    assert c.seq == 1 and tr.counts()["ignored start"] == 2, tr.counts()

    # 3. A start after the post-roll opens a new clip rather than being ignored.
    tr = PirTrigger()
    feed(tr, "event", BASE + 10, BASE + 10, seq=1, event="start")
    feed(tr, "event", BASE + 12, BASE + 12, seq=1, event="stop")
    clips = feed(tr, "event", BASE + 30, BASE + 30, seq=2, event="start")
    assert len(clips) == 1 and tr.session.seq == 2

    # 4. No stop: the cap closes the clip at t0 + MAX_SEC.
    tr = PirTrigger()
    feed(tr, "event", BASE + 10, BASE + 10, seq=1, event="start")
    (c,) = tr.tick(BASE + 10 + MAX_SEC + 1)
    assert c.reason == "max" and round(c.end - BASE) == 10 + MAX_SEC

    # 5. A late event is placed at its event time, not its receive time.
    tr = PirTrigger()
    tr.connected(BASE)
    feed(tr, "state", BASE + 100, BASE + 100)
    feed(tr, "event", BASE + 200, BASE + 220, seq=1, event="start")      # 20 s late
    feed(tr, "event", BASE + 203, BASE + 223, seq=1, event="stop")
    (c,) = tr.tick(BASE + 240)
    assert round(c.start - BASE) == 200 - PRE_ROLL_SEC, c

    # 6. A start older than STALE_SEC on arrival is dropped; its stop then finds no session.
    tr = PirTrigger()
    tr.connected(BASE)
    feed(tr, "state", BASE + 100, BASE + 100)
    feed(tr, "event", BASE + 200, BASE + 290, seq=1, event="start")      # 90 s late
    feed(tr, "event", BASE + 205, BASE + 295, seq=1, event="stop")
    cn = tr.counts()
    assert tr.session.state == "idle" and cn["stale start (dropped)"] == 1, cn

    # 7. A late event never counts as a reboot; only pir/state going backwards does.
    tr = PirTrigger()
    tr.connected(BASE)
    feed(tr, "state", BASE + 300, BASE + 300)
    feed(tr, "event", BASE + 250, BASE + 301, seq=1, event="start")      # older boot_ms
    assert tr.clock.reboots == 0
    tr.message("state", {"boot_ms": 5000}, BASE + 400)                  # boot_ms restarted
    assert tr.clock.reboots == 1

    # 8. Right after a connect nothing is trusted until a message arrives SETTLE_SEC later.
    tr = PirTrigger()
    tr.connected(BASE + 500)
    feed(tr, "state", BASE + 400, BASE + 501)                            # backlog-sized gap
    feed(tr, "event", BASE + 450, BASE + 501.5, seq=1, event="start")
    assert tr.counts()["unageable start"] == 1 and tr.session.state == "idle"
    feed(tr, "state", BASE + 507, BASE + 507)                            # live
    feed(tr, "event", BASE + 508, BASE + 508, seq=2, event="start")
    assert tr.session.state == "recording" and round(tr.session.t0 - BASE) == 508

    # 9. The Pico going offline closes an open clip at that moment.
    tr = PirTrigger()
    feed(tr, "event", BASE + 10, BASE + 10, seq=1, event="start")
    (c,) = tr.message("status", "offline", BASE + 60)
    assert c.reason == "offline" and round(c.end - BASE) == 60

    # 10. A QoS 1 duplicate is counted once.
    tr = PirTrigger()
    feed(tr, "event", BASE + 10, BASE + 10, seq=1, event="start")
    feed(tr, "event", BASE + 10, BASE + 10.05, seq=1, event="start")
    assert tr.counts()["duplicate"] == 1 and tr.counts()["ignored start"] == 0

    # 11. A persisted reference survives a watcher restart -- unless the Pico rebooted too.
    tr = PirTrigger()
    tr.connected(BASE)
    feed(tr, "state", BASE + 100, BASE + 100)
    saved = tr.clock.to_dict()
    again = PirTrigger(clock=PicoClock.from_dict(saved))
    again.connected(BASE + 300)
    feed(again, "event", BASE + 250, BASE + 300.5, seq=7, event="start")  # backlog, same boot
    assert again.session.state == "recording" and round(again.session.t0 - BASE) == 250
    rebooted = PirTrigger(clock=PicoClock.from_dict(saved))
    rebooted.connected(BASE + 300)
    rebooted.message("event", {"boot_ms": 2000, "seq": 1, "event": "start"}, BASE + 300.5)
    assert rebooted.counts()["unageable start"] == 1

    print("pir_session: all 11 scenarios pass")


if __name__ == "__main__":
    _self_test()
