#!/usr/bin/env python3
"""Phase 2 observation harness for the PIR sensor -- log everything the Pico 2 W publishes
on the local MQTT broker, for hours, then summarise it.

Answers what PIR-MQTT-VMS-PI4.md needs measured before any recording logic exists (Phase 2,
Appendix A.5): how often motion starts, how long it lasts, how long the gaps between a stop
and the next start are, what fires overnight with nobody there, and how late messages reach
the Pi (which checks STALE_SEC, section 3.5).

Same shape as observe_events.py, for the same reasons:

* **Heartbeats.** A log with no events is ambiguous -- quiet hallway, or dead harness? A
  heartbeat every 5 minutes makes silence provable. The Pico's own pir/state heartbeat
  (every 30 s) is logged too, so a quiet stretch can be told apart from an absent Pico.
* **Raw on parse failure.** A payload that is not the expected JSON is kept verbatim.
* **Bounded runs.** --hours, started by hand, never as an enabled unit: onvif-observe was left
  enabled after its measurement and kept appending to its git-tracked log at every boot.

**Clean MQTT session, deliberately** -- unlike the planned watcher, which needs a persistent
one. A persistent session would make the broker queue days of the Pico's events between runs
and replay them into the next log stamped with the wrong receive time. Instead every
disconnect is recorded, so a gap in the log is explainable.

Connection details come from /etc/adapter/adapter.env (MQTT_HOST, MQTT_PORT, MQTT_USER,
MQTT_PASSWORD_FILE), read only when observing, so --analyse works anywhere. Command-line
options override them.

Usage:
  observe_pir.py --out measurements/pir-2026-10-04-afternoon.jsonl --hours 5
  observe_pir.py --analyse measurements/pir-2026-10-04-afternoon.jsonl [--quiet 22:00-07:00]
  observe_pir.py --tail          # watch events live, e.g. while setting the hold-time pot
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HEARTBEAT_SEC = 300
PICO_HEARTBEAT_SEC = 30           # pir/state cadence promised by the Pico (section 3.2)
PICO_SILENT_SEC = 75              # 2.5 heartbeats without pir/state = the Pico was gone
STALE_SEC = 60                    # section 3.5: older events are dropped by the watcher
PRE_ROLL_SEC = 12
MAX_SEC = 180
POST_SEC_CANDIDATES = (0, 5, 10, 30)
GAP_THRESHOLDS = (5, 10, 15, 30, 60)

_stop = False
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _emit(fh, rec):
    if fh is None:                 # --tail without --out: print only, keep nothing
        return
    with _lock:                    # paho calls back from its own thread
        fh.write(json.dumps(rec) + "\n")
        fh.flush()                 # a run that dies at hour 3 must keep hours 1-2


def _tail_line(rec):
    """One human-readable line per message, for tuning the sensor live (--tail)."""
    when = datetime.fromisoformat(rec["t"]).astimezone().strftime("%H:%M:%S")
    topic, p = rec["topic"].split("/", 2)[-1], rec.get("payload")
    if topic == "pir/event" and isinstance(p, dict):
        extra = f"  lasted {p['duration_ms'] / 1000:.2f} s" if p.get("duration_ms") is not None else ""
        return f"{when}  motion {p.get('event', '?'):5} #{p.get('seq')}{extra}"
    if topic == "pir/state" and isinstance(p, dict):
        return None                # the 30 s heartbeat would drown the events
    return f"{when}  {topic} = {p if 'payload' in rec else '<binary>'}" + (" (retained)" if rec["retain"] else "")


def _ntp_synced():
    """The Pi has no RTC: until NTP syncs, its clock is whatever it last saved."""
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "yes"
    except Exception:
        return None


# --- observing -----------------------------------------------------------------------------

def _settings(args):
    """CLI options first, then adapter.env. No literal defaults (CLAUDE.md, config).

    config is imported only for a value the command line didn't give: importing it reads the
    AWS identity keys too, which a run with every option supplied has no reason to need.
    """
    def setting(key, given):
        if given:
            return given
        import config
        return config.get(key)

    host = setting("MQTT_HOST", args.host)
    port = int(setting("MQTT_PORT", args.port))
    user = setting("MQTT_USER", args.user)
    pw_file = setting("MQTT_PASSWORD_FILE", args.password_file)
    try:
        password = Path(pw_file).read_text().strip()
    except OSError as e:
        sys.exit(f"cannot read the MQTT password file {pw_file}: {e.strerror}\n"
                 f"create it as shown in PIR-MQTT-VMS-PI4.md Appendix B.7")
    return host, port, user, password


def observe(args):
    import paho.mqtt.client as mqtt

    host, port, user, password = _settings(args)
    topic = f"home/{args.device}/#"
    duration_sec = int(args.hours * 3600)
    state = {"connected": False, "fatal": None, "msgs": 0, "events": 0}

    out = Path(args.out) if args.out else None
    fh = None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        fh = open(out, "a")

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            _emit(fh, {"t": _now(), "kind": "connect_failed", "reason": str(reason_code)})
            # Wrong credentials won't fix themselves; anything else (broker restarting) may.
            if "authori" in str(reason_code).lower() or "password" in str(reason_code).lower():
                state["fatal"] = str(reason_code)
            return
        state["connected"] = True
        client.subscribe(topic, qos=1)
        _emit(fh, {"t": _now(), "kind": "connected", "topic": topic})

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if state["connected"]:
            _emit(fh, {"t": _now(), "kind": "disconnected", "reason": str(reason_code)})
        state["connected"] = False

    def on_message(client, userdata, msg):
        rec = {"t": _now(), "kind": "msg", "topic": msg.topic, "qos": msg.qos,
               "retain": bool(msg.retain)}
        try:
            text = msg.payload.decode("utf-8")
            try:
                rec["payload"] = json.loads(text)
            except ValueError:
                rec["payload"] = text             # status "online"/"offline" is plain text
        except UnicodeDecodeError:
            rec["raw"] = msg.payload.hex()[:8000]  # never lose a message to a decoder
        state["msgs"] += 1
        if msg.topic.endswith("/pir/event"):
            state["events"] += 1
        _emit(fh, rec)
        if args.tail and (line := _tail_line(rec)):
            print(line, flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"vms-pir-observe-{os.getpid()}", clean_session=True)
    client.username_pw_set(user, password)
    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    started = time.monotonic()
    _emit(fh, {"t": _now(), "kind": "start", "host": host, "port": port, "user": user,
               "topic": topic, "duration_sec": duration_sec, "ntp_synced": _ntp_synced()})
    client.connect_async(host, port, keepalive=30)
    client.loop_start()

    last_beat = started
    while not _stop and (duration_sec <= 0 or time.monotonic() - started < duration_sec):
        if state["fatal"]:
            break
        time.sleep(1)
        if time.monotonic() - last_beat >= HEARTBEAT_SEC:
            last_beat = time.monotonic()
            _emit(fh, {"t": _now(), "kind": "heartbeat",
                       "elapsed_sec": round(last_beat - started), "connected": state["connected"],
                       "msgs_so_far": state["msgs"], "events_so_far": state["events"],
                       "ntp_synced": _ntp_synced()})

    client.loop_stop()
    client.disconnect()
    _emit(fh, {"t": _now(), "kind": "stop", "msgs_total": state["msgs"],
               "events_total": state["events"], "fatal": state["fatal"]})
    if fh:
        fh.close()
    if state["fatal"]:
        sys.exit(f"broker refused the login ({state['fatal']}) -- check MQTT_USER and the "
                 f"password file")
    if out:
        print(f"logged {state['msgs']} messages ({state['events']} events) to {out}")


# --- analysing -----------------------------------------------------------------------------

def load_log(path):
    # Tolerate a damaged tail: an interrupted run is the normal case for this tool, and a
    # hard kill leaves a half-written line or a block of NULs (observe_events.py, same rule).
    recs, bad = [], 0
    for line in open(path, errors="replace"):
        line = line.strip("\x00 \t\r\n")
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        print(f"note: skipped {bad} unparseable line(s) -- expected after an interrupted run\n")
    for r in recs:
        r["_ts"] = datetime.fromisoformat(r["t"]).timestamp()
    return recs


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))]


def _fmt_dist(xs, unit="s", scale=1.0):
    if not xs:
        return "none"
    v = [x * scale for x in xs]
    return (f"n={len(v)}  min {min(v):.1f}{unit}  p10 {_pct(v, 10):.1f}  median {_pct(v, 50):.1f}"
            f"  p90 {_pct(v, 90):.1f}  max {max(v):.1f}{unit}")


def _in_window(ts, window):
    lt = datetime.fromtimestamp(ts).astimezone()
    m = lt.hour * 60 + lt.minute
    a, b = window
    return a <= m < b if a < b else (m >= a or m < b)


def _parse_window(s):
    a, b = s.split("-")
    to_min = lambda hm: int(hm.split(":")[0]) * 60 + int(hm.split(":")[1])
    return to_min(a), to_min(b)


def _sessions(events, offline_ts, post_sec):
    """First duty-cycle estimate: a simplified ClipSession (section 3.3). Phase 4's replay
    of the real ClipSession supersedes these numbers."""
    clips, ignored, sess = [], 0, None
    offline = sorted(offline_ts)

    def close(at):
        clips.append(at - sess["t0"] + PRE_ROLL_SEC)

    for e in events:
        t = e["time"]
        if sess:
            cap = sess["t0"] + MAX_SEC
            end = sess["end"] if sess["end"] is not None else cap
            off = next((o for o in offline if sess["t0"] <= o < min(t, end)), None)
            if off is not None:
                close(off); sess = None
            elif t >= min(end, cap):
                close(min(end, cap)); sess = None
        if sess is None:
            if e["event"] == "start":
                sess = {"t0": t, "seq": e["seq"], "end": None}
            continue
        if e["event"] == "stop" and e["seq"] == sess["seq"] and sess["end"] is None:
            sess["end"] = min(t + post_sec, sess["t0"] + MAX_SEC)
        elif e["event"] == "start":
            ignored += 1
    if sess:
        end = sess["end"] if sess["end"] is not None else sess["t0"] + MAX_SEC
        close(min(end, sess["t0"] + MAX_SEC))
    return clips, ignored


def analyse(path, quiet):
    recs = load_log(path)
    print(f"file:        {path}")
    if not recs:
        print("empty")
        return
    kinds = Counter(r.get("kind") for r in recs)
    msgs = [r for r in recs if r.get("kind") == "msg"]
    t0, t1 = recs[0]["_ts"], recs[-1]["_ts"]
    span = t1 - t0
    loc = lambda ts: datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"records:     {len(recs)}  ({', '.join(f'{k}={n}' for k, n in sorted(kinds.items()))})")
    print(f"span:        {loc(t0)}  ->  {loc(t1)}  ({span / 3600:.2f} h)")

    # --- is the log trustworthy? -------------------------------------------------------------
    beats = kinds.get("heartbeat", 0)
    expected = int(span // HEARTBEAT_SEC)
    print(f"coverage:    {beats}/{expected} expected harness heartbeats"
          f"{'  <-- GAPS: treat quiet periods with suspicion' if beats < expected else ''}")
    if any(r.get("ntp_synced") is False for r in recs if "ntp_synced" in r):
        print("clock:       WARNING -- the Pi's clock was not NTP-synced for part of the run")
    down, t_down = 0.0, None
    for r in recs:
        if r.get("kind") == "disconnected":
            t_down = r["_ts"]
        elif r.get("kind") == "connected" and t_down is not None:
            down += r["_ts"] - t_down
            t_down = None
    if kinds.get("disconnected"):
        print(f"broker:      {kinds['disconnected']} disconnect(s), {down:.0f} s offline in total")
    if kinds.get("connect_failed"):
        print(f"broker:      {kinds['connect_failed']} failed connect(s)")

    live = [m for m in msgs if not m.get("retain")]      # retained = replay at subscribe
    by_suffix = lambda s: [m for m in live if m["topic"].endswith(s)]
    states, ev_msgs = by_suffix("/pir/state"), by_suffix("/pir/event")
    statuses, cmds = by_suffix("/status"), by_suffix("/pir/cmd/retrigger")
    unparsed = [m for m in msgs if "raw" in m or (m["topic"].endswith(("/pir/event", "/pir/state"))
                                                   and not isinstance(m.get("payload"), dict))]
    print(f"messages:    {len(msgs)} ({len(msgs) - len(live)} retained replays); "
          f"events {len(ev_msgs)}, state {len(states)}, status {len(statuses)}, "
          f"commands {len(cmds)}, unparsed {len(unparsed)}")
    if not live:
        print("\nNo live messages. Is the Pico running Phase 1 firmware and connected?")
        return

    # --- Pico boots and the clock reference (section 3.5) -----------------------------------
    # Reboots are detected from pir/state ALONE. The Pico publishes it in order and stamps it
    # at publish time, so its boot_ms only goes backwards when the Pico restarts. An event's
    # boot_ms may legitimately be older than the latest state's -- it waited in the Pico's
    # queue -- and treating that as a reboot misread every late event (found testing this
    # tool against a synthetic log). Events belong to the boot whose first state preceded them:
    # after a reconnect the Pico publishes state before draining its queue (section 3.2).
    stamped = [m for m in live if isinstance(m.get("payload"), dict) and "boot_ms" in m["payload"]]
    boot_starts, last_bm = [], None
    for m in stamped:
        if m["topic"].endswith("/pir/state"):
            bm = m["payload"]["boot_ms"]
            if last_bm is None or bm < last_bm - 1000:
                boot_starts.append(m["_ts"])
            last_bm = bm
    for m in stamped:
        m["_boot"] = max(0, bisect_right(boot_starts, m["_ts"]) - 1)
        m["_d"] = m["_ts"] * 1000 - m["payload"]["boot_ms"]
    boots = Counter(m["_boot"] for m in stamped)
    offset = {b: min(m["_d"] for m in stamped if m["_boot"] == b) for b in boots}
    lat = [(m["_d"] - offset[m["_boot"]]) / 1000 for m in stamped]
    print(f"\nPico boots seen: {max(1, len(boot_starts))}"
          + (f" (restarts at {', '.join(loc(t) for t in boot_starts[1:])})" if len(boot_starts) > 1 else ""))
    print(f"delivery lateness (receive time - event time, from boot_ms): {_fmt_dist(lat)}")
    stale = sum(1 for x in lat if x > STALE_SEC)
    print(f"  messages older than STALE_SEC ({STALE_SEC} s) on arrival: {stale}"
          f"{'  <-- the watcher would drop these starts' if stale else ''}")
    for b in sorted(boots):
        seg = [m for m in stamped if m["_boot"] == b]
        if seg[-1]["_ts"] - seg[0]["_ts"] >= 3600:
            first = min(m["_d"] for m in seg[:max(1, len(seg) // 10)])
            last = min(m["_d"] for m in seg[-max(1, len(seg) // 10):])
            hours = (seg[-1]["_ts"] - seg[0]["_ts"]) / 3600
            print(f"  clock drift, boot {b}: {(last - first) / hours:+.1f} ms/h "
                  f"(negative: the Pico's crystal runs fast against the Pi's NTP clock)")

    # --- is the Pico alive? -----------------------------------------------------------------
    st_ts = [m["_ts"] for m in states]
    silent = [(a, b - a) for a, b in zip(st_ts, st_ts[1:]) if b - a > PICO_SILENT_SEC]
    if st_ts:
        print(f"\nPico heartbeat (pir/state): {len(states)} messages, "
              f"{len(silent)} silence(s) over {PICO_SILENT_SEC} s"
              + (f", longest {max(s for _, s in silent):.0f} s at {loc(max(silent, key=lambda x: x[1])[0])}"
                 if silent else ""))
    offline_ts = [m["_ts"] for m in statuses if m.get("payload") == "offline"]
    for m in statuses:
        print(f"  status {m.get('payload')!s:8} at {loc(m['_ts'])}")
    warming = sum(1 for m in states if isinstance(m.get("payload"), dict)
                  and m["payload"].get("warming_up"))
    if warming:
        print(f"  warm-up states: {warming}")
    dropped = [m["payload"].get("dropped") for m in states
               if isinstance(m.get("payload"), dict) and m["payload"].get("dropped")]
    if dropped:
        print(f"  Pico queue drops reported: up to {max(dropped)}  <-- events lost on the Pico")

    # --- events ------------------------------------------------------------------------------
    seen, events, dups = set(), [], 0
    for m in ev_msgs:
        p = m.get("payload")
        if not isinstance(p, dict) or p.get("event") not in ("start", "stop"):
            continue
        key = (m.get("_boot", 0), p.get("seq"), p["event"])
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        b = m.get("_boot")
        events.append({"time": (offset[b] + p["boot_ms"]) / 1000 if b is not None else m["_ts"],
                       "recv": m["_ts"], "event": p["event"], "seq": p.get("seq"),
                       "boot": m.get("_boot", 0), "duration_ms": p.get("duration_ms")})
    events.sort(key=lambda e: e["time"])
    print(f"\nevents: {len(events)} unique ({dups} QoS 1 duplicates removed)")

    # Retrigger mode by time: the firmware default is retriggerable until a command says not.
    changes = [(m["_ts"], str(m.get("payload"))) for m in cmds]
    def mode_at(ts):
        mode = "retriggerable"
        for t, v in changes:
            if t <= ts:
                mode = "single-trigger" if v == "0" else "retriggerable"
        return mode
    for t, v in changes:
        print(f"  mode command '{v}' at {loc(t)}")

    starts = [e for e in events if e["event"] == "start"]
    stops = {(e["boot"], e["seq"]): e for e in events if e["event"] == "stop"}
    unpaired = [s for s in starts if (s["boot"], s["seq"]) not in stops]
    gaps_in_seq = 0
    for b in boots or {0: 0}:
        seqs = sorted({s["seq"] for s in starts if s["boot"] == b and isinstance(s["seq"], int)})
        gaps_in_seq += sum(y - x - 1 for x, y in zip(seqs, seqs[1:]) if y - x > 1)
    print(f"  starts {len(starts)}, stops {len(stops)}, starts without a stop {len(unpaired)}, "
          f"missing seq numbers {gaps_in_seq}"
          f"{'  <-- events were lost' if gaps_in_seq else ''}")
    if not starts:
        print("\nNo motion recorded. If the heartbeats are complete, that is a real negative.")
        return

    print("\nstarts by local hour:")
    hist = Counter(datetime.fromtimestamp(s["time"]).astimezone().strftime("%H:00") for s in starts)
    for h in sorted(hist):
        print(f"  {h}  {'#' * min(hist[h], 60):<60} {hist[h]}")

    for mode in ("retriggerable", "single-trigger"):
        ms = [s for s in starts if mode_at(s["recv"]) == mode]
        if not ms:
            continue
        durs = [stops[(s["boot"], s["seq"])]["duration_ms"] / 1000 for s in ms
                if (s["boot"], s["seq"]) in stops and stops[(s["boot"], s["seq"])]["duration_ms"]]
        print(f"\n[{mode}]  {len(ms)} starts")
        print(f"  motion duration: {_fmt_dist(durs)}")
        if durs:
            print(f"  -> T_hold is at most {min(durs):.1f} s (the shortest motion); confirm with "
                  f"a single brief wave (Appendix A.5 step 1)")
        gaps = []
        for s in ms:
            stop = stops.get((s["boot"], s["seq"]))
            nxt = next((n for n in starts if n["time"] > s["time"] and n["boot"] == s["boot"]), None)
            if stop and nxt and nxt["time"] >= stop["time"]:
                gaps.append(nxt["time"] - stop["time"])
        print(f"  stop -> next start: {_fmt_dist(gaps)}")
        if gaps:
            print("  " + "  ".join(f"<{th}s: {sum(1 for g in gaps if g < th)}" for th in GAP_THRESHOLDS)
                  + "   (Appendix A.5 step 3: many gaps just above a POST_SEC means brief returns)")

    # --- the quiet window: false-positive candidates ----------------------------------------
    win = _parse_window(quiet)
    q_starts = [s for s in starts if _in_window(s["time"], win)]
    q_obs = sum(1 for k in range(int(t0), int(t1), 60) if _in_window(k, win)) / 60
    print(f"\nquiet window {quiet}: {q_obs:.1f} h observed, {len(q_starts)} start(s)"
          + (f" = {len(q_starts) / q_obs:.2f}/h" if q_obs else ""))
    clusters, cur = [], []
    for s in q_starts:
        if cur and s["time"] - cur[-1]["time"] > 300:
            clusters.append(cur); cur = []
        cur.append(s)
    if cur:
        clusters.append(cur)
    for c in clusters:
        print(f"  {loc(c[0]['time'])}  {len(c)} start(s) over {c[-1]['time'] - c[0]['time']:.0f} s"
              f"   <- was anyone there? if not, a false positive")

    # --- duty cycle, first estimate ------------------------------------------------------------
    if span < 3600:
        print("\nduty cycle: not estimated -- the span is under an hour, and the 12 s pre-roll "
              "alone would dominate it")
        return
    print(f"\nduty cycle, first estimate (pre-roll {PRE_ROLL_SEC} s, MAX_SEC {MAX_SEC} s, "
          f"over the {span / 3600:.2f} h span):")
    for post in POST_SEC_CANDIDATES:
        clips, ignored = _sessions(events, offline_ts, post)
        total = sum(clips)
        print(f"  POST_SEC {post:3d} s: {len(clips):4d} clips, {total / 60:7.1f} min recorded, "
              f"duty {100 * total / span:5.2f} %, {ignored} start(s) ignored while recording")
    print("  (a simplified session model; Phase 4 replays this log through the real ClipSession)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyse", metavar="LOGFILE", help="summarise a finished run and exit")
    ap.add_argument("--quiet", default="22:00-07:00",
                    help="local window in which motion is a false-positive candidate")
    ap.add_argument("--out", help="JSONL file to append to (created if missing)")
    ap.add_argument("--tail", action="store_true",
                    help="print events live as they arrive (to tune the sensor); --out optional")
    ap.add_argument("--hours", type=float, default=0, help="0 = run until stopped")
    ap.add_argument("--device", default="pico2w-01", help="topics are home/<device>/...")
    ap.add_argument("--host"), ap.add_argument("--port"), ap.add_argument("--user")
    ap.add_argument("--password-file")
    args = ap.parse_args()

    if args.analyse:
        analyse(args.analyse, args.quiet)
        return
    if not args.out and not args.tail:
        ap.error("--out is required when observing (or use --tail to only watch)")

    def _sig(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    observe(args)


if __name__ == "__main__":
    main()
