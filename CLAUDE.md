# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A buildable, portfolio-grade "Cloud Adapter" demo: a Raspberry Pi 4B pulls video from
local cameras and pushes it outbound to AWS (Kinesis Video Streams), takes commands over
an outbound MQTT session to AWS IoT Core, and serves a browser client through API
Gateway — with **zero inbound ports opened** on the home router. The architectural thesis
(every arrow crosses the router outbound-initiated) is written up in
`OUTBOUND-CLOUD.md`.

**`FoundAndFixed.md` is the defect inventory** — every bug this project has hit, what it
looked like, why it happened and what fixed it, numbered permanently. Other docs keep only
the rule a bug produced plus a reference (`FoundAndFixed.md #N`); when you fix something
non-obvious, append an entry there and reference it rather than retelling the story in
three files.

`README.md` is the front door — project overview, feature list, and a Pi 4B install guide
written for someone arriving cold. It summarises; it is never the source of truth.

**`Demo-AWS-Video-revCosts4.md` is the canonical, actively-maintained build guide and the
single source of truth for *why* things are built the way they are** — it's a narrative
log of the real build, including bugs hit and how they were diagnosed, updated
continuously as the system evolves. `LAUNCH.md` is the short operational runbook (what to
actually run, in order, assuming the guide has already been followed once) — check it
first for "how do I start/stop/verify this." Its Part A is also the fresh-Pi setup
checklist (SDK build with its patches, venv, MediaMTX binary, AWS CLI, device
certificate, systemd unit files), each step with a command that proves it worked. `AUDIO.md` records how optional per-camera audio was designed and built — guide §18 is
the canonical operational reference, `FoundAndFixed.md` #15/#16/#17 are the defect
records, and `AUDIO.md` keeps what neither holds: why the design ended up this shape, and
the five claims that were withdrawn along the way. Guide **§21 (Appendix D)** is the reference for per-camera H.264/H.265, with the raw
evidence — encode capability, KVS behaviour, the browser matrix — in
`measurements/codec-phase0.md`, which carries a banner marking which parts describe the
successor's feature rather than this one. `OUTAGE.md` is the working record for durable outage
buffering (guide §16.3c) — design, measurements and open questions — and folds into
§16.3c when that work completes; it is authoritative for that feature in the meantime.
`PIR-MQTT-VMS-PI4.md` is the plan, now largely built, for PIR-triggered local recording of
`cam-01` (a Pico 2 W sensor over a local MQTT broker, footage from the outage-buffering ring), and for moving this repository's cloud side onto
its own `pir-`-prefixed resources (Lambdas, API, client bucket). Done: the broker and
`paho-mqtt` (Phase 0), the Pico firmware (1), the PIR switch (3), the session logic
`adapter/pir_session.py` (4), ring recording for PIR in the outage supervisor (5) and the
watcher `kvs-pir-watcher`, which writes `clip.mp4` per session to the stick (6), and uploads of
clips requested on the LAN (7: an `upload-requested` marker in the session folder;
`kvs-outage-uploader` sends it to the shared clip list labelled `pir`, and every upload now
carries `videoCodec`), the admin page's PIR panel and clip list (8), this project's own
`pir-` cloud resources: API, function copies, table, app client (9), and the Pi's status item
and clip index in the table `pir-local`, with upload requests from the cloud carried out by
the same uploader (10), this project's own cloud page on its own bucket and distribution
(11), and the PIR ring and session footage in RAM (12, its one-week soak postponed). Phase 2's
logger
`adapter/observe_pir.py` is ready, its measurements postponed. `PIR-MQTT-VMS-Pico.md` is a
**verbatim mirror** of the firmware repository's copy (`blink_freertos`, authoritative for the
Pico and the MQTT interface it publishes): never edit it here, copy it from there again. The
PI4 plan's §4 is the
compatibility contract with the successor, which is going ONVIF-only; read it before
touching anything shared in AWS. `Demo-AWS-Video-MCh-15.md` and `COSTS-1.3.md` are superseded earlier revisions of the
guide and the cost model. `NETWORK.md` is companion reference material — MediaMTX's role,
how WS-Discovery works, VLAN options, H.264-vs-H.265 — written while *planning* the MVP;
its analysis stands and its §-numbers still resolve, but it carries a banner marking the
three places the world moved on (MediaMTX is a unit now, discovery shipped, VLAN still
open). Reference it for the reasoning, not for what to run; `SafeZone_Group-cloud_EN-rev_1.md` is the original product-requirements sketch this
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

Scan the USB cameras attached to this Pi — formats, resolutions, frame rates, v4l2
controls and the microphone on the same USB device (read-only; safe while streaming).
The admin GUI's "Scan USB cameras" button calls the same code via `GET /api/usb-cameras`:
```bash
python3 adapter/usb_camera.py            # JSON, same shape as the GUI endpoint
```

Watch the MQTT control-plane state topic live (debug aid):
```bash
python3 adapter/observe_state.py
```

Watch the **local** MQTT broker (LAN, PIR plan — a different broker from AWS IoT Core):
```bash
mosquitto_sub -h localhost -u vms -P '<vms-password>' -v -t 'home/#'
```

Log and analyse the PIR sensor's traffic (Phase 2 of `PIR-MQTT-VMS-PI4.md`). Run it bounded,
from a transient unit or with `--hours`; **never as an enabled unit**, the `onvif-observe`
mistake below:
```bash
python3 adapter/observe_pir.py --out measurements/pir-<date>-<label>.jsonl --hours 5
python3 adapter/observe_pir.py --analyse measurements/pir-<date>-<label>.jsonl
python3 adapter/observe_pir.py --tail      # print events live, no file (tuning the sensor)
```

The PIR session logic is pure code (`adapter/pir_session.py`, no MQTT, no files, time passed
in), checked by its own scenarios and replayed against recorded logs, the way
`replay-gate.py` replays ONVIF logs through `ClipGate`:
```bash
python3 adapter/pir_session.py                                  # 11 scenario checks
python3 adapter/bin/replay-pir.py measurements/pir-<date>-<label>.jsonl --clips
python3 adapter/pir_watcher.py --dry-run    # the watcher, logging sessions without writing
```
PIR clips land in `/mnt/vms-buffer/pir/<sessionId>/clip.mp4` (with `thumb-1.jpg` and the
session journal `state.json`); the watcher's live state is `$XDG_RUNTIME_DIR/vms/pir-state.json`.
Each file in a session folder has one writer (plan §3.6): the uploader's are `uploaded.json`
and `cloud-request.json`. The watcher mirrors the clips into `pir-local` through
`adapter/pir_cloud.py`, by reconciliation: what it last wrote is in
`~/.local/state/vms/pir-cloud-sync.json`, and deleting that file only causes one rewrite.

### Deploying a Lambda

> **This AWS account is shared with the successor repo (`VideoSafeZone`).** Since 2026-10-03
> this repository has its **own copies** of every function its page uses, named `pir-<name>`,
> behind its own API `pir-api` (`PIR-MQTT-VMS-PI4.md` §3.9, Phase 9). **The unprefixed
> functions are the successor's** (and `pico2w-*` belong to the Pico firmware's own backend in
> `blink_freertos`): **never deploy them from here.** Doing that once silently
> replaced the successor's `record-clip` and client (`FoundAndFixed.md` #48). The one shared
> function this project still relies on is `clip-to-s3`, on cam-02's ONVIF event path (§4.4).

Deploy code with `cloud/deploy-pir.sh`. It refuses any name without the `pir-` prefix, checks
every name before deploying any, and builds the zip in a temporary directory:
```bash
cloud/deploy-pir.sh pir-list-clips pir-control      # pir-<name> <- cloud/lambda/<name with _>.py
cloud/deploy-pir.sh all                             # every function in cloud/pir-stack.json
```
`cloud/pir_stack.py` creates the `pir-` resources (table `pir-local` with its sparse index
`upload-requests`, the roles, the functions,
the API, the app client `pir-web`, the `PirLocal` policy on the device role, and the page's
private bucket and CloudFront distribution), idempotently,
and records their IDs in `cloud/pir-stack.json`; `--check` only reports. Run it with the
operator's AWS CLI credentials: `venv-adapter/bin/python3 cloud/pir_stack.py`.

Function names use hyphens (`pir-get-hls-url`), files use underscores (`get_hls_url.py`). To
check what is actually deployed, compare hashes: `aws lambda get-function-configuration
--function-name <name> --query CodeSha256` is `base64(sha256(zip bytes))`, so it matches a
local zip only if it was deployed from that exact file. The same code zipped at another time
hashes differently, because a zip stores timestamps. Zips are never tracked (`.gitignore`).

IAM policy documents for each function's role live in `cloud/iam/`, **as documentation only;
nothing applies them** except `pir_stack.py` for the `pir-` roles, which uses the same files as
the originals, so a copy's permissions equal the original's. Widening a permission means `aws
iam put-role-policy` against the live role, so the file and the deployment drift apart
silently. After any such change, run `cloud/iam/check-drift.sh` (semantic diff of every file
against the attached policy, the `pir-` roles included; `--pull` rewrites the files from what
is deployed). `FoundAndFixed.md` #41.

### Deploying the browser client

Since 2026-10-03 (`PIR-MQTT-VMS-PI4.md` Phase 11) this repository's page lives in **its own
bucket `pir-client-596633517506`** behind **its own distribution**, and talks to `pir-api` and
the `pir-web` app client:
```bash
cloud/deploy-pir.sh client
```
Served at **https://d10sy0s307vyid.cloudfront.net** (distribution `E53AGX9O0GDTV`, OAC; the
bucket is private and readable only by that distribution). The script refuses any bucket not
named `pir-client-…` and any page whose `API`/`COGNITO_CLIENT_ID` aren't the ones in
`cloud/pir-stack.json`, and it uploads with `Cache-Control: no-cache, must-revalidate`, which
makes CloudFront revalidate on every request, so an upload is live immediately.

> **Never write to the shared bucket `vms-demo-client-596633517506`** (served at
> `https://dugyd3kkt36pw.cloudfront.net`, distribution `E1B12167KKII6B`). Its `index.html` is
> the successor's, and with versioning disabled an overwrite can't be rolled back: deploying
> this repository's page there once reverted the successor's (`FoundAndFixed.md` #48).

### systemd — two separate managers, easy to mix up

**System units** (root, `/etc/systemd/system/`, need `sudo systemctl`): `kvs-cam01.service`,
`kvs-cam02.service`, `kvs-cam@.service` (template for GUI-provisioned cameras, §16.6) — the
actual KVS producers that cost money while running. Since 2026-10-03 there is also
`mosquitto.service`, from the Debian package rather than A8: the LAN MQTT broker on port
1883 for the PIR trigger (`LAUNCH.md` A10, `PIR-MQTT-VMS-PI4.md` Appendix B): the Pico
publishes to it and `kvs-pir-watcher` subscribes. It costs nothing to leave running.

**User units** (`~/.config/systemd/user/`, `systemctl --user`, no sudo): `kvs-mediamtx`,
`kvs-camera-init`, `kvs-camera-publish`, `kvs-agent`, `onvif-admin`,
`kvs-event-watcher` (ONVIF detection → clips), `kvs-outage-buffer` +
`kvs-outage-uploader` (durable outage buffering, `OUTAGE.md`), `kvs-pir-watcher` (PIR motion →
local clips on the USB stick, `PIR-MQTT-VMS-PI4.md` Phases 6 and 10; local recording makes no AWS
call and idles while no camera has `pirRecording` on, while a separate thread reports status and
the clip index to `pir-local`, best effort).

One more is **enabled on this Pi but is not part of the system**: `onvif-observe` runs
`adapter/observe_events.py --hours 9.5`, the Phase-0 event-observation harness behind
`Camera-Features.md` §"event capture" and `COSTS-1.4.md`'s duty-cycle figures. It restarts
at every boot and **appends to a git-tracked file**
(`measurements/events-cam-02-2026-09-08-overnight.jsonl`), which is why that file shows as
modified after any long uptime. Disable it (`systemctl --user disable --now onvif-observe`)
unless a measurement is actually wanted.

A unit in one manager **cannot** `Requires=`/`After=` a unit in the other — they're
independent systemd instances. (A templated system unit once declared
`Requires=kvs-mediamtx.service`, a user unit, and failed with "Unit not found" — see
guide §16 for the fix, which was simply to drop the cross-manager dependency.)

Unit files are **not in git**. `LAUNCH.md` A8 generates all of them, writing this clone's
literal absolute paths into `ExecStart=`/`WorkingDirectory=`/`Environment=`. systemd
never reads `.bashrc` and doesn't expand `$VMS_HOME` or `~`. When the clone moves, re-run A8.

Full launch sequence, order, and startup gotchas: `LAUNCH.md` Part B.

### Configuration: paths from the repo, identity from one file

Two separate things, two mechanisms — don't hardcode either.

**Deployment identity** (AWS region, IoT Thing, role alias, both IoT endpoints, evidence
bucket) lives in `/etc/adapter/adapter.env`, read by `adapter/config.py` in Python and
`adapter/bin/adapter-config.sh` in shell. Both take an already-set environment variable
over the file, and **neither has defaults** — a missing key raises `ConfigError` naming
the key and the file, rather than quietly talking to whichever account a stale default
points at. New code that needs one of these values imports `config`; it does not add a
literal. Template: `config/adapter.env.example` (`LAUNCH.md` A9, `FoundAndFixed.md` #25).

The same file carries four **optional** keys for the local MQTT broker (`MQTT_HOST`,
`MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD_FILE`, PIR plan). Only the code that talks to the
broker reads them, with `config.get()` at use, never as module-level constants in
`config.py`; that is what keeps every other process working on a Pi without a broker. The
password itself is not in the file: `MQTT_PASSWORD_FILE` names a separate file, readable by
root and the VMS user only (`PIR-MQTT-VMS-PI4.md` Appendix B.7).

### Paths: never hardcode the clone location

The repo has lived at more than one path (`~/MyProjects/VMS`, now
`~/Projects/VideoSafeZone`), and hardcoded `/home/...` paths broke on the move. Code in
`adapter/` resolves the root as `$VMS_HOME` if set, else from its own location. Python
modules in `adapter/` use
`os.environ.get("VMS_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`.
Scripts in `adapter/bin/` use
`VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"`.
Derive the venv's `pythonX.Y` directory from `sys.version_info` rather than spelling it
out. Follow the same pattern in new code; don't rely on `VMS_HOME` being exported, since
systemd units don't have it.

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

**Headless Chromium renders blank on this Pi** (empty screenshots, a 40-byte DOM). To check a
page or a `<video>` in a real browser without a desktop, drive **headless Firefox through
Marionette**: `firefox --headless --marionette --no-remote --profile <tmpdir>`, then
length-prefixed JSON commands on `127.0.0.1:2828` (`WebDriver:Navigate`, `ExecuteScript`,
`TakeScreenshot`) from stdlib Python. That's how the PIR pages were verified
(`PIR-MQTT-VMS-PI4.md` Phases 8 and 11). Scripts see the page's functions and DOM but not its
top-level `let`/`const` variables, so read state from the DOM. This Firefox decodes H.265.

## Architecture

### Two independent camera pipelines converge on MediaMTX

`cam-01` is a USB webcam (MJPG-only) that must be transcoded; `cam-02` (and any camera
added later) is a real ONVIF/RTSP IP camera that passes through untouched.

**Passthrough means the camera's codec, and that can be H.264 or H.265.** A passthrough
producer does not assume: `adapter/bin/stream-codec.py <cameraId>` asks MediaMTX which
video track it is actually receiving and prints `VIDEO_CODEC=h264|h265`, and
`video_depay_chain` in `producer-lib.sh` turns that into the depayloader chain
(`rtph265depay ! h265parse ! video/x-h265,stream-format=hvc1` vs the H.264 `avc` form).
The wire is the authority because a camera's encoder is changed *on the camera*, so any
registry field can be stale — the registry's `videoCodecActive` is written *from* this
reading, not consulted for it. `stream-format=hvc1`, not byte-stream, for the same reason
the H.264 side uses `avc`: kvssink takes codec private data only from the caps'
`codec_data`, so Annex-B would ingest happily and then refuse to play back. No source
within the wait is a deliberate hard failure, so systemd retries. `cam-01` is H.264-only
(we encode it) and keeps its own chain. Both publish
into **MediaMTX**, which is the local hub for everything downstream: it re-serves RTSP,
exposes its own local HLS output (port 8888, LAN-reachable — used only by the ONVIF admin
GUI's browser-side preview, never the cloud path), and exposes a control API (port 9997,
localhost-only) used to add camera paths live without a config-file rewrite + restart
(which would otherwise drop every other camera's connection). `cam-01`'s pipeline (`adapter/bin/publish-cam01.sh`) is built from
`/etc/adapter/cameras/cam01.env` (template `config/cameras/cam01.env.example`), not from
literals: `CAPS`, `CAM_FPS_OUT`, `VIDEO_BITRATE`, `GOP`, `H264_PROFILE`/`LEVEL`,
`AUDIO_RATE` and the v4l2 control lists all default to the PW310 values the script used to
carry, so an absent file changes nothing. The decode stage follows `CAPS` (`image/jpeg` →
`v4l2jpegdec`; `video/x-raw` → none), and `camera-init.sh` applies only the controls the
attached camera actually has, so a different webcam does not abort it. It
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
boundary. With audio off, `publish-cam01.sh` is byte-for-byte the pre-audio pipeline; the
**producer** scripts are deliberately not, since #43b — their video-only branch links the
source's audio pad to a `fakesink` rather than leaving it unlinked, because an unlinked
pad can abort the whole pipeline with `not-linked (-1)` depending on pad order
(`FoundAndFixed.md` #43).

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
it work and are easy to undo by accident: `recordDeleteAfter` is `0s` **permanently** on the
stick ring (the supervisor owns retention — handing it to MediaMTX's cleaner would let one
raced tick delete the captured outage; only the PIR ring in RAM gets the cleaner, as a 10-minute
backstop against a dead supervisor filling the runtime tmpfs), and **no MediaMTX API call
happens at outage onset**, because
patching any record field rebuilds the recorder and puts a keyframe seam exactly at T0.
Measured effect on a 5-minute outage: gap-fill 27.4% → 99.8%.

Since 2026-10-03 the same supervisor is also the **only owner of recording for the PIR
trigger** (`PIR-MQTT-VMS-PI4.md` §3.6): a camera with `pirRecording` on is armed **whether
or not its producer runs**, so the producer gate's flash protection doesn't apply. That is
why, since Phase 12, **a camera armed for PIR records into RAM** (`$XDG_RUNTIME_DIR/vms/ring/`)
and each session's segments are **staged in RAM** as hard links
(`$XDG_RUNTIME_DIR/vms/pir/<sessionId>/`); the watcher merges from there, so the stick receives
only the finished clip. Copying segments to the stick instead measured 3.3× the kept footage.
An outage-only camera keeps its ring on the stick, unchanged. **Still keep `pirRecording`
off when nobody is around** until Phase 12's postponed one-week soak has run. Everything that
reads the ring
looks in both places (`ring_segments()`); moving a segment out of RAM is a durable copy, so the
outage path still makes no MediaMTX call at T0. A session not yet merged is lost on a reboot
(accepted), and one unmerged 10 min after capture is moved to the stick. Only cameras armed
*for the outage* join an outage capture: a PIR-only camera has limit 0, which `Capture` reads
as "no limit".

A separate KVS producer process per camera (`kvs-cam01.service` / `kvs-cam02.service` /
future `kvs-cam@<id>.service` instances) pulls from MediaMTX's RTSP and pushes to its own
Kinesis Video Stream. None of the three producer scripts `exec gst-launch` directly: they call
`producer_run` from `adapter/bin/producer-lib.sh`, which treats *any* end of the pipeline as
a failure and additionally watches its output for "the source closed the session" lines,
killing a pipeline that hangs instead of exiting. Both halves exist because a producer that
loses its source otherwise stays `active` while uploading nothing — see `FoundAndFixed.md`
#44 and #46 before simplifying it back to an `exec`. The units carry
`RuntimeDirectory=vms-producer` for that watchdog's pipe. **`publish-cam01.sh` is the
exception and still uses `exec`** — it is the publisher, not a producer, but it has the
same exposure (MediaMTX restarts → clean EOS → exit 0 → `Restart=on-failure` ignores it),
and the successor has not converted it either. Known gap, not an oversight. It can also
**hang without exiting**: on 2026-10-03 it stalled a second after starting at boot and stayed
`active`, sending nothing, for 7.5 hours, with no error and no EOS (`FoundAndFixed.md` #51,
open; `systemctl --user restart kvs-camera-publish` recovers it). Check `bytesReceived`
advancing before trusting cam-01. **This is the layer Start/Stop buttons (in either GUI) actually
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

`cam-01`'s row also carries hardware facts written by the admin GUI's USB scan
(`audioCapable`, `cameraModel`, `cameraSerial`, `usbCaps`, `usbCapsProbedAt` — guide
§22.6). `audioCapable` is the load-bearing one: it gates the producer's audio and both
GUIs' audio checkbox, so a webcam with no microphone must set it false. **Pipeline
settings never go in the registry** — they live in `/etc/adapter/cameras/cam01.env`,
because a producer has to start with AWS unreachable.

Since 2026-10-03 cam-01's row also has `pirTopic` (the PIR sensor's MQTT topic, the
capability) and `pirRecording` (the switch, written by the admin GUI), from the PIR plan.
The switch is **deliberately a separate field, not a `recordingMode` value**: the table is
shared with the successor, whose code knows exactly four modes, and a new field is
invisible to it where a fifth mode would not be (`PIR-MQTT-VMS-PI4.md` §4.2–§4.3).
The outage supervisor arms the ring on `pirRecording` and the PIR watcher acts on motion (§3.6).

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
control where applicable. Since Phase 11 it is this project's own page, on `pir-api`: cameras
with a PIR sensor (from `GET /pir`) also get the PIR switch, the Pi's live status and the
"Motion clips on the Pi" list with Upload to AWS, and Evidence clips badge `pir` clips. Both
lists page by 10 with the successor's pager (page numbers styled as links, the count always
shown); motion clips deleted on the Pi are hidden behind a "show" link and never listed once
their `ttl` has passed; `list_clips.py` carries the successor's paging line for line, so `pir-list-clips` and the
shared `list-clips` answer every request identically. Clips
still on the Pi can't be played from the cloud (no inbound connections); an upload makes
them playable.

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
`systemctl is-active`-backed status column, and the local-HLS preview mentioned above). For
cameras with a PIR sensor it also has a "PIR trigger" switch (`POST /api/cameras/<id>/pir`,
which writes only `pirRecording`, and writes nothing when nothing changes), a **PIR sensor**
panel (section 5: the Pico's state, recent events with their lateness, the retrigger mode) and
a **PIR clips** list (section 6: 10 per page with the same pager as the cloud page; play from
the stick, with "Stop & close player"; keep, delete, Upload to AWS), all over
the watcher's heartbeat and the session folders, with no AWS call except the upload itself.
Anything the page *polls* must not depend on AWS: the camera-status poll checks IDs against the
supervisor's registry cache, because a DynamoDB lookup per poll froze the page during a DNS
failure (`FoundAndFixed.md` #55).

It also scans the Pi's **own USB bus** (`GET /api/usb-cameras` → `adapter/usb_camera.py`,
guide §22): formats, resolutions, frame rates, v4l2 controls and the microphone on the
same USB device, all read from the driver so a swapped-in webcam shows its own
capabilities rather than the PW310's. Read-only and safe while streaming. The panel
renders the `/etc/adapter/cameras/cam01.env` it would write and can **apply** it:
`adapter/bin/configure-camera.sh` (via `sudo`, like `provision-camera.sh`) validates every
line against a whitelist, backs up the previous file, and writes atomically; the GUI then
restarts the camera units and **requires MediaMTX's `bytesReceived` to advance** before
calling it a success — `ready` alone is not proof, a stalled pipeline reports it happily.
If video does not return within 30 s the previous configuration is restored automatically.
Guide §22.5.

Camera controls (exposure, white balance, focus) are **LAN-side only, deliberately** —
like the codec, they are a property of a camera on this Pi, so they stay out of the cloud
client.
Each control has **Set now** (writes it live while streaming; lost on the next start,
because `camera-init.sh` reapplies the file) and is saved by Apply. Every write is read
back, since the driver clamps instead of refusing. Guide §22.7.

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
