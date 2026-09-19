#!/usr/bin/env python3
"""Measure how much of a window actually survived into the cloud.

This is the metric guide §16.3c asks for ("report the gap-fill percentage before and
after") and §16.6 sets a threshold on ("fragment continuity < 99.5% over a 10-minute
window"). §10.4 describes the KVS half of it; neither ever defines the equation, so:

    gap-fill % = (seconds of the window recoverable from the cloud) / (window seconds)

**Both sources count, and they must be unioned, not added.** Before outage buffering the
only source was KVS fragments. With it there is a second -- backfilled clips in S3 -- and
the two overlap, because kvssink replays its last `replayDuration` on reconnect while the
buffer also holds that span. Summing would report >100% and look like a triumph.

Usage:
    gap-fill.py --stream cam-02 --start 2026-09-19T14:14:43 --end 2026-09-19T14:19:16
    gap-fill.py --stream cam-02 --last 600          # the window ending now
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import boto3

REGION = "eu-central-1"

# Ambient operator credentials, NOT the adapter's device identity. This is a measurement
# tool an operator runs, not something the device does -- and ListFragments / Query on
# `clips` are not in the device role. Widening that role to make a reporting script work
# would be exactly the kind of quiet privilege creep the role-alias design exists to
# avoid.
def get_session(region=REGION):
    return boto3.Session(region_name=region)


def parse_when(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d.astimezone() if d.tzinfo is None else d


def kvs_intervals(session, stream: str, start: datetime, end: datetime):
    """Fragments KVS actually holds for the window, as (start, end) pairs."""
    kv = session.client("kinesisvideo", region_name=REGION)
    ep = kv.get_data_endpoint(StreamName=stream,
                              APIName="LIST_FRAGMENTS")["DataEndpoint"]
    kam = session.client("kinesis-video-archived-media",
                         endpoint_url=ep, region_name=REGION)
    out, token = [], None
    while True:
        kw = {
            "StreamName": stream,
            "FragmentSelector": {
                # PRODUCER_TIMESTAMP, not SERVER: after an outage and backfill they
                # differ, and the operator means producer time (guide §9.2).
                "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
            },
            "MaxResults": 1000,
        }
        if token:
            kw["NextToken"] = token
        r = kam.list_fragments(**kw)
        for f in r.get("Fragments", []):
            s = f["ProducerTimestamp"]
            out.append((s, s + timedelta(milliseconds=f["FragmentLengthInMilliseconds"])))
        token = r.get("NextToken")
        if not token:
            break
    return out


def clip_intervals(session, camera_id: str, start: datetime, end: datetime):
    """Backfilled clips overlapping the window, from the `clips` registry."""
    from boto3.dynamodb.conditions import Key
    table = session.resource("dynamodb", region_name=REGION).Table("clips")
    out = []
    # Sort key is an ISO8601 string, so a lexicographic range works -- but widen the low
    # end by a day, since a clip that STARTS before the window can still cover part of it.
    lo = (start - timedelta(days=1)).isoformat()
    r = table.query(KeyConditionExpression=Key("cameraId").eq(camera_id)
                    & Key("startTs").between(lo, end.isoformat()))
    for it in r.get("Items", []):
        try:
            s = parse_when(it["startTs"])
            e = s + timedelta(seconds=int(it.get("durationSec") or 0))
        except Exception:
            continue
        if e > start and s < end:
            out.append((s, e))
    return out


def union_seconds(intervals, start: datetime, end: datetime) -> float:
    """Total covered seconds inside [start,end], overlaps counted once."""
    clipped = [(max(s, start), min(e, end)) for s, e in intervals
               if min(e, end) > max(s, start)]
    if not clipped:
        return 0.0
    clipped.sort()
    total, cur_s, cur_e = 0.0, *clipped[0]
    for s, e in clipped[1:]:
        if s > cur_e:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    return total + (cur_e - cur_s).total_seconds()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--last", type=int, help="window of N seconds ending now")
    args = ap.parse_args()

    if args.last:
        end = datetime.now().astimezone()
        start = end - timedelta(seconds=args.last)
    else:
        if not (args.start and args.end):
            ap.error("give --start and --end, or --last")
        start, end = parse_when(args.start), parse_when(args.end)

    window = (end - start).total_seconds()
    session = get_session(REGION)

    kvs = kvs_intervals(session, args.stream, start, end)
    clips = clip_intervals(session, args.stream, start, end)

    kvs_s = union_seconds(kvs, start, end)
    clip_s = union_seconds(clips, start, end)
    both_s = union_seconds(kvs + clips, start, end)

    print(f"window            {start.isoformat()}  ->  {end.isoformat()}")
    print(f"                  {window:.0f}s")
    print()
    print(f"KVS fragments     {len(kvs):>4}   covering {kvs_s:7.1f}s   {kvs_s/window*100:6.2f}%")
    print(f"backfilled clips  {len(clips):>4}   covering {clip_s:7.1f}s   {clip_s/window*100:6.2f}%")
    print(f"{'-'*58}")
    print(f"GAP-FILL (union)               {both_s:7.1f}s   {both_s/window*100:6.2f}%")
    missing = window - both_s
    if missing > 1:
        print(f"lost                           {missing:7.1f}s   {missing/window*100:6.2f}%")


if __name__ == "__main__":
    sys.exit(main())
