#!/usr/bin/env python3
"""Phase 0 observation harness -- log every ONVIF event a camera emits, for hours.

Answers the questions that size detection-driven recording (Camera-Features.md §9) and
that nothing else can establish: which detection topics actually fire on this camera,
whether they latch (State=true ... State=false) or pulse, how many events one person
walking through generates, and the idle false-positive rate overnight.

Two deliberate design choices, both learned the hard way in this project:

* **Heartbeats.** A log with no events is ambiguous -- quiet scene, or dead harness? A
  periodic heartbeat line makes silence provable rather than assumed.
* **Raw XML on parse failure.** An earlier ad-hoc probe swallowed a parse error and
  reported an empty payload for an event that definitely had one. Here, anything the
  parser cannot decode is preserved verbatim so a later pass can recover it.

Usage:
  observe_events.py --camera cam-02 --out events.jsonl        # registry lookup for creds
  observe_events.py --host 192.168.178.67 --user admin --password ***
  observe_events.py --analyse events.jsonl                    # summarise a finished run
"""
import argparse
import asyncio
import json
import signal
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WSDL = "/home/vladimir/MyProjects/VMS/venv-adapter/lib/python3.13/site-packages/onvif/wsdl"
PULL_NS = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
HEARTBEAT_SEC = 300

_stop = False


def _now():
    return datetime.now(timezone.utc).isoformat()


def _emit(fh, rec):
    fh.write(json.dumps(rec) + "\n")
    fh.flush()          # a run that dies at hour 3 must keep hours 1-2


def _parse_message(msg):
    """Best-effort extraction of Topic + Data SimpleItems. Returns (topic, data, raw)."""
    from lxml import etree

    topic, data, raw = None, {}, None
    try:
        topic = str(msg.Topic._value_1)
    except Exception:
        pass

    body = getattr(msg, "Message", None)
    body = getattr(body, "_value_1", body)

    # SimpleItem may arrive as zeep objects (.Name/.Value) or lxml elements (.get()).
    for section in ("Data", "Source"):
        try:
            items = getattr(getattr(body, section), "SimpleItem", []) or []
            for si in items:
                name = getattr(si, "Name", None) or (si.get("Name") if hasattr(si, "get") else None)
                val = getattr(si, "Value", None) or (si.get("Value") if hasattr(si, "get") else None)
                if name is not None:
                    data[f"{section}.{name}"] = val
        except Exception:
            pass

    if not data:                      # never lose an event to a parser gap
        try:
            raw = etree.tostring(body).decode()[:4000]
        except Exception:
            raw = repr(body)[:4000]

    return topic or "?", data, raw


async def _subscribe(cam):
    ev = await cam.create_events_service()
    sub = await ev.CreatePullPointSubscription()
    # The pullpoint XAddr only exists once a subscription has been created.
    cam.xaddrs[PULL_NS] = sub.SubscriptionReference.Address._value_1
    return await cam.create_pullpoint_service()


async def observe(host, port, user, password, out_path, duration_sec):
    from onvif import ONVIFCamera

    cam = ONVIFCamera(host, int(port), user, password, wsdl_dir=WSDL)
    await cam.update_xaddrs()

    fh = open(out_path, "a")
    started = asyncio.get_event_loop().time()
    last_beat = 0.0
    n_events = 0
    _emit(fh, {"t": _now(), "kind": "start", "host": host, "duration_sec": duration_sec})

    pull = None
    while not _stop and (duration_sec <= 0 or
                         asyncio.get_event_loop().time() - started < duration_sec):
        try:
            if pull is None:
                pull = await _subscribe(cam)
                _emit(fh, {"t": _now(), "kind": "subscribed"})

            msgs = await pull.PullMessages({"Timeout": "PT10S", "MessageLimit": 50})
            for m in (getattr(msgs, "NotificationMessage", None) or []):
                topic, data, raw = _parse_message(m)
                rec = {"t": _now(), "kind": "event", "topic": topic, "data": data}
                if raw:
                    rec["raw"] = raw
                _emit(fh, rec)
                n_events += 1
        except Exception as e:
            # A dropped subscription is normal over hours (they expire, the camera
            # reboots, the LAN blips). Record it so a gap in the log is explainable,
            # then rebuild rather than exiting.
            _emit(fh, {"t": _now(), "kind": "resubscribe",
                       "error": f"{type(e).__name__}: {str(e)[:200]}"})
            pull = None
            await asyncio.sleep(5)

        elapsed = asyncio.get_event_loop().time() - started
        if elapsed - last_beat >= HEARTBEAT_SEC:
            last_beat = elapsed
            _emit(fh, {"t": _now(), "kind": "heartbeat",
                       "elapsed_sec": round(elapsed), "events_so_far": n_events})

    _emit(fh, {"t": _now(), "kind": "stop", "events_total": n_events})
    fh.close()
    await cam.close()
    print(f"logged {n_events} events to {out_path}")


def analyse(path):
    # Tolerate a damaged tail. An interrupted run is the *normal* case for this tool,
    # and a hard kill typically leaves either a half-written line or a block of NULs
    # (length extended, data never flushed). Refusing to read 800 good records because
    # of one bad trailing byte would be the wrong failure mode.
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
    events = [r for r in recs if r.get("kind") == "event"]
    beats = [r for r in recs if r.get("kind") == "heartbeat"]
    drops = [r for r in recs if r.get("kind") == "resubscribe"]

    print(f"file:        {path}")
    print(f"records:     {len(recs)}  (events={len(events)} heartbeats={len(beats)} resubscribes={len(drops)})")
    if not recs:
        return
    t0 = datetime.fromisoformat(recs[0]["t"])
    t1 = datetime.fromisoformat(recs[-1]["t"])
    span_h = (t1 - t0).total_seconds() / 3600
    print(f"span:        {t0.isoformat()}  ->  {t1.isoformat()}  ({span_h:.2f} h)")
    # Coverage: heartbeats prove the harness was alive through quiet stretches.
    expected_beats = int((t1 - t0).total_seconds() // HEARTBEAT_SEC)
    print(f"coverage:    {len(beats)}/{expected_beats} expected heartbeats"
          f"{'  <-- GAPS, treat quiet periods with suspicion' if len(beats) < expected_beats else ''}")
    if drops:
        print(f"resubscribes: {len(drops)} (first: {drops[0]['error'][:80]})")

    if not events:
        print("\nNo events fired. If heartbeat coverage is complete, that is a real negative.")
        return

    # Idle rate is the point of this harness, and inter-event gaps cannot see it:
    # a gap only exists *between* two events, so a burst followed by hours of quiet
    # looks identical to steady activity. Report the silence explicitly, measured to
    # the last record (not wall clock) -- after the run ends, nothing was observed.
    t_last_rec = datetime.fromisoformat(recs[-1]["t"])
    t_first_ev = datetime.fromisoformat(events[0]["t"]) if events else None
    t_last_ev = datetime.fromisoformat(events[-1]["t"]) if events else None
    if events:
        lead = (t_first_ev - t0).total_seconds() / 60
        tail = (t_last_rec - t_last_ev).total_seconds() / 60
        print(f"\nquiet periods (observed, heartbeat-backed):")
        print(f"  before first event: {lead:7.1f} min")
        print(f"  after last event:   {tail:7.1f} min   <- idle rate lives here")
        print(f"  last event at:      {t_last_ev.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    print("\nactivity by local hour (rising edges):")
    def _val(e):
        d = e.get("data") or {}
        return d.get("Data.State") or d.get("Data.IsMotion")
    rises = [datetime.fromisoformat(e["t"]) for e in events if _val(e) == "true"]
    hist = Counter(t.astimezone().strftime("%H:00") for t in rises)
    for h in sorted(hist):
        print(f"  {h}  {'#' * min(hist[h], 60):<60} {hist[h]}")

    print("\nper-topic:")
    by_topic = Counter(e["topic"] for e in events)
    for topic, n in by_topic.most_common():
        per_h = n / span_h if span_h else 0
        print(f"  {n:6d}  ({per_h:7.1f}/h)  {topic}")

    # Latch vs pulse: does the topic ever report a false/off state?
    print("\nlatch vs pulse (distinct payload values seen):")
    vals = defaultdict(Counter)
    for e in events:
        for k, v in (e.get("data") or {}).items():
            if k.startswith("Data."):
                vals[e["topic"]][f"{k}={v}"] += 1
    for topic in by_topic:
        seen = vals.get(topic)
        if not seen:
            print(f"  {topic}: no parsed payload (check 'raw' fields)")
            continue
        shape = "latching (on+off seen)" if len(seen) > 1 else "pulse-only (single value)"
        print(f"  {topic}: {shape} -- {dict(seen)}")

    # Burst structure drives the cooldown: how many events per real-world incident?
    print("\ninter-event gaps per topic (drives cooldown sizing):")
    for topic in by_topic:
        ts = [datetime.fromisoformat(e["t"]) for e in events if e["topic"] == topic]
        gaps = sorted((ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1))
        if not gaps:
            print(f"  {topic}: single event")
            continue
        med = gaps[len(gaps) // 2]
        clustered = sum(1 for g in gaps if g < 60)
        print(f"  {topic}: median {med:.1f}s, min {gaps[0]:.1f}s, max {gaps[-1]:.1f}s, "
              f"{clustered}/{len(gaps)} gaps under 60s")
        print(f"      -> at a 60s cooldown this becomes ~{len(gaps) - clustered + 1} clips "
              f"instead of {len(ts)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analyse", metavar="LOGFILE", help="summarise a finished run and exit")
    ap.add_argument("--camera", default="cam-02", help="registry cameraId for credentials")
    ap.add_argument("--host"), ap.add_argument("--user"), ap.add_argument("--password")
    ap.add_argument("--port", default=80)
    ap.add_argument("--out", default="events.jsonl")
    ap.add_argument("--hours", type=float, default=0, help="0 = run until stopped")
    args = ap.parse_args()

    if args.analyse:
        analyse(args.analyse)
        return

    host, user, pw, port = args.host, args.user, args.password, args.port
    if not (host and user and pw):
        from aws_device_creds import get_session
        item = (get_session().resource("dynamodb").Table("cameras")
                .get_item(Key={"cameraId": args.camera}).get("Item"))
        if not item:
            sys.exit(f"camera '{args.camera}' not in the registry, and no --host/--user/--password given")
        host, user, pw = item["onvifHost"], item["onvifUser"], item["onvifPassword"]
        port = item.get("onvifPort", 80)

    def _sig(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    asyncio.run(observe(host, port, user, pw, args.out, int(args.hours * 3600)))


if __name__ == "__main__":
    main()
