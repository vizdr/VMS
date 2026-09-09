#!/usr/bin/env python3
"""Replay a recorded observe_events.py log through event_watcher's ClipGate.

Validates the rising-edge + cooldown logic against real captured detections rather than
requiring someone to stand in front of a camera. Prints how many clips each mode would
have produced over the recorded window, which is also the number that drives cost.

  replay-gate.py measurements/events-cam-02-2026-09-08-overnight.jsonl [--cooldown 60]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from event_watcher import ClipGate, MODE_TOPICS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("--cooldown", type=int, default=60)
    args = ap.parse_args()

    recs = []
    for line in open(args.logfile, errors="replace"):
        line = line.strip("\x00 \t\r\n")
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    events = [r for r in recs if r.get("kind") == "event"]
    if not events:
        sys.exit("no events in log")

    t0 = datetime.fromisoformat(events[0]["t"])
    span_h = (datetime.fromisoformat(events[-1]["t"]) - t0).total_seconds() / 3600

    print(f"{args.logfile}\n{len(events)} events over {span_h:.2f} h, "
          f"cooldown={args.cooldown}s\n")
    print(f"  {'mode':<11}{'topic messages':>15}{'rising edges':>14}"
          f"{'CLIPS':>8}{'suppressed':>12}")
    print("  " + "-" * 60)

    for mode, topic in MODE_TOPICS.items():
        gate = ClipGate(args.cooldown)
        msgs = rises = clips = supp = 0
        for e in events:
            if e["topic"] != topic:
                continue
            msgs += 1
            d = e.get("data") or {}
            raw = d.get("Data.State") or d.get("Data.IsMotion")
            if raw is None:
                continue
            state = str(raw).lower() == "true"
            if state:
                rises += 1 if not gate.asserted else 0
            t = (datetime.fromisoformat(e["t"]) - t0).total_seconds()
            fire, reason = gate.update(state, t)
            if fire:
                clips += 1
            elif reason.startswith("cooldown"):
                supp += 1
        print(f"  {mode:<11}{msgs:>15}{rises:>14}{clips:>8}{supp:>12}")

    print(f"\n  clips/hour at this cooldown drives the duty cycle in COSTS-1.4 §7.2")


if __name__ == "__main__":
    main()
