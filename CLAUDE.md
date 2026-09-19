# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A buildable, portfolio-grade "Cloud Adapter" demo: a Raspberry Pi 4B pulls video from
local cameras and pushes it outbound to AWS (Kinesis Video Streams), takes commands over
an outbound MQTT session to AWS IoT Core, and serves a browser client through API
Gateway — with **zero inbound ports opened** on the home router. The architectural thesis
(every arrow crosses the router outbound-initiated) is written up in
`OUTBOUND-CLOUD.md`.

**`Demo-AWS-Video-revCosts4.md` is the canonical, actively-maintained build guide and the
single source of truth for *why* things are built the way they are** — it's a narrative
log of the real build, including bugs hit and how they were diagnosed, updated
continuously as the system evolves. `LAUNCH.md` is the short operational runbook (what to
actually run, in order, assuming the guide has already been followed once) — check it
first for "how do I start/stop/verify this." `AUDIO.md` records how optional per-camera audio was designed and built (guide §18 is the
canonical reference; `AUDIO.md` keeps the reasoning, the two silent bugs that shaped it,
and the claims that were withdrawn). `OUTAGE.md` is the working record for durable outage
buffering (guide §16.3c) — design, measurements and open questions — and folds into
§16.3c when that work completes; it is authoritative for that feature in the meantime. `Demo-AWS-Video-MCh-15.md`, `COSTS-1.3.md`,
and `NETWORK.md` are earlier/companion material and may be stale relative to the current
guide; `SafeZone_Group-cloud_EN-rev_1.md` is the original product-requirements sketch this
demo is modeled on. **When in doubt about current architecture or "why is it done this
way," read `Demo-AWS-Video-revCosts4.md` (or grep it for the relevant §-number) before
guessing from code alone** — most non-obvious decisions are explained there with the
real failure that motivated them.

There is no build system, package manifest, or automated test suite. Verification is
done live, against the running Pi and AWS account — see "Verifying changes" below.

## Commands

### Adapter-side Python (agent, ONVIF admin, discovery)

All of `adapter/*.py` and `adapter/onvif-admin/` run under one venv:
```bash
source venv-adapter/bin/activate   # or call venv-adapter/bin/python3 directly
```

Run the local ONVIF admin GUI directly (normally managed by `onvif-admin.service`):
```bash
cd adapter/onvif-admin && python3 app.py   # http://<pi-ip>:8080
```

Run WS-Discovery from the CLI:
```bash
python3 adapter/bin/discover-onvif.py --user admin --password *** --timeout 5
```

Watch the MQTT control-plane state topic live (debug aid):
```bash
python3 adapter/observe_state.py
```

### Deploying a Lambda

Every function in `cloud/lambda/` is deployed the same way — zip the single file, push it:
```bash
cd cloud/lambda && zip -q <name>.zip <name>.py && \
  aws lambda update-function-code --function-name <name> --zip-file fileb://<name>.zip
```
Function names use hyphens (`get-hls-url`), files use underscores (`get_hls_url.py`).
IAM policy documents for each function's role live in `cloud/iam/`.

### Deploying the browser client

```bash
aws s3 cp client/index.html s3://vms-demo-client-596633517506/index.html \
  --content-type text/html --cache-control "no-cache, must-revalidate"
```
Served at **https://dugyd3kkt36pw.cloudfront.net** (CloudFront + OAC, guide §8.5.1). The
`--cache-control` flag is required and now does double duty: it prevented S3
static-website stale-client incidents, and it makes CloudFront revalidate rather than
serve a cached copy, so an upload is live immediately (`x-cache: RefreshHit`). Drop the
header and you also need `aws cloudfront create-invalidation --distribution-id
E1B12167KKII6B --paths '/*'`.

The plain-HTTP S3 website URL is still live pending §8.5.1's final cutover (OAC-only
bucket policy → Block Public Access → `delete-bucket-website`).

### systemd — two separate managers, easy to mix up

**System units** (root, `/etc/systemd/system/`, need `sudo systemctl`): `kvs-cam01.service`,
`kvs-cam02.service`, `kvs-cam@.service` (template for GUI-provisioned cameras, §16.6) — the
actual KVS producers that cost money while running.

**User units** (`~/.config/systemd/user/`, `systemctl --user`, no sudo): `kvs-mediamtx`,
`kvs-camera-init`, `kvs-camera-publish`, `kvs-agent`, `onvif-admin`,
`kvs-event-watcher` (ONVIF detection → clips), `kvs-outage-buffer` +
`kvs-outage-uploader` (durable outage buffering, `OUTAGE.md`).

A unit in one manager **cannot** `Requires=`/`After=` a unit in the other — they're
independent systemd instances. (A templated system unit once declared
`Requires=kvs-mediamtx.service`, a user unit, and failed with "Unit not found" — see
guide §16 for the fix, which was simply to drop the cross-manager dependency.)

Full launch sequence, order, and startup gotchas: `LAUNCH.md` Part B.

### Verifying changes

**`systemctl ... is-active` is not proof anything is actually working** — a producer unit
can report `active` while crash-looping silently against a dead upstream RTSP source.
Always verify media is actually flowing:
```bash
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam01      # local capture proof
ffprobe "http://127.0.0.1:8888/cam01/index.m3u8"             # MediaMTX's own local HLS
# cloud path: GetHLSStreamingSessionURL -> ffprobe the returned URL (LAUNCH.md Part C)
```
When changing anything in a GStreamer pipeline (`adapter/bin/*.sh`), don't stop at "no
pipeline errors and CPU/logs look right" — decode an actual frame and look at it
(`ffmpeg -i <url> -frames:v 1 out.png`, then view it). Two real regressions this project
shipped (a corrupted-looking probe that was actually a probing artifact, and a genuinely
wrong H.264 profile that rendered black only in a browser) were only caught this way —
CLI tools like `ffmpeg`/`ffprobe` are far more tolerant of malformed streams than a
browser's MSE decoder, so "ffmpeg played it" is necessary but not sufficient evidence.

## Architecture

### Two independent camera pipelines converge on MediaMTX

`cam-01` is a USB webcam (MJPG-only) that must be transcoded; `cam-02` (and any camera
added later) is a real ONVIF/RTSP IP camera that passes through untouched. Both publish
into **MediaMTX**, which is the local hub for everything downstream: it re-serves RTSP,
exposes its own local HLS output (port 8888, LAN-reachable — used only by the ONVIF admin
GUI's browser-side preview, never the cloud path), and exposes a control API (port 9997,
localhost-only) used to add camera paths live without a config-file rewrite + restart
(which would otherwise drop every other camera's connection). `cam-01`'s pipeline (`adapter/bin/publish-cam01.sh`)
uses the Pi 4's hardware JPEG-decode/ISP-convert/H.264-encode blocks (`v4l2jpegdec`,
`v4l2convert`, `v4l2h264enc` — all separate V4L2 M2M devices under `bcm2835-codec`) rather
than software elements, for a ~2x CPU reduction; the encoder must be told `profile=high`
explicitly, since GStreamer's `v4l2h264enc` wrapper otherwise negotiates Baseline even
though the hardware control's own default is High, and Baseline broke browser (but not
`ffmpeg`) playback.

**Audio is optional, per-camera, and off by default** (guide §18). Two registry flags gate
it — `audioCapable` (hardware fact, set at registration) and `audioEnabled` (user choice,
set from either GUI) — and the producer scripts read them once at startup via
`adapter/bin/camera-audio.py`. The setting therefore applies on the camera's **next
Start**, which is deliberate: KVS rejects a stream whose fragments change from video-only
to audio+video partway through, so applying it live would break `GetClip` across the
boundary. With audio off, every pipeline is byte-for-byte the pre-audio one.

The constraint that shapes all of it: **KVS's ingest and playback paths accept different
codecs, and ingest is the permissive one.** `kvssink` takes G.711 and malformed AAC
codec-private-data without complaint; `GetHLSStreamingSessionURL`/`GetClip` then refuse to
serve them. So audio is always transcoded to AAC at the producer (never encoded earlier
and passed through RTSP — `rtspclientsink` payloads AAC as LATM, which mangles the CPD),
and the sample rate is chosen against the *video frame rate* rather than for fidelity,
because `kvssink` synthesises the DTS that GStreamer audio buffers lack from a counter
shared with the video track. Guide §18.3 has the arithmetic; the short version is that
audio frame duration must exceed the video frame interval, and getting it wrong silently
loses half the audio.

**Durable outage buffering** (`OUTAGE.md`, guide §16.3c) is also MediaMTX's job, not a
pipeline change: it records a rolling 2-minute window to a USB stick
(`/mnt/vms-buffer`) for any camera whose registry row asks for it *and* whose producer is
running, keeps everything once AWS goes unreachable, and backfills merged clips into the
existing evidence-clip list on recovery. Per-camera, **off by default**. Two things make
it work and are easy to undo by accident: `recordDeleteAfter` is `0s` **permanently** (the
supervisor owns retention — handing it to MediaMTX's cleaner would let one raced tick
delete the captured outage), and **no MediaMTX API call happens at outage onset**, because
patching any record field rebuilds the recorder and puts a keyframe seam exactly at T0.
Measured effect on a 5-minute outage: gap-fill 27.4% → 99.8%.

A separate KVS producer process per camera (`kvs-cam01.service` / `kvs-cam02.service` /
future `kvs-cam@<id>.service` instances) pulls from MediaMTX's RTSP and pushes to its own
Kinesis Video Stream. **This is the layer Start/Stop buttons (in either GUI) actually
control** — toggling it does not affect MediaMTX or the camera's own feed, which keep
running regardless. This is a common point of confusion: the "local preview" (MediaMTX
HLS) and the "KVS push" (cloud) are independent signals.

### The camera registry is the single source of truth — not a hardcoded list

A DynamoDB table `cameras` (PK `cameraId`, e.g. `"cam-01"`) holds each camera's mode
(`transcode`/`passthrough`), ONVIF credentials, RTSP URL, IR-control capability, and KVS
stream ARN. Every Lambda that needs to validate a camera ID, and `agent.py`'s MQTT
handler, read this table directly — there is no hardcoded allow-list anywhere. Adding a
camera through the ONVIF admin GUI (below) makes it work everywhere (cloud client,
MQTT control, all API routes) immediately, with no code change. IAM for
camera-ARN-scoped actions (`kinesisvideo:*`, per-Lambda) uses a `stream/cam-*/*` wildcard
rather than enumerated ARNs specifically so this stays true without an IAM edit per
camera — a deliberate least-privilege tradeoff, not an oversight.

Credential storage in that table is phased: currently a plain `onvifPassword` attribute
(Phase 1); a planned Phase 2 moves it to an SSM Parameter Store `SecureString` referenced
by a `credentialRef`, decrypted only via the adapter's own device identity. Not yet built.

### Device identity: one X.509 cert, no static keys on the Pi

The adapter authenticates to AWS as IoT Thing `adapter-01` via an X.509 certificate
(`certs/`, gitignored) and an IoT **role alias** (`KVSAdapterRoleAlias`), which vends
short-lived AWS credentials over HTTPS given the cert for mTLS. `kvssink` uses this
directly for KVS `PutMedia`. `adapter/aws_device_creds.py` wraps the same
credentials-endpoint call for arbitrary boto3 use — `agent.py` and
`adapter/onvif-admin/app.py` both call it (fresh per request; the underlying token has a
3600s TTL, so caching a session at process start would silently break a long-running
daemon after an hour). This is why the role's IAM policy, not a second credential, is
what's widened whenever the adapter needs a new AWS permission (e.g. `CreateStream`,
DynamoDB access on `cameras`) — see `cloud/iam/kvs-producer-policy.json`.

### Two GUIs, deliberately not one

`client/index.html` — the **cloud** client. Static, S3-hosted, Cognito-authenticated,
reachable from anywhere. Talks only to API Gateway/Lambda/MQTT; has no LAN access.
Renders one panel per camera fetched from `GET /cameras` (not hardcoded), with live view
(cloud HLS via KVS), manual recording, evidence-clip browsing/tiering, and per-camera IR
control where applicable.

`adapter/onvif-admin/` — a small local Flask app, LAN-only, no login, run directly on the
Pi. It exists because **WS-Discovery is UDP multicast and only works from a process on
the camera's own LAN segment** — the cloud client and Lambda structurally cannot reach it.
It does discovery (`adapter/onvif_discovery.py`, shared with the `discover-onvif.py` CLI),
one-click registration (adds the MediaMTX path live, provisions a `kvs-cam@.service`
instance via the input-validated `adapter/bin/provision-camera.sh`, creates the KVS
stream, writes the `cameras` row), re-registration for an already-known camera (updates
MediaMTX's path + the registry row, but deliberately never touches systemd for `cam-01`/
`cam-02` — they predate the `kvs-cam@` template, and re-provisioning them would start a
second, conflicting producer), and local control (Start/Stop, IR mode, a live
`systemctl is-active`-backed status column, and the local-HLS preview mentioned above).

Naming quirk both share: the camera identifier is hyphenated (`cam-01`, used for the KVS
stream name, DynamoDB key, S3 key prefix, and every API field) but the MediaMTX path name
and systemd unit suffix are not (`cam01`, `kvs-cam01.service`). `camera_control.py`'s
`mediamtx_path_name()`/`unit_name()` are the one place this conversion happens — a naive
per-caller `f"kvs-{camera_id}.service"` once produced a nonexistent unit name that
`systemctl` silently no-op'd against, so route every new unit-name computation through
these helpers rather than reimplementing the strip.

### Cost is a first-order design constraint, not an afterthought

The guide's §1.2 cost model treats "never leave `kvs-cam0N.service` running unattended"
as a hard rule (it's what incurs `PutMedia` charges) — this shows up throughout the code
as the reason Start/Stop exists as an explicit action rather than the producer just
running continuously, and why "stop the stream" is step one of the teardown/shutdown
sequence in `LAUNCH.md` Part F.
