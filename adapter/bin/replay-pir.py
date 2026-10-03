#!/usr/bin/env python3
"""Replay a recorded observe_pir.py log through the PIR session logic (adapter/pir_session.py).

The same idea as replay-gate.py for the ONVIF watcher: the logic the planned kvs-pir-watcher
will run, fed real captured traffic instead of someone waving at the sensor. For each
POST_SEC it prints how many clips the log would have produced, how much footage that is
(the duty cycle, which drives storage, flash wear and upload volume -- PIR-MQTT-VMS-PI4.md
§2.4), and what was ignored, dropped as stale, or couldn't be aged.

  replay-pir.py measurements/pir-<date>-<label>.jsonl [--post 0,5,10,30] [--max 180]
                [--stale 60] [--clips]

The replay assumes the ring buffer was recording throughout. Retained messages are skipped,
exactly as the watcher must skip them.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from observe_pir import load_log
from pir_session import MAX_SEC, POST_SEC, STALE_SEC, PirTrigger

KINDS = (("/pir/event", "event"), ("/pir/state", "state"), ("/status", "status"))


def topic_kind(topic):
    return next((kind for suffix, kind in KINDS if topic.endswith(suffix)), None)


def replay(recs, post_sec, max_sec, stale_sec):
    """Connects are deliberately NOT passed to trig.connected(). The settle rule exists
    because the watcher's persistent session can replay broker backlog right after a
    connect; observe_pir.py logs through a clean session, so its logs hold no backlog, and
    applying the rule would drop live events that merely followed a connect (it dropped
    both motions in the first real capture)."""
    trig = PirTrigger(post_sec=post_sec, max_sec=max_sec, stale_sec=stale_sec)
    clips = []
    for r in recs:
        if r.get("kind") == "msg" and not r.get("retain") and topic_kind(r["topic"]):
            clips += trig.message(topic_kind(r["topic"]), r.get("payload"), r["_ts"])
        else:
            clips += trig.tick(r["_ts"])
    clips += trig.finish(recs[-1]["_ts"])
    return clips, trig.counts()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile")
    ap.add_argument("--post", default=f"0,{POST_SEC},10,30",
                    help="comma-separated POST_SEC values to compare")
    ap.add_argument("--max", type=int, default=MAX_SEC, help="MAX_SEC, the clip cap")
    ap.add_argument("--stale", type=int, default=STALE_SEC, help="STALE_SEC")
    ap.add_argument("--clips", action="store_true",
                    help=f"list every clip for POST_SEC={POST_SEC} (or the first --post value)")
    args = ap.parse_args()

    recs = load_log(args.logfile)
    if not recs:
        sys.exit("empty log")
    span = recs[-1]["_ts"] - recs[0]["_ts"]
    n_ev = sum(1 for r in recs if r.get("kind") == "msg" and r["topic"].endswith("/pir/event")
               and not r.get("retain"))
    posts = [int(p) for p in args.post.split(",")]
    print(f"{args.logfile}\n{n_ev} event messages over {span / 3600:.2f} h; "
          f"MAX_SEC {args.max} s, STALE_SEC {args.stale} s, pre-roll 12 s\n")

    keys = ("ignored start", "stale start (dropped)", "unageable start", "duplicate")
    print(f"  {'POST_SEC':>8} {'clips':>6} {'minutes':>8} {'duty':>7}   "
          + "  ".join(f"{k.split(' (')[0]:>14}" for k in keys))
    print("  " + "-" * 104)
    results = {}
    for post in posts:
        clips, counts = replay(recs, post, args.max, args.stale)
        results[post] = (clips, counts)
        total = sum(c.duration for c in clips)
        duty = f"{100 * total / span:6.2f}%" if span >= 3600 else "   n/a"
        print(f"  {post:>8} {len(clips):>6} {total / 60:>8.1f} {duty:>7}   "
              + "  ".join(f"{counts.get(k, 0):>14}" for k in keys))
    if span < 3600:
        print("\n  duty cycle n/a: the log spans under an hour, where the 12 s pre-roll dominates")

    show = POST_SEC if POST_SEC in results else posts[0]
    clips, counts = results[show]
    other = {k: v for k, v in counts.items() if k not in keys}
    if other:
        print(f"\n  at POST_SEC {show} s also: "
              + ", ".join(f"{k} {v}" for k, v in sorted(other.items())))
    if args.clips:
        print(f"\n  clips at POST_SEC {show} s (local time):")
        for c in clips:
            t = datetime.fromtimestamp(c.start).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            print(f"    {t}  {c.duration:6.1f} s  seq {c.seq}  closed by {c.reason}")
    print("\n  clip minutes are summed per clip; adjacent clips can share pre-roll footage")


if __name__ == "__main__":
    main()
