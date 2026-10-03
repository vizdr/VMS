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

It is also the one executor of PIR clip uploads (PIR-MQTT-VMS-PI4.md §3.7, §3.11), from both
GUIs: the admin app's `upload-requested` marker, and `uploadRequestedAt` set on a clip's
pir-local row by the cloud page. Results go to `uploaded.json` and onto that row.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

import camera_control
from outage_buffer import (BUFFER_ROOT, OUTAGE_DIR, PIR_DIR, buffer_ready, log, read_registry_cache,
                           segment_start, utc_now)
from pir_cloud import clip_start_ts

REGION = config.AWS_REGION
BUCKET = config.EVIDENCE_BUCKET
SCAN_SEC = 30
PIR_TABLE, PIR_REQUEST_INDEX = "pir-local", "upload-requests"   # cloud/pir_stack.py
PIR_SESSION_MAX_AGE_SEC = 1800     # the cloud poll runs every pass; the token lives 3600 s
PIR_VERIFY_SEC = 300               # a clip deleted in AWS is back to "local only" within 5 min

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


def merge(chunk: list[tuple[Path, dict]], out: Path,
          inpoint: float = None, outpoint: float = None) -> bool:
    """Concat with ffmpeg, transcoding audio to AAC.

    `-c:a aac` is mandatory, not a preference. MediaMTX writes LPCM as ISO 23003-5 `ipcm`
    and G.711 as ulaw/alaw sample entries, and no browser decodes either inside MP4 --
    `-c copy` would give a clip that plays in ffplay and is silent in the cloud client,
    exactly the trap CLAUDE.md warns about.

    `+faststart` because the clip is played from a presigned URL in a <video> tag;
    `+genpts` because each segment's internal timestamps do not start at zero.
    """
    # inpoint/outpoint trim the first and last file (seconds from each file's own start) --
    # the PIR watcher cuts a session's window out of whole ring segments. With -c:v copy the
    # cut lands on the keyframe at or before the inpoint, so a clip may start up to one GOP
    # (2 s on cam-01) early -- never late.
    lines = []
    for i, (p, _) in enumerate(chunk):
        lines.append(f"file '{p}'\n")
        if i == 0 and inpoint:
            lines.append(f"inpoint {inpoint:.3f}\n")
        if i == len(chunk) - 1 and outpoint:
            lines.append(f"outpoint {outpoint:.3f}\n")
    listing = out.with_suffix(".txt")
    listing.write_text("".join(lines))
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


def video_codec(path: Path) -> str | None:
    """'h264' / 'h265', the names clip_to_s3.py and record_clip.py write into the clips table.
    Every other row there carries it; uploads from here used to be the only ones without
    (PIR-MQTT-VMS-PI4.md §4.3, data contract rule 3)."""
    probe = ffprobe(path)
    for s in (probe or {}).get("streams", []):
        if s.get("codec_type") == "video":
            return {"h264": "h264", "hevc": "h265"}.get(s.get("codec_name"))
    return None


def upload_and_register(session, camera_id: str, merged: Path, start: datetime,
                        duration: float, labels: list[str], suffix: str = "outage") -> str | None:
    """Upload, confirm, then register. Returns the S3 key only if all three succeeded.

    `suffix` names the file: "outage" for backfill, "pir-s<seq>" for a requested PIR clip --
    the seq because two PIR sessions can start in the same second, and the key has only
    second resolution.
    """
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from botocore.exceptions import ClientError

    key = (f"clips/{camera_id}/{start.strftime('%Y/%m/%d')}/"
           f"{start.strftime('%H%M%S')}-{suffix}.mp4")
    codec = video_codec(merged)
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
            return None
    except Exception as e:
        log(f"    upload failed ({type(e).__name__}: {str(e)[:120]}) -- keeping local copy")
        return None

    try:
        table = session.resource("dynamodb", region_name=REGION).Table("clips")
        table.put_item(
            Item={
                "cameraId": camera_id,
                "startTs": start.isoformat(),
                "s3Key": key,
                "labels": labels,
                "durationSec": int(round(duration)),
                **({"videoCodec": codec} if codec else {}),
            },
            # clip_to_s3.py and record_clip.py both put_item unconditionally; do not copy
            # that here. A key collision with a real evidence clip must fail loudly rather
            # than overwrite it.
            ConditionExpression="attribute_not_exists(startTs)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log(f"    a clip already exists at {start.isoformat()} -- not overwriting")
            return key           # object is in S3; do not retry forever
        log(f"    registry write failed ({e}) -- keeping local copy")
        return None
    return key


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


def pending_pir_uploads() -> list[Path]:
    """PIR sessions whose clip someone asked to upload (PIR-MQTT-VMS-PI4.md §3.7): a merged
    clip with an `upload-requested` marker and no `uploaded.json` yet."""
    if not PIR_DIR.is_dir():
        return []
    return sorted(m.parent for m in PIR_DIR.glob("*/upload-requested")
                  if not (m.parent / "uploaded.json").exists())


_pir_warned = set()


def process_pir_request(session, sdir: Path, via: str = "lan", requested_by: str | None = None) -> None:
    """Upload one requested PIR clip. The local clip stays: an upload is a copy (D10), and
    local retention (pir_watcher.py) decides when the local file goes."""
    try:
        j = json.loads((sdir / "state.json").read_text())
    except Exception:
        return
    clip = sdir / (j.get("clip") or "clip.mp4")
    if j.get("status") != "merged" or not clip.exists():
        if sdir.name not in _pir_warned:          # not ready yet, or failed: say so once
            log(f"pir {sdir.name}: upload requested, but the clip is {j.get('status')} -- waiting")
            _pir_warned.add(sdir.name)
        return
    start = datetime.fromtimestamp(float(j["from"])).astimezone()
    labels = ["pir", f"pir:seq {j.get('seq')}"]
    log(f"pir {sdir.name}: upload requested" + (f" in the cloud by {requested_by}" if via == "cloud" else ""))
    key = upload_and_register(session, j["cameraId"], clip, start,
                              float(j.get("durationSec") or 0), labels,
                              suffix=f"pir-s{j.get('seq')}")
    if not key:
        log(f"  pir {sdir.name}: not uploaded -- the request stays and is retried next pass")
        return
    record = {"s3Key": key, "startTs": start.isoformat(), "uploadedAt": utc_now().isoformat(),
              "sizeBytes": clip.stat().st_size, "videoCodec": video_codec(clip),
              "via": via, "requestedBy": requested_by}
    _write_json(sdir / "uploaded.json", record)
    log(f"  pir {sdir.name}: in AWS as {key}; local copy kept")


def _write_json(path: Path, data) -> None:
    """This process is the only writer of uploaded.json and cloud-request.json."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# --- cloud requests and results on pir-local (PIR-MQTT-VMS-PI4.md §3.11, Phase 10) -------------

_pir_cloud = {"session": None, "at": 0.0, "ok": None, "unindexed": set(), "verified": 0.0}


def _pir_session():
    c = _pir_cloud
    if c["session"] is None or time.monotonic() - c["at"] > PIR_SESSION_MAX_AGE_SEC:
        from aws_device_creds import get_session
        c["session"], c["at"] = get_session(REGION), time.monotonic()
    return c["session"]


def _short_timeouts():
    # One attempt, seconds not minutes: this runs every pass, and an unreachable AWS must not
    # cost what boto3's defaults take (outage_buffer._fetch_registry).
    from botocore.config import Config
    return Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})


def _pir_table():
    return _pir_session().resource("dynamodb", region_name=REGION, config=_short_timeouts()).Table(PIR_TABLE)


def cloud_requests(cameras: list[str]) -> list[dict]:
    """Rows where someone pressed Upload in the cloud page and no result is written yet.
    Read from the sparse index, never by querying the table: see pir_stack.ensure_request_index."""
    from boto3.dynamodb.conditions import Attr, Key
    table, found = _pir_table(), []
    for cam in cameras:
        kwargs = {"IndexName": PIR_REQUEST_INDEX, "KeyConditionExpression": Key("cameraId").eq(cam),
                  "FilterExpression": Attr("uploadedKey").not_exists() & Attr("uploadError").not_exists()}
        while True:
            r = table.query(**kwargs)
            found += r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return sorted(found, key=lambda i: i["uploadRequestedAt"])


def _row_result(row_key: dict, **fields) -> bool:
    """Write the uploader's fields -- and only those -- onto an existing index row. False if the
    row is not there (not indexed yet, or already expired): never create a row from here."""
    from botocore.exceptions import ClientError
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    removes = [k for k in ("uploadError",) if k not in fields]
    try:
        _pir_table().update_item(
            Key=row_key, ConditionExpression="attribute_exists(sk)",
            UpdateExpression=f"SET {sets}" + (f" REMOVE {', '.join(removes)}" if removes else ""),
            ExpressionAttributeValues={f":{k}": v for k, v in fields.items()})
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def process_cloud_request(session, row: dict) -> None:
    sid, key = row.get("sessionId"), {"cameraId": row["cameraId"], "sk": row["sk"]}
    sdir = PIR_DIR / sid if sid else None
    if sdir is None or not (sdir / "state.json").exists():
        # buffer_ready() was checked by the caller, so a missing folder really is gone, not an
        # unplugged stick.
        _row_result(key, uploadError="gone", uploadedAt=utc_now().isoformat())
        log(f"pir {sid}: requested in the cloud by {row.get('requestedBy')}, but the clip is gone")
        return
    try:
        j = json.loads((sdir / "state.json").read_text())
    except Exception:
        return
    if j.get("status") != "merged" or not (sdir / (j.get("clip") or "clip.mp4")).exists():
        _row_result(key, uploadError=f"clip {j.get('status')}", uploadedAt=utc_now().isoformat())
        return
    if not (sdir / "cloud-request.json").exists():
        # Recorded locally: the admin app shows it, and retention treats the clip as pending.
        _write_json(sdir / "cloud-request.json", {
            "requestedAt": row["uploadRequestedAt"], "requestedBy": row.get("requestedBy"),
            "seenAt": utc_now().isoformat()})
    if not (sdir / "uploaded.json").exists():
        process_pir_request(session, sdir, via="cloud", requested_by=row.get("requestedBy"))
    # The result reaches the row through mirror_results(), like every other upload's.


def mirror_results() -> None:
    """Put every local upload result onto its index row, whichever GUI asked for it -- so the
    cloud page shows a clip uploaded from the admin app as 'in AWS' too. uploaded.json gets
    indexRowUpdated once that write succeeded; until the watcher has indexed the clip there is
    no row yet, and this retries quietly each pass."""
    if not PIR_DIR.is_dir():
        return
    for path in sorted(PIR_DIR.glob("*/uploaded.json")):
        try:
            rec = json.loads(path.read_text())
            j = json.loads((path.parent / "state.json").read_text())
        except Exception:
            continue
        if rec.get("indexRowUpdated"):
            continue
        key = {"cameraId": j["cameraId"], "sk": f"clip#{clip_start_ts(j)}"}
        if _row_result(key, uploadedKey=rec["s3Key"], uploadedAt=rec["uploadedAt"]):
            _write_json(path, {**rec, "indexRowUpdated": True})
            _pir_cloud["unindexed"].discard(path.parent.name)
        elif path.parent.name not in _pir_cloud["unindexed"]:
            _pir_cloud["unindexed"].add(path.parent.name)
            log(f"pir {path.parent.name}: uploaded, but its index row isn't there yet -- retrying")


def verify_uploads() -> None:
    """A PIR clip whose AWS copy was deleted -- from either project's page, or by hand -- goes
    back to "on the Pi only" in both GUIs and can be uploaded again (D10: deleting the copy in
    AWS never touches the local clip). Existence is read by listing the day folders that hold
    uploads, not by HEAD: without ListBucket on the whole bucket, a HEAD on a missing key answers
    403, not 404, and PirLocal grants ListBucket for `clips/` only.

    Resetting ends the request cycle, so this also removes the request markers -- the admin app's
    `upload-requested` and pir-control's `uploadRequestedAt`/`requestedBy`. It is the one place
    this process touches another writer's field, and only once the copy is gone: left in place,
    either marker would make the next pass upload the clip again. uploaded.json goes last, so a
    crash halfway leaves a state the next check simply repeats."""
    from botocore.exceptions import ClientError
    records = {}
    for path in PIR_DIR.glob("*/uploaded.json") if PIR_DIR.is_dir() else []:
        try:
            records[path.parent] = (json.loads(path.read_text()),
                                    json.loads((path.parent / "state.json").read_text()))
        except Exception:
            continue
    if not records:
        return
    s3 = _pir_session().client("s3", region_name=REGION, config=_short_timeouts(),
                               endpoint_url=f"https://s3.{REGION}.amazonaws.com")
    existing = set()
    for prefix in {rec["s3Key"].rsplit("/", 1)[0] + "/" for rec, _ in records.values()}:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
            existing.update(o["Key"] for o in page.get("Contents", []))
    for sdir, (rec, j) in records.items():
        if rec["s3Key"] in existing:
            continue
        try:
            _pir_table().update_item(
                Key={"cameraId": j["cameraId"], "sk": f"clip#{clip_start_ts(j)}"},
                ConditionExpression="uploadedKey = :k",     # only a row that still points at it
                UpdateExpression="REMOVE uploadedKey, uploadedAt, uploadError, uploadRequestedAt, requestedBy",
                ExpressionAttributeValues={":k": rec["s3Key"]})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        for name in ("upload-requested", "cloud-request.json", "uploaded.json"):
            (sdir / name).unlink(missing_ok=True)
        log(f"pir {sdir.name}: its AWS copy {rec['s3Key']} was deleted -- back to local only")


def pir_cloud_pass(session_for_upload) -> None:
    """One pass of the cloud side. Its own error handling: an unreachable AWS here must not
    hold up outage backfill or LAN requests, and is logged once per streak, not every 30 s."""
    cameras = [cam for cam, row in read_registry_cache().items() if row.get("pirTopic")]
    if not cameras:
        return
    c = _pir_cloud
    try:
        rows = cloud_requests(cameras)
        if rows:
            session = session_for_upload or c["session"]
            for row in rows:
                process_cloud_request(session, row)
        mirror_results()
        if time.monotonic() - c["verified"] >= PIR_VERIFY_SEC:
            verify_uploads()
            c["verified"] = time.monotonic()
        if c["ok"] is False:
            log("pir-local reachable again -- cloud requests resume")
        c["ok"] = True
    except Exception as e:  # noqa: BLE001
        if c["ok"] is not False:
            log(f"pir-local unreachable ({type(e).__name__}: {str(e)[:120]}) -- cloud requests wait")
        c["ok"], c["session"] = False, None


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
            pir_requests = pending_pir_uploads()
            session = None
            if pending or pir_requests:
                from aws_device_creds import get_session
                # Fresh session per pass: the role-alias token is valid 3600s and this is
                # a long-running daemon (see aws_device_creds.py).
                session = get_session(REGION)
                for journal in pending:
                    process_capture(session, journal.parent)
                # After any outage backfill: that is the footage KVS lost, so it goes first.
                for sdir in pir_requests:
                    process_pir_request(session, sdir)
            pir_cloud_pass(session)          # None: it uses its own cached session
            time.sleep(SCAN_SEC)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            log(f"pass failed ({type(e).__name__}: {str(e)[:160]}) -- retrying")
            time.sleep(SCAN_SEC)


if __name__ == "__main__":
    sys.exit(main())
