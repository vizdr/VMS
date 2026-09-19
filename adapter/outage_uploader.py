#!/usr/bin/env python3
"""Backfill buffered outage footage to S3 and register it as evidence clips.

Runs alongside outage_buffer.py, which captures segments to
/mnt/vms-buffer/outage/<id>/ and marks the journal `pending-upload`. This process
merges, uploads and registers them, then deletes the local copies.

The rule that makes this safe, carried over verbatim from guide 17/M3:
**delete only after a confirmed 200.** If the WAN is down the upload raises, the files
stay on the stick, and the next pass retries them oldest-first.

Buffered clips land in the SAME place as evidence clips -- `clips/<cameraId>/...` with a
row in the `clips` table -- so list/play/tier/delete and the S3 lifecycle rule all work
on them unchanged. The key shape is not cosmetic: delete_clip.py derives the camera from
`key.split("/")[1]`.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import camera_control
from outage_buffer import (BUFFER_ROOT, OUTAGE_DIR, buffer_ready, log, segment_start,
                           utc_now)

REGION = "eu-central-1"
BUCKET = "vms-demo-evidence-596633517506"
SCAN_SEC = 30

# At recovery kvssink is flushing its own backlog up the same uplink. Piling ~1 GB of
# chunks on top can cause a second outage and lose that backlog -- so wait, then upload
# strictly sequentially.
SETTLE_AFTER_RECOVERY_SEC = 60

# chunk = clamp(outageLen/6, 10min, 2h). One rule for every limit: it yields <= ~6 rows
# for short outages and lands on exactly 2h for the 12h and 24h settings. list_clips.py
# returns only the 50 newest clips AND does one s3.head_object per clip, so unbounded row
# counts would bury real evidence clips and add dozens of synchronous HEADs per page load.
CHUNK_MIN_SEC = 600
CHUNK_MAX_SEC = 7200
CHUNK_DIVISOR = 6

AUDIO_BITRATE_BY_RATE = {8000: "32k", 16000: "32k"}   # see the AAC per-frame clamp below
DEFAULT_AUDIO_BITRATE = "48k"


def ffprobe(path: Path) -> dict | None:
    """Validate a segment and return its stream layout, or None if unreadable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout)
        if not d.get("streams"):
            return None
        return d
    except Exception:
        return None


def track_signature(probe: dict) -> tuple:
    """What must match for two segments to be concatenatable with -c:v copy.

    A track-layout change mid-outage is not hypothetical: toggling audioEnabled applies
    on the producer's next start, which can land inside an outage.
    """
    sig = []
    for s in probe["streams"]:
        if s["codec_type"] == "video":
            sig.append(("v", s.get("codec_name"), s.get("width"), s.get("height")))
        elif s["codec_type"] == "audio":
            sig.append(("a", s.get("codec_name"), s.get("sample_rate"), s.get("channels")))
    return tuple(sorted(sig))


def audio_rate(probe: dict) -> int | None:
    for s in probe["streams"]:
        if s["codec_type"] == "audio":
            try:
                return int(s.get("sample_rate"))
            except (TypeError, ValueError):
                return None
    return None


def partition_runs(segs: list[Path]) -> list[list[tuple[Path, dict]]]:
    """Split into maximal runs that probe cleanly and share a track layout.

    A bad segment must never cost the whole outage, and a gap between runs is
    information -- it becomes a separate clip rather than being silently bridged.
    """
    runs, cur, cur_sig = [], [], None
    for p in segs:
        probe = ffprobe(p)
        if probe is None:
            log(f"    quarantine (unreadable): {p.name}")
            if cur:
                runs.append(cur)
                cur, cur_sig = [], None
            continue
        sig = track_signature(probe)
        if cur and sig != cur_sig:
            log(f"    track layout changed at {p.name} -- starting a new clip")
            runs.append(cur)
            cur = []
        cur_sig = sig
        cur.append((p, probe))
    if cur:
        runs.append(cur)
    return runs


def chunk_length(total_sec: float) -> float:
    return max(CHUNK_MIN_SEC, min(CHUNK_MAX_SEC, total_sec / CHUNK_DIVISOR))


def seg_duration(probe: dict) -> float:
    try:
        return float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def merge(chunk: list[tuple[Path, dict]], out: Path) -> bool:
    """Concat with ffmpeg, transcoding audio to AAC.

    `-c:a aac` is mandatory, not a preference. MediaMTX writes LPCM as ISO 23003-5 `ipcm`
    and G.711 as ulaw/alaw sample entries, and no browser decodes either inside MP4 --
    `-c copy` would give a clip that plays in ffplay and is silent in the cloud client,
    exactly the trap CLAUDE.md warns about.

    `+faststart` because the clip is played from a presigned URL in a <video> tag;
    `+genpts` because each segment's internal timestamps do not start at zero.
    """
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p}'\n" for p, _ in chunk))
    rate = audio_rate(chunk[0][1])
    # At 8 kHz, 1024-sample AAC frames are 128 ms, and 64 kbps needs 8192 bits per frame
    # against a 6144 ceiling -- ffmpeg clamps and warns. Same arithmetic that sets the
    # sample rate in guide 18.3.
    abr = AUDIO_BITRATE_BY_RATE.get(rate, DEFAULT_AUDIO_BITRATE)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(listing), "-fflags", "+genpts", "-max_interleave_delta", "0",
           "-c:v", "copy"]
    cmd += (["-c:a", "aac", "-b:a", abr] if rate else ["-an"])
    cmd += ["-movflags", "+faststart", "-y", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    listing.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        log(f"    merge FAILED: {r.stderr.strip()[:200]}")
        return False
    return True


def upload_and_register(session, camera_id: str, merged: Path,
                        start: datetime, duration: float, labels: list[str]) -> bool:
    """Upload, confirm, then register. Returns True only if all three succeeded."""
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from botocore.exceptions import ClientError

    key = (f"clips/{camera_id}/{start.strftime('%Y/%m/%d')}/"
           f"{start.strftime('%H%M%S')}-outage.mp4")
    cfg = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3})
    s3 = session.client("s3", region_name=REGION,
                        endpoint_url=f"https://s3.{REGION}.amazonaws.com", config=cfg)

    size_mb = merged.stat().st_size / 1024 ** 2
    log(f"    uploading {merged.name} ({size_mb:.0f} MB) -> s3://{BUCKET}/{key}")
    try:
        # Managed transfer: multipart above 8 MB with retries, rather than one enormous
        # put_object. max_concurrency=1 keeps this from competing with kvssink's backlog.
        s3.upload_file(str(merged), BUCKET, key,
                       ExtraArgs={"ContentType": "video/mp4"},
                       Config=TransferConfig(max_concurrency=1, multipart_threshold=8 * 1024 ** 2))
        # The "confirmed 200" the delete rule depends on.
        head = s3.head_object(Bucket=BUCKET, Key=key)
        if head["ContentLength"] != merged.stat().st_size:
            log("    size mismatch after upload -- keeping local copy")
            return False
    except Exception as e:
        log(f"    upload failed ({type(e).__name__}: {str(e)[:120]}) -- keeping local copy")
        return False

    try:
        table = session.resource("dynamodb", region_name=REGION).Table("clips")
        table.put_item(
            Item={
                "cameraId": camera_id,
                "startTs": start.isoformat(),
                "s3Key": key,
                "labels": labels,
                "durationSec": int(round(duration)),
            },
            # clip_to_s3.py and record_clip.py both put_item unconditionally; do not copy
            # that here. A key collision with a real evidence clip must fail loudly rather
            # than overwrite it.
            ConditionExpression="attribute_not_exists(startTs)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log(f"    a clip already exists at {start.isoformat()} -- not overwriting")
            return True          # object is in S3; do not retry forever
        log(f"    registry write failed ({e}) -- keeping local copy")
        return False
    return True


def process_group(session, capture_dir: Path, cam_dir: Path, camera_id: str,
                  outage_id: str, group: str) -> bool:
    """One camera's segments from one capture group ('head' or 'tail')."""
    segs = sorted(cam_dir.glob("*.mp4"))
    if not segs:
        return True
    log(f"  {camera_id} [{group}]: {len(segs)} segment(s)")

    runs = partition_runs(segs)
    if not runs:
        log(f"  {camera_id} [{group}]: nothing usable")
        return True

    all_ok = True
    for run_idx, run in enumerate(runs):
        total = sum(seg_duration(pr) for _, pr in run)
        target = chunk_length(total)
        chunks, cur, cur_len = [], [], 0.0
        for item in run:
            cur.append(item)
            cur_len += seg_duration(item[1])
            if cur_len >= target:
                chunks.append(cur)
                cur, cur_len = [], 0.0
        if cur:
            chunks.append(cur)

        for i, chunk in enumerate(chunks, 1):
            start = segment_start(chunk[0][0])
            dur = sum(seg_duration(pr) for _, pr in chunk)
            merged = capture_dir / f"{camera_id}-{group}-{run_idx}-{i}.mp4"
            if not merge(chunk, merged):
                all_ok = False
                continue
            labels = ["outage-buffer", group, f"outage:{outage_id}"]
            if len(chunks) > 1:
                labels.append(f"part {i}/{len(chunks)}")
            if len(runs) > 1:
                labels.append("gap-before" if run_idx else "")
            labels = [l for l in labels if l]

            if upload_and_register(session, camera_id, merged, start, dur, labels):
                for p, _ in chunk:
                    p.unlink(missing_ok=True)     # only after a confirmed 200
                merged.unlink(missing_ok=True)
                log(f"    registered {start.isoformat()} ({dur:.0f}s)")
            else:
                merged.unlink(missing_ok=True)
                all_ok = False
    return all_ok


def process_capture(session, capture_dir: Path) -> None:
    journal = capture_dir / "state.json"
    try:
        j = json.loads(journal.read_text())
    except Exception:
        return
    if j.get("status") != "pending-upload":
        return

    ended = j.get("endedWall")
    if ended:
        age = (utc_now() - datetime.fromisoformat(ended)).total_seconds()
        if age < SETTLE_AFTER_RECOVERY_SEC:
            return      # let kvssink drain its own backlog first

    log(f"backfilling {j['outageId']} ({j.get('elapsedSec')}s outage)")
    ok = True
    for group, root in (("head", capture_dir), ("tail", capture_dir / "tail")):
        if not root.is_dir():
            continue
        for cam_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "tail"):
            camera_id = _camera_id_for_path(cam_dir.name)
            if not process_group(session, capture_dir, cam_dir, camera_id,
                                 j["outageId"], group):
                ok = False

    if ok:
        j["status"] = "done"
        j["uploadedAt"] = utc_now().isoformat()
        journal.write_text(json.dumps(j, indent=2))
        # Everything is confirmed in S3 and registered; the tree is now redundant.
        import shutil
        shutil.rmtree(capture_dir, ignore_errors=True)
        log(f"  {j['outageId']}: complete, local copy removed")
    else:
        log(f"  {j['outageId']}: incomplete -- will retry next pass")


def _camera_id_for_path(mediamtx_path: str) -> str:
    # "cam02" -> "cam-02". camera_control owns the forward conversion; this is the only
    # place the inverse is needed, and it must agree with it.
    if mediamtx_path.startswith("cam") and mediamtx_path[3:].isdigit():
        return f"cam-{mediamtx_path[3:]}"
    return mediamtx_path


def main():
    log("outage uploader starting")
    session = None
    while True:
        try:
            ok, why = buffer_ready()
            if not ok:
                time.sleep(SCAN_SEC)
                continue
            pending = sorted(OUTAGE_DIR.glob("*/state.json"))
            if pending:
                from aws_device_creds import get_session
                # Fresh session per pass: the role-alias token is valid 3600s and this is
                # a long-running daemon (see aws_device_creds.py).
                session = get_session(REGION)
                for journal in pending:
                    process_capture(session, journal.parent)
            time.sleep(SCAN_SEC)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            log(f"pass failed ({type(e).__name__}: {str(e)[:160]}) -- retrying")
            time.sleep(SCAN_SEC)


if __name__ == "__main__":
    sys.exit(main())
