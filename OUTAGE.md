# Durable outage buffering — design, findings and status

Closing the last open row in the guide's §16.2 gap analysis: *"Outage buffering | 32 GB
USB, auto-backfill | RAM only (`storage-size`) | **M** — disk-backed queue"*. This is the
feature the reference product markets hardest and the one this prototype most
conspicuously lacked.

Compiled 2026-09-19; cross-references refreshed 2026-09-26.
**`Demo-AWS-Video-revCosts4.md` remains the canonical build guide**;
this document is the working record for one feature and will be folded into §16.3c when
the work completes. `COSTS-1.4.md` stays authoritative for bitrate and cost.

**Every claim below is marked measured or predicted.** This document has already had to
retract one confident assertion (§3.1), which is exactly why the distinction is kept.

| Status | Meaning |
|---|---|
| **measured** | observed on the live system, with the number recorded |
| *predicted* | derived from source, disassembly or arithmetic; not yet exercised |

The defects this work produced are catalogued in **`FoundAndFixed.md` #18–#23 and #39**;
that file is the defect record, this one is the design reasoning and the measurements.

---

## 1. The problem, stated precisely

### 1.1 What actually happens today is worse than "RAM only" suggests

Read from the vendored SDK rather than the guide:

| Fact | Source |
|---|---|
| `DEFAULT_BUFFER_DURATION_SECONDS 120` | `gstkvssink.cpp:99` |
| `DEFAULT_STORAGE_SIZE_MB 128` | `gstkvssink.cpp:111` |
| Eviction = `CONTENT_STORE_PRESSURE_POLICY_DROP_TAIL_ITEM` / `CONTENT_VIEW_OVERFLOW_POLICY_DROP_UNTIL_FRAGMENT_START` | `StreamDefinition.h:74-75` |
| `bufferDurationOverflowPressureHandler` is `UNUSED_PARAM(custom_data); return STATUS_SUCCESS;` | `KvsSinkStreamCallbackProvider.cpp:8-11` |
| Frame/fragment drops are `LOG_WARN` only | `KvsSinkStreamCallbackProvider.cpp:43-59` |

Three consequences, none of them documented before now:

1. **The 128 MB store never binds — the 120-second buffer duration does.** At `cam-01`'s
   1.0 Mbps, 120 s is 15 MB against a 128 MB store. An outage longer than two minutes
   loses footage no matter how much RAM is free.
2. **Eviction is drop-oldest, keep-newest** — precisely backwards for outage buffering.
   The start of the outage, usually the interesting part, is discarded first.
3. **The overflow callback is a silent no-op.** Nothing surfaces.

None of the production scripts set `storage-size` at all (`adapter/bin/stream-cam01.sh`,
`stream-cam02.sh`, `stream-channel.sh`) — they inherit the compiled-in defaults.

### 1.2 §10.2's test cannot detect this, twice over

The guide's outage test (§10.2) blocks port 443/8883 for **120 seconds** — exactly the
SDK's own buffer duration, so KVS loses nothing and the test proves nothing about
buffering. And its suggested grep:

```bash
journalctl -u kvs-cam01 -f | grep -Ei "retry|reconnect|error"
```

does not match `droppedFrame`, `storage overflow`, or `Overall storage byte size`
(`KinesisVideoStream.cpp:45-68`) — the only lines that would actually show the buffer
filling. §10.2 has also never been executed: `measurements/reconnect_timeline.md`, listed
in the repo layout at guide:2004, does not exist.

### 1.3 Two conflicting specs already existed, neither implemented

| | §16.3c | §17/M1 + M3 |
|---|---|---|
| Medium | `/mnt/usb` | `/var/spool` (SD card) |
| Segment | 10 s MP4 | 60 s MPEG-TS |
| Writer | `splitmuxsink` | `splitmuxsink` |
| Uploader | "walks the directory" | `adapter/bin/uploader.py`, fully specified, does not exist |

This work supersedes both. **§17/M1's MPEG-TS choice was a latent bug** — see §3.1: it
cannot carry either camera's audio and would have recorded mute, silently.

The one rule worth carrying over verbatim is M3's: **delete only after a confirmed 200**
(guide:2648-2651).

---

## 2. Decisions

Taken deliberately, with the alternatives considered:

| Decision | Choice | Why not the alternative |
|---|---|---|
| Arming | **Hybrid pre-roll** — always record with a rolling 2 min window; on outage, stop deleting | A pure outage-trigger loses the footage between the drop and detection. Continuous-with-cloud-delete has no gap either but writes to flash 24/7. |
| At limit | **Freeze the capture, resume the rolling window** — keep the beginning of the outage, and pick up the last 2 min before recovery too | `record: false` would also lose the run-up to recovery. Resuming costs nothing and adds ~80 s beyond what kvssink replays (§6.2). The middle of a long outage is still lost either way. |
| Surfacing | **Existing clip list**, merged chunks, labelled `outage-buffer` | A separate UI section needs new Lambdas; raw 30 s segments flood `list_clips`' 50-row page. |
| Detection | **MQTT `on_connection_interrupted` / `on_connection_resumed`** | Free, no polling, already-present plumbing. Its ~45 s latency is affordable *only because of the pre-roll* — see §2.1. |
| Segment | 30 s fMP4 | |
| Limits | Off (default), 30 s, 2 min, 5 min, 10 min, 30 min, 1 h, 5 h, **12 h, 24 h** | |
| Minimum outage | **120 s** — shorter outages produce no clip at all | Below it kvssink loses nothing, so a clip would duplicate footage already in the cloud (§4.6). |

### 2.1 Why the slow detector is correct here

The pre-roll and the cheap detector interlock. The pre-roll works only if the retained
window at detection still reaches back past the drop:

```
outage begins                 T0
MQTT interruption surfaces    T0 + ~45 s      (1.5 x keep_alive_secs=30)
MediaMTX is holding           [now - 2 min, now]  =  [T0 - 75 s, T0 + 45 s]
                                     ^ 75 s of margin BEFORE the outage
```

Detection latency therefore costs no footage, and there is 75 s of pre-outage context as
a bonus.

> **Invariant: `pre-roll` > worst-case detection latency.** If `keep_alive_secs` is ever
> raised, or detection is switched to something slower, the pre-roll must grow with it —
> otherwise this silently starts losing the start of every outage, which is the exact
> failure being fixed.

### 2.2 Time-limit semantics

Ambiguous unless stated, because a 30 s limit is shorter than the 2 min pre-roll:

> `outageBufferSec` is the footage recorded **after** the connection drops. The pre-roll
> is separate, fixed, and always included. **Total = pre-roll + limit**, with the cut
> landing on the first completed segment at or after `T0 + limit`.

So a 30 s limit yields ~2.5 min of footage — a legitimate "just capture the moment it
dropped" setting, but only once the pre-roll is named in the UI.

Since the limit no longer stops recording (§6.2), a long outage yields the head plus a
2-minute tail from just before recovery, with a gap between them whenever the outage
exceeds `limit + ~120 s`.

---

## 3. Findings

### 3.1 Audio IS recorded — a retracted claim

**Withdrawn:** an earlier version of this design stated buffered footage would be
video-only, inferred from the `skipping track %d (%s)` string in the MediaMTX binary. The
string is real; the inference was wrong, and it was asserted with more confidence than the
evidence supported.

Disassembling both recorder back-ends gives the actual codec tables:

| Back-end | Codecs |
|---|---|
| **fMP4** (15 cases, `recorder.(*formatFMP4).initialize` @ 0xe41810) | AV1, VP9, H265, H264, MPEG4Video, MPEG1Video, MJPEG, Opus, MPEG4Audio, MPEG4AudioLATM, MPEG1Audio, AC3, **G711, G722, LPCM** |
| **MPEG-TS** (10 cases, `recorder.(*formatMPEGTS).initialize` @ 0xe496c0) | H265, H264, MPEG4Video, MPEG1Video, MPEG4Audio, MPEG4AudioLATM, MPEG1Audio, Opus, AC3, KLV — **no G711, no LPCM** |

**measured:** a `cam-02` segment carries `Audio: pcm_s16be (ipcm / 0x6D637069), 8000 Hz,
1 channels`. MediaMTX decodes the camera's G.711 and stores it as ISO 23003-5 LPCM.

Two consequences:

- **`recordFormat: fmp4` is mandatory, not a preference.** MPEG-TS would silently record
  video-only with one WARN line — which is what guide §17/M1 specified.
- **The merge step must transcode audio to AAC.** No browser decodes `ipcm` or
  `ulaw`/`alaw` inside MP4, so `-c copy` would yield a clip that plays in `ffplay` and is
  silent in the cloud client. This is the same ingest-permissive / playback-strict trap
  the audio work hit three times (guide §18.1).

### 3.2 Which MediaMTX fields are live-patchable

`core.pathConfCanBeUpdated` (@ 0xe80060) clones the old `conf.Path`, copies a **fixed
whitelist**, and `DeepEqual`s. If it returns false, `pathManager.doReloadConf` closes and
recreates the path — disconnecting `rtspclientsink` and every reader, including the KVS
producer.

Whitelisted: `Regexp`, `Name`, `Forward`, `Record`, `RecordPath`, `RecordFormat`,
`RecordPartDuration`, `RecordMaxPartSize`, `RecordSegmentDuration`, `RecordDeleteAfter`,
and the RPICamera tuning block.

**Not whitelisted: `runOnRecordSegmentCreate` / `runOnRecordSegmentComplete`, and
`Playback`.** Setting the segment hook via the API would close and recreate the path,
killing the very stream the feature protects.

**measured:** `POST /v3/config/paths/add/cam01` with only `source: publisher` left the
live publisher untouched — `readyTime` and source id unchanged across the add. Likewise a
`PATCH` of all six record fields: both paths stayed `ready`, `readyTime` unchanged.

### 3.3 Any record-field PATCH restarts the *recorder*

Distinct from §3.2: the path survives, but `path.doReloadConf → startRecording` tears down
and rebuilds the recorder, so the next fMP4 segment can only begin at a random-access
unit. With `h264_i_frame_period=30` at 15 fps (`publish-cam01.sh:39`) that is up to ~2 s
of video; `cam-02`'s GOP is unknown and may be longer.

This killed the original design, which flipped `recordDeleteAfter` at T0 and so placed
that seam **precisely at the moment the outage starts** — the worst possible instant. See
§4.2.

### 3.4 LPCM is uncompressed and can dominate the buffer

**measured**, from real segments:

| | video | audio | total | 5 h | 12 h | 24 h |
|---|---|---|---|---|---|---|
| `cam-01` 720p15, audio off | 1.00 Mbps | — | **1.00 Mbps** | 2.25 GB | 5.40 GB | 10.8 GB |
| `cam-01` 720p15, audio on | 1.00 Mbps | 0.256 Mbps | **1.26 Mbps** | 2.83 GB | 6.80 GB | 13.6 GB |
| `cam-02` sub 640×360, audio on | 0.046 Mbps | 0.128 Mbps | **0.161 Mbps** | 0.36 GB | 0.87 GB | 1.74 GB |
| **both cameras, audio on** | | | | **3.2 GB** | **7.7 GB** | **15.4 GB** |

On `cam-02` the **audio track is ~3× the video** — 128 kbps of 8 kHz/16-bit PCM against a
46 kbps sub-stream. Counter-intuitive, and it means enabling audio roughly triples that
camera's buffer footprint.

Worst case (both cameras, audio on, **24 h**) is ~15.4 GB of 57 GB — 27 %, with the
transient merge headroom (~2.3 GB) on top. **Disk space is still not the binding
constraint; the time limit is**, and the disk guard exists mainly for the unmounted-stick
case.

It does stop being comfortable at fleet scale, though: **four `cam-01`-class cameras at
the 24 h limit is ~54 GB and overruns the stick.** The guard must therefore be a real
running check against free space, not a reassurance — and the 24 h option should be
understood as "one or two cameras", which is what this adapter runs.

### 3.5 Flash wear, and what the producer gate buys

*predicted*, from §3.4's rates:

| scenario | per day | per year | full-drive writes/yr |
|---|---|---|---|
| both armed, **24/7 (ungated)** | 19.1 GB | ~7.0 TB | ~123 |
| both, **gated on producer-active**, typical duty cycle | ~0.5 GB | ~180 GB | ~3 |

The gate is doing real work. Ungated, a 2-minute rolling window is close to the worst case
for a consumer USB stick's crude FTL — the same tiny LBA region rewritten continuously,
plus ext4 journal and directory churn from a file created and deleted every 30 s.

---

## 4. Design

### 4.1 MediaMTX records — not `splitmuxsink`

Both prior sketches bolt a `tee`+`splitmuxsink` leg onto the producer pipelines. That
means rewriting the pipelines that took the `profile=high`, LATM-CPD and shared-DTS bugs
to get right, and it ties buffering to the producer, which is normally **stopped**.

MediaMTX already runs, already holds both feeds, survives producer start/stop, and its
record fields are live-patchable through the same control API
`adapter/onvif-admin/app.py:113` already uses. **No GStreamer pipeline changes at all.**

### 4.2 The supervisor owns retention — MediaMTX's cleaner is never used

`recordDeleteAfter: 0s` is set **once at arming and never changed**. The supervisor's own
5 s tick deletes completed `live/` segments older than the pre-roll.

**At T0 the supervisor makes no MediaMTX API call at all.** It stops deleting, and starts
renaming completed segments from `live/<path>/` into `outage/<id>/<path>/` — same
filesystem, atomic, instant.

This removes, in one move:
- the §3.3 recorder seam at T0
- the `recordDeleteAfter` flip race
- the move-to-staging race
- MediaMTX's cleaner as custodian of the footage (one raced tick would have deleted the
  entire outage buffer, unrecoverably — the exact footage the feature exists to save)

### 4.3 Segment completeness without the hook

The hook is unusable (§3.2). MediaMTX keeps exactly one open segment per path and writes
into the final filename, patching the moov duration on close. Therefore:

> **A segment is complete iff a segment with a later start time exists beside it.**

Simpler, no coupling, and it survives a missed invocation. The last segment of an outage
has an unpatched duration — `ffprobe` it and drop only that one if it fails.

### 4.4 Arming rules

Armed only when **all** hold: `outageBufferSec > 0`; that camera's KVS producer is active;
the USB filesystem is genuinely mounted.

```
record: true
recordPath: /mnt/vms-buffer/live/%path/%Y-%m-%d_%H-%M-%S-%f
recordFormat: fmp4              <- mandatory, see 3.1
recordSegmentDuration: 30s
recordPartDuration: 1s          <- the RPO on power loss
recordDeleteAfter: 0s           <- deletion disabled; supervisor owns retention
```

Gating on producer-active is what keeps the pre-roll off the flash 24/7 (§3.5). It does
mean outage buffering is effectively a *demo-time* feature, since producers are normally
stopped — coherent with the cost rule, but worth saying plainly, because "durable outage
buffering" sounds like a 24/7 guarantee and is not one.

### 4.5 Detection

MQTT interruption is a **proxy** for KVS reachability and wrong in both directions: it
fires on duplicate-client-ID takeover, IoT throttling or a keepalive miss under load; it
stays silent when kvssink fails on expired role-alias credentials or a crash-looping
producer — the case CLAUDE.md explicitly warns about. So:

```
outage   = agent_says_interrupted OR probe_failed_N_consecutive
recovery = agent_says_resumed     AND probe_succeeds
```

with an independent TCP probe to the IoT endpoint every 10 s. This degrades to probe-only
if `agent.py` dies. The state file is a **heartbeat** rewritten every ~10 s regardless of
transitions — a transition-only file that stops changing is indistinguishable from a
healthy connection, so a crashed agent would silently disable the feature.

### 4.6 Backfill

Segments are already safe in `outage/<id>/`, so recovery has no critical section.

0. **Discard outages shorter than ~120 s.** kvssink buffers 120 s and replays 40 s, so a
   short blip loses nothing and a clip here would duplicate footage already in the cloud.
1. **Wait ~60 s before uploading.** At recovery kvssink is flushing its own backlog up the
   same uplink; adding chunks on top can cause a second outage. Then upload sequentially
   with boto3 managed transfer.
2. Partition into maximal runs that `ffprobe` cleanly **and share a track layout** — a
   layout change is not hypothetical, since toggling `audioEnabled` applies on the next
   producer start and could land mid-outage. Then:
   ```bash
   ffmpeg -nostdin -f concat -safe 0 -i list.txt \
          -fflags +genpts -max_interleave_delta 0 \
          -c:v copy -c:a aac -b:a 32k -movflags +faststart out.mp4
   ```
   `-c:a aac` is not optional (§3.1). `32k` not `64k` for 8 kHz sources — 64 k trips
   `Too many bits 8192 > 6144 per frame`, the same 1024-samples-per-frame arithmetic that
   governs the sample-rate choice in guide §18.3.
3. Upload to `clips/<cameraId>/<YYYY>/<MM>/<DD>/<HHMMSS>-outage.mp4`. The
   `clips/<cameraId>/...` shape is **mandatory** — `delete_clip.py:17-24` derives the
   camera from `key.split("/")[1]`.
4. Write a `clips` row: `cameraId`, `startTs`, `s3Key`, `labels: ["outage-buffer"]`,
   `durationSec`. Exactly `clip_to_s3.py:33-39`'s schema, so Play / Tier / Delete and the
   lifecycle rule work unchanged.
5. Delete local files only after a confirmed success.

**Chunk adaptively**, with a 2 h ceiling:

```
chunk = clamp(outageLen / 6, 10 min, 2 h)
```

One rule covers every limit, and it lands on exactly 2 h for the 12 h and 24 h cases as
specified:

| Limit | chunk | rows/camera | chunk size (`cam-01`, audio on) |
|---|---|---|---|
| ≤ 10 min | 10 min | 1 | ≤ 94 MB |
| 30 min | 10 min | 3 | 94 MB |
| 1 h | 10 min | 6 | 94 MB |
| 5 h | 50 min | 6 | 472 MB |
| **12 h** | **2 h** | **6** | **1.13 GB** |
| **24 h** | **2 h** | **12** | **1.13 GB** |

The ceiling matters in both directions. `list_clips.py:22` returns 50 clips *and* does one
synchronous `s3.head_object` per clip, so unbounded row counts bury real evidence clips —
but a chunk much larger than 2 h becomes an unwieldy single object to merge, upload and
seek within.

Three consequences of the 2 h / 1.13 GB chunk that must be handled:

- **Upload takes real time.** 1.13 GB on a typical home uplink is ~15 min per chunk; a
  24 h outage on `cam-01` is ~12 chunks ≈ 3 h to drain. Sequential upload and the 60 s
  post-recovery delay (step 1) are what stop this from competing with kvssink's own
  backlog.
- **Merge needs transient headroom** — output alongside inputs, ~2.3 GB peak per chunk.
- **The client renders `${clip.durationSec} sec.`** (`client/index.html:658`), which
  displays a 2 h clip as `7200 sec.` Format as h/m before shipping B3.

`startTs` and `durationSec` must be **measured** — from the first segment's real start and
from `ffprobe` on the merged file — never computed from the limit.

### 4.7 Storage layout

```
/mnt/vms-buffer/.vms-buffer-ok        # sentinel: refuse to arm without it
/mnt/vms-buffer/live/<path>/          # rolling window, supervisor-managed
/mnt/vms-buffer/outage/<id>/<path>/   # captured, outside any cleaner's scope
/mnt/vms-buffer/outage/<id>/state.json# atomic journal for reboot recovery
/mnt/vms-buffer/outage/<id>/merged/   # chunks pending upload
```

The sentinel is not belt-and-braces. If the mount disappears but the directory remains,
MediaMTX writes onto `mmcblk0` — and with 25 GB free a long buffer **fits**, which is
worse than failing, because it puts exactly the write load the USB stick exists to absorb
onto the SD card.

---

## 5. Status

### 5.1 B0 — provision and prove the writer — **DONE**

| Claim | Result |
|---|---|
| USB formatted and mounted | ext4 `vms-buffer`, 57 GB, UUID in `/etc/fstab` with `nofail,noatime` |
| Explicit `cam01` path doesn't drop the publisher | `readyTime` + source id **unchanged** |
| Record-field PATCH doesn't restart the path | both paths stayed `ready` |
| 30 s fMP4 segments land | cam01 3.76 MB/30 s = 1.00 Mbps ✓, cuts exactly 30 s apart |
| Audio is recorded | cam02 segment carries `pcm_s16be (ipcm)` 8 kHz — refutes §3.1's earlier prediction |
| `recordDeleteAfter: 0s` disables deletion | oldest segment still present after 4 min |
| A frame decodes | clean 1280×720, no corruption |
| Merge is browser-playable | 3 segments → 1:35.63, video copied, `ipcm` → AAC-LC, `+faststart` |

The USB previously held a bootable Pi OS clone; it was 3 days old, 2.3 MB of empty default
home directories, and was erased with the owner's approval. The Pi boots from `mmcblk0`
(`/etc/fstab` PARTUUID `bc1dd790-*`), so wiping it could not affect boot.

System left idle: recording disarmed, test segments cleared, both paths healthy.

### 5.2 B1 — detection and arming — **DONE**

New: `adapter/outage_buffer.py` (user unit `kvs-outage-buffer.service`),
`adapter/mediamtx_api.py`, `adapter/aws_state.py`. Modified: `adapter/agent.py`
(interrupted/resumed callbacks + heartbeat thread).

Verified live against a real network block:

| Claim | Result |
|---|---|
| Arms only when producer active | armed within one tick of `kvs-cam02` starting |
| Reconcile is idempotent | 0 re-patches once config matches |
| Rolling window bounds itself | 13 → **4 segments** (120 s / 30 s + 1 open) |
| Outage detected | `probe failed x2`, ~15 s after the block |
| **Pre-roll preserved** | earliest captured segment **130 s before detection** |
| `live/` stays bounded during capture | 1 segment |
| Outage < 120 s discarded | 44 s outage → "discarding capture (KVS loses nothing this short)" |
| Recovery finalises | `status: pending-upload`, journal written |
| Disarm hysteresis | producer stopped 13:48:50 → disarmed 13:49:50, exactly the 60 s grace |

**Not yet exercised:** the limit freeze and `collect_tail()`. Both recoveries arrived
before the 300 s test limit elapsed. They are implemented but unproven — treat as
*predicted* until B2 testing covers them.

#### Three bugs found by testing, all silent

1. **Segment filenames are local time, not UTC.** `recordPath`'s `%Y-%m-%d_%H-%M-%S` is
   formatted in the machine's zone; parsing it as UTC put every segment two hours in the
   future on a CEST box, so the retention cutoff never matched. **Measured 9 segments
   where 4–5 were expected — the rolling window grew without bound and would have filled
   the stick silently.** Now parsed with `astimezone()`, with an mtime fallback so an
   unparseable name can never become un-prunable.

2. **The supervisor blocked on AWS during an outage — the one time it must not.**
   `load_registry()` called DynamoDB on the tick path with boto3's defaults (60 s connect
   / 60 s read, with retries). When AWS went away the process sat in
   `poll_schedule_timeout` for **45 minutes**, never reaching the connectivity check,
   never detecting the outage it existed to watch for. Fixed two ways: all AWS access
   moved to a background thread so the tick loop makes no network call at all, and the
   scan pinned to `connect_timeout=3, read_timeout=5, max_attempts=1`.

3. **Recovery on a single successful probe caused flapping.** The IoT endpoint's DNS
   rotates across AWS ranges; one rotation briefly landed on a reachable address and the
   supervisor declared recovery, finalising the capture and opening another. **Measured 3
   finalise/reopen cycles within one outage**, fragmenting it into separate clips. Fixed
   with asymmetric thresholds — 2 consecutive failures to declare an outage, **3
   consecutive successes** to declare recovery. Acting early on failure is cheap (record
   footage that may not be needed); acting early on recovery is not (stop recording).

### 5.3 B2 — backfill — **DONE**

New: `adapter/outage_uploader.py` (user unit `kvs-outage-uploader.service`),
`cloud/iam/outage-backfill-policy.json` (attached to `KVSAdapterRole` as `OutageBackfill`
— the device role previously had no S3 or `clips` access at all).

Verified by a full cycle with a 120 s limit and a 198 s outage:

| Claim | Result |
|---|---|
| Pre-roll captured | head starts **41 s before** the block |
| Limit freezes the capture | fired 10 s after the limit (tick granularity) |
| Rolling window resumes after freeze | `live/` refilled while the outage continued |
| **Tail collected at recovery** | 7 head + **3 tail** segments |
| Two clips, not one | head 213 s + tail 93 s, separately labelled |
| Merge transcodes audio | `ipcm` → **`aac (LC) 8000 Hz`**, browser-playable |
| Uploaded and registered | both rows in `clips`, visible via `list-clips` |
| Delete only after confirmed 200 | local tree removed only once both succeeded |
| Frame decodes | clean 640×360; the camera's OSD reads **13:44:21** exactly 60 s into a clip registered at 13:43:21 — `startTs` correct end to end |

Labels carry the grouping: `outage-buffer, head, outage:20260919T121609Z`.

**A nuance the test exposed:** head ended 14:17:35 and tail began 14:17:35 —
*contiguous*. The §6.2 gap only opens when `outage_duration − limit` exceeds the pre-roll;
for shorter overruns the resumed rolling window covers the join completely. Worth stating,
because it makes the gap a long-outage phenomenon rather than a routine one.

#### A fourth bug: the probe cost twice its timeout

`socket.create_connection` applies its timeout **per resolved address**, and the IoT
endpoint has both A and AAAA records — so `PROBE_TIMEOUT = 4` cost **8 s per probe**,
measured. With two probes needed, detection took **86 s against a 120 s pre-roll**: it
still worked, but on a third of the margin §2.1 claims, and that margin would vanish
entirely on a host returning more addresses.

Replaced with an explicit resolve-then-try loop under a total `PROBE_BUDGET_SEC = 4`, so
detection latency is a property of the configuration rather than of DNS. Measured after:
0.02 s when reachable, hard-capped at 4 s when not.

### 5.4 B3 — user control — **DONE**

New: `cloud/lambda/set_camera_outage_buffer.py`,
`cloud/iam/set-camera-outage-buffer-policy.json`, role
`SetCameraOutageBufferLambdaRole`, route `POST /cameras/outage-buffer` (Cognito
authorizer `htx29t`, AWS_PROXY, MOCK OPTIONS) deployed to `prod`. Modified:
`list_cameras.py`, `client/index.html`, `adapter/onvif-admin/{app.py,static/index.html}`.

| Claim | Result |
|---|---|
| Validation, both APIs | 45 s and `true` rejected, unknown camera rejected, all 10 valid values accepted |
| `true` specifically | rejected — `isinstance(True, int)` is True in Python, so it needs an explicit `bool` check |
| Cloud → device | Lambda → DynamoDB → supervisor logged `registry: {'cam-02': 1800}` within the 60 s poll |
| Admin → device | same, via `POST /api/cameras/<id>/outage-buffer` |
| Default | both cameras `0` (Off) |
| Layout | rendered headlessly before deploying, not guessed |

**Applies within 60 s, no restart** — unlike the audio flag. The supervisor re-reads the
registry and arms or disarms MediaMTX recording in place, so there is nothing to bounce.
The UI says so, and also says the setting only does anything while that camera is
streaming.

The dropdown shows the disk cost against the long options (`24 hours (~13.6 GB)`),
because 24 h is a quarter of the stick and that is worth seeing *before* choosing it.

`durationSec` now renders as `3 h 33 min.` rather than `12780 sec.` — the 45 s evidence
clips still read as seconds, the threshold being 90 s.

#### Duplication accepted deliberately

`VALID_LIMITS` exists in four places: the Lambda, `outage_buffer.py`, and both GUIs. The
Lambda cannot import adapter code, and a value the API accepts but the supervisor rejects
would be silently ignored on the device — so the list is duplicated with a comment in each
copy pointing at the others. Noted here so the next person changing it knows where to look.

### 5.5 B4 — measure and document — **DONE**

New: `adapter/bin/gap-fill.py` (the measurement tool), `adapter/bin/awsblock.sh` (a
working outage simulator), `measurements/reconnect_timeline.md` — the file the repo layout
at guide:2004 has always listed and which never existed.

**The result the feature exists to produce**, measured on `cam-02`, both runs identical
apart from the setting:

| 5-minute WAN outage | gap-fill | lost |
|---|---|---|
| **before** (`outageBufferSec = 0`) | **27.38 %** | 220.8 s — 72.62 % |
| **after** (`= 3600`) | **99.78 %** | 0.7 s — 0.22 % |

Read honestly (full detail in `measurements/reconnect_timeline.md` §3):

- the residual 0.22 % is measurement edge, not lost footage — the window ends when the
  firewall rules came out, a few seconds after connectivity actually returned
- KVS-alone also rose (27.4 % → 36.0 %), which is **not** an effect of this feature, only
  where kvssink's 120 s ring happened to sit relative to recovery. Only the union is the
  claim
- the clip covers 90.3 %, not 100 %, because clips are built from *completed* 30 s
  segments; KVS's replay covers that tail, which is why the two sources must be unioned
- one paired run: enough for the order of magnitude, not a distribution

**Detection latency, measured separately:** 86 s on the first implementation, **4 s**
after the probe fix — against a 120 s pre-roll, a 30× margin.

Docs updated: guide §16.3c (rewritten as a measured result; §17/M1 marked superseded and
its MPEG-TS choice identified as a latent silent-mute bug), §16.2's gap-analysis row,
§10.2 (warning that its own test cannot work as written), `CLAUDE.md`, `LAUNCH.md` Part C,
`COSTS-1.4.md` §7.3b.

### 5.6 After delivery — one defect found on a second Pi

**`FoundAndFixed.md` #39**, found 2026-09-25 while verifying `LAUNCH.md` Part B on a Pi
that had no USB stick set up, and fixed here 2026-09-26. The supervisor crash-looped
before its loop ever ran: `main()` created `OUTAGE_DIR` to scan for orphan captures, and
with `/mnt/vms-buffer` absent that `mkdir` needs root. `PermissionError`, exit,
`Restart=on-failure`, 80 times.

It is worth recording rather than just fixing, because the design was already right and
one line ignored it: the sentinel check (§4.7) exists precisely so a missing stick means
*idle and disarmed*, and this document promises the outage units idle unless enabled. The
unguarded line simply ran first. Verified on this Pi both ways — with the stick absent
(active, zero restarts, `buffer unavailable (not mounted) -- idle, disarmed` in the
journal) and present (`buffer ready`, orphan scan once).

### 5.7 Not done

- **`collect_tail()` under a frozen capture** is exercised (B2) but the *gap* it is meant
  to narrow has never been observed, because no test outage ran long enough to exceed
  `limit + preroll`. §6.2's arithmetic is still *predicted*.
- **Multi-camera** — every test used `cam-02` alone. Two cameras buffering concurrently,
  and the disk guard actually tripping, are untested.
- **A long outage** — the longest run was ~5 minutes. The 2 h chunking path, the ~15 min
  per-chunk upload, and the 24 h disk footprint are arithmetic, not measurements.

---

## 6. Failure modes and open questions

### 6.1 Reconcile continuously, never on transitions alone

The supervisor must be a **reconciler**, the shape `event_watcher.py:197-226` already uses.
Every ~5 s it compares desired against actual and patches the difference.

| Failure | Handling |
|---|---|
| **MediaMTX restarts** | API config changes are **not persisted to the yml**; a restart reverts every armed path and recording silently stops. A reconciler re-applies within one tick. |
| **Armed but not writing** | Positive liveness: a new segment must appear every `2 × recordSegmentDuration`. One test catching a drifted config, a dead publisher, and a read-only stick. |
| **Pi reboots mid-outage** | Segments survive minus the last ~1 s part. Resume from `state.json`. Note the reboot probably also leaves producers stopped, so the supervisor comes up disarmed — defensible but surprising; decide explicitly. |
| **No RTC on a Pi 4** | A power-cut outage means the clock may be wrong with no NTP (network still down). Segment filenames feed `startTs`, the DynamoDB **sort key**. Store `(wall, monotonic)` pairs, correct after resync, label `clock-uncertain` otherwise. |
| **`agent.py` dies** | "No signal" must **not** read as an outage. Stale heartbeat ⇒ *unknown*: hold the pre-roll, refuse to switch to retain-all, and say so. |
| **Producer stopped mid-outage** | **An in-flight capture is never cancelled** — its only termination conditions are limit, disk guard, recovery. Arm/disarm uses hysteresis and reads `ActiveState`/`SubState`, not `is-active`'s single word, or a crash-looping producer disarms buffering exactly when it is needed. |
| **USB stick never set up** | Distinct from unplugging, and it bit: `main()` created `OUTAGE_DIR` at startup *before* the first `buffer_ready()` check, so on a Pi with no stick the `mkdir` needed root, raised `PermissionError`, and the unit sat at `activating` through 80 restarts. `is-active` would not have shown it — the state word was `activating`, not `failed`. Nothing now touches the buffer until `buffer_ready()` passes; the orphan scan runs on the first ready tick, and readiness transitions are logged (`FoundAndFixed.md` #39). |
| **USB unplugged** | Sentinel vanishes ⇒ disarm immediately. Worn flash usually fails by going silently read-only — caught by the liveness check. |
| **Corrupt segment** | Partition into good, track-equal runs; one clip per run. A gap between runs is information, not an error. |
| **WAN flaps during backfill** | Delete only after a confirmed 200; retry oldest-first. |
| **`clips` key collision** | `put_item` with `ConditionExpression=attribute_not_exists(startTs)`. `clip_to_s3.py` and `record_clip.py` both write unconditionally; do not copy that here. |

### 6.2 The hole in the middle — a consequence of "stop at limit" — RESOLVED

kvssink drops **oldest** (§1.1), so after a long outage KVS holds only the tail. The
buffer holds `[T0 − preroll, T0 + limit]`. For any outage longer than `limit + 120 s`
there is a **gap in the middle that exists nowhere**. That is the direct consequence of
choosing "stop at limit" over a rolling window, and it is unavoidable under that choice.

**Decided: at the limit, resume the rolling 2-minute window rather than stopping.** The
captured head is already frozen in `outage/<id>/`, so this costs one line — the supervisor
simply resumes deleting `live/` segments older than the pre-roll — and `record: false` is
then never used at all during an outage.

**Correction to how much this buys.** The design review claimed it "fills half of the
hole". It does not, and the arithmetic matters:

| | span delivered |
|---|---|
| kvssink on reconnect | `[T_rec − ~40 s, T_rec]` |
| resumed rolling window | `[T_rec − 120 s, T_rec]` |
| **net gain** | **~80 s** |

**measured** from source: rollback is capped at `replayDuration`, and
`gstkvssink.cpp:100` sets `DEFAULT_REPLAY_DURATION_SECONDS 40`. The SDK's own
the SDK's own `vendor/…/docs/buffering.md` confirms the rollback goes back to the last
ACK's next fragment *or*
`replayDuration`, **whichever is less** — so after a long outage with no ACKs landing,
kvssink re-sends ~40 s, not its full 120 s buffer.

So resuming the window adds ~80 s at the end of the outage, plus insurance if kvssink's
replay fails entirely. **The middle hole remains.** Worth taking — it is free — but not
worth describing as a fix for §6.2.

Consequence for backfill: an outage produces **two clip groups**, not one — the head
`[T0 − preroll, T0 + limit]` and the tail `[T_rec − 120 s, T_rec]` — with a documented
gap between them. Label them so the gap is visible rather than implied.

### 6.3 Other items — all closed

- **Minimum-outage threshold — agreed at 120 s.** §4.6 step 0 discards shorter outages.
  Below that kvssink loses nothing, so a clip would duplicate footage already in the
  cloud, and every routine MQTT wobble would otherwise mint a ~2.5 min clip.

- **`play_clip.py` presign TTL — FIXED and deployed.** Was a flat 300 s, which was fine
  for 45 s evidence clips (~5 MB) and wrong the moment 2 h / 1.13 GB outage chunks
  existed. Now scales with object size:

  | object | presign |
  |---|---|
  | 45 s clip, ~5 MB | 951 s |
  | 10 min chunk, ~90 MB | 1 821 s |
  | 2 h chunk, ~1.1 GB | 12 748 s (3.5 h) |

  Floor 15 min, ceiling 6 h, ~100 KB/s assumed effective rate. The response now also
  returns `expires_in`. **Caveat recorded in the code:** the URL is signed with the
  Lambda's *temporary* role credentials, so it dies when those do regardless of
  `ExpiresIn` — the 6 h ceiling keeps the promise roughly honest.

- **Re-registration side effect — FIXED, and milder than reported.** The design review
  said `onvif-admin/app.py` patching `sourceProtocol` would restart the path. **measured:
  it does not** — `pathConfCanBeUpdated` compares values, and the admin always sends
  `tcp`, which is already the stored value, so `DeepEqual` passes and the update applies
  in place. The restart only fires when a value genuinely changes.

  Two real issues remained and are fixed:
  1. `sourceProtocol` is a deprecated alias of `rtspTransport`. Now sends
     `rtspTransport` (verified live: accepted, applies in place).
  2. The patch was issued **unconditionally**. `source` is *not* in the in-place
     whitelist, so a re-registration that genuinely moves the URI restarts the path and
     disconnects every reader — including a running KVS producer and any outage-buffer
     recording mid-segment. That restart is correct when the URI moved; it is pure cost
     when it has not. The code now reads the current config first and skips the patch
     when the source is unchanged — the common case, where a user simply re-scans.

---

## 7. Verification plan

The feature's whole point is a measurement, so verification *is* the deliverable.

1. **Unit-level** — force the state file offline; confirm segments accumulate, the limit
   stops recording on time, and the pre-roll reaches back past T0.
2. **Disk guard** — unmount the USB mid-outage; confirm the supervisor disarms rather than
   writing to the SD card.
3. **Resilience** — `systemctl --user restart kvs-mediamtx` mid-outage (must re-arm within
   one tick); `kill -9` the supervisor (must resume from journal); `systemctl stop` the
   producer (capture must still complete and upload).
4. **§10.2, extended.** The guide's 120 s test is exactly the SDK's buffer duration and
   proves nothing. Run **90 s** (must produce *no* clip), **5 min**, and **30 min**.
   ```bash
   sudo iptables -A OUTPUT -p tcp --dport 443  -j DROP
   sudo iptables -A OUTPUT -p tcp --dport 8883 -j DROP
   ```
   **§10.2's snippet does not work on this network, and the failure is silent.**
   Three things were measured while building B1:

   - **It is IPv4-only.** This LAN has working IPv6, and AWS resolves to
     `2a05:d014:…`, so every "blocked" connection simply went over IPv6 and returned
     HTTP 200. The block appears applied, packet counters even increment on unrelated
     traffic, and nothing is actually blocked. **`ip6tables` rules are required too.**
   - **Blocking a resolved IP is useless.** The IoT endpoint rotates across AWS ranges —
     observed at `18.196.251.80`, `3.69.141.146`, `18.185.210.34` and `18.153.244.214`
     within minutes. Block ranges (`3/8, 18/8, 35/8, 52/8, 54/8` and `2a05::/16`), not
     addresses. Anthropic's API is on `160.79.104.10` / `2607:6bc0::`, outside all of
     them, so a developer session survives the block.
   - `iptables` here is `v1.8.11 (nf_tables)`, the nft-backed shim — present and working,
     despite the guide's warning that it can be absent on Debian trixie.

   **Confirm both families are genuinely blocked before believing any outage result**, by
   curling an AWS endpoint and checking it fails, not by trusting that the rule was added.
5. **§10.4 fragment continuity, before and after** — the "gap-fill percentage" §16.3c asks
   for, and the number worth publishing. Sum `list-fragments` durations against wall-clock
   across the outage window, counting the S3-backfilled span. Record in
   `measurements/reconnect_timeline.md`.
6. **Grep the right lines** — add `droppedFrame`, `storage overflow` and
   `Overall storage byte size` to §10.2's pattern.
7. **Decode a frame and play it in a browser.** Per CLAUDE.md, ffmpeg accepting it is
   necessary but not sufficient — and §3.1 is a live example of why.

---

## 8. What to reuse

| Reuse | Where |
|---|---|
| `mediamtx_path_name()` / `unit_name()` | `adapter/camera_control.py:17,27` — CLAUDE.md documents a real bug from reimplementing the `cam-01`→`cam01` strip |
| `get_stream_status()` | `adapter/camera_control.py:36` — but harden per §6.1 before arming on it |
| `get_session()` | `adapter/aws_device_creds.py` — fresh per call; never cache at process start |
| Daemon shape, registry poll, offline tolerance | `adapter/event_watcher.py:60,206` — including "registry read failed … keeping current set", which is essential since DynamoDB is unreachable exactly when `outageBufferSec` matters |
| `ClipGate` + `bin/replay-gate.py` | `adapter/event_watcher.py:67` — put the connectivity edge/debounce logic in a pure class replayable against a recorded log, rather than testing by pulling the network cable |
| Clip key shape, labels, `clips` schema | `cloud/lambda/record_clip.py:46`, `clip_to_s3.py:31` — `-manual` is the precedent for `-outage` |
| Clip list UI | `client/index.html:658,663` — labels and tier badges render for free |
| The `set_camera_audio` triple | Lambda + route + adapter route + checkbox, including the `appliesOn` message pattern |
| MediaMTX API access | `adapter/onvif-admin/app.py:35,113,136` — factor into `adapter/mediamtx_api.py` before a third copy appears |
| `sweep_cap()` concept | guide:2662-2673 — reuse the idea as the disk guard; discard the `/var/spool`+MPEG-TS mechanism (§3.1) |
