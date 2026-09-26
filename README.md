# Cloud Video Adapter

A Raspberry Pi 4B pulls video from local cameras, pushes it to AWS, takes commands back,
and serves a browser client from anywhere — with **zero inbound ports opened** on the home
router.

Every arrow crosses the router outbound-initiated. There is no port forward, no VPN, no
dynamic-DNS, and no inbound firewall rule anywhere in the system. That constraint is the
point: it is what makes a device deployable in a building whose network you do not
control.

This is a working build, not a sketch. Where a number appears below, it was measured on
the running system; where something does not work, it says so.

---

## Contents

- [Architecture](#architecture) · [Features](#features) · [Measured results](#measured-results)
- [Hardware](#hardware) · [Install](#install-on-a-raspberry-pi-4b-4-gb) · [Daily use](#daily-use)
- [Cost](#cost) · [Repository map](#repository-map) · [Documentation](#documentation)
- [Non-goals](#deliberate-non-goals) · [Rebuilding elsewhere](#rebuilding-on-a-different-aws-account)

---

## Architecture

```
  LAN (no inbound ports)                    │  every arrow outbound-initiated
                                            │
  cam-01  USB webcam ──MJPEG──┐             │
  (AVerMedia PW310)           │             │
                              ▼             │
                      ┌───────────────┐     │      ┌──────────────────────┐
                      │   MediaMTX    │─RTSP┼─────►│ Kinesis Video Streams│
  cam-02  ONVIF IPC ──┤  local hub    │     │      │  live HLS + clips    │
  (RTSP, H.264)       │  :8554 RTSP   │     │      └──────────────────────┘
                      │  :8888 HLS    │     │
                      │  :9997 API    │     │      ┌──────────────────────┐
                      └───────┬───────┘     │      │  IoT Core  MQTT:443  │
                              │             │◄────►│  start/stop/IR       │
                    ┌─────────▼─────────┐   │      └──────────────────────┘
                    │  USB outage buffer│   │
                    │  /mnt/vms-buffer  │───┼─────►┌──────────────────────┐
                    │  rolling 2 min    │backfill  │  S3 evidence clips   │
                    └───────────────────┘   │      │  + DynamoDB registry │
                                            │      └──────────────────────┘
                                            │                 ▲
                                            │      ┌──────────┴───────────┐
                                            │      │ API Gateway + Lambda │
                                            │      │ Cognito · CloudFront │
                                            │      └──────────┬───────────┘
                                            │                 │
                                                     browser client (anywhere)
```

**MediaMTX is the local hub.** Both cameras publish into it; everything downstream reads
from it. That is what lets the cloud producer, the local preview and the outage buffer be
independent of each other and of the cameras.

**Cost is a first-order constraint.** The KVS producer is *not* a background service — it
runs only when started, because that is what incurs `PutMedia` charges. Start/Stop in
either GUI controls exactly that process.

---

## Features

| | |
|---|---|
| **Multi-camera** | USB (transcoded) and ONVIF/RTSP (passthrough) side by side. Cameras come from a DynamoDB registry, not a hardcoded list — adding one works everywhere immediately |
| **Hardware encoding** | Pi 4's JPEG-decode, ISP-convert and H.264-encode blocks, ~2× CPU reduction vs software |
| **Live view** | Cloud HLS via KVS, Cognito-authenticated, served over HTTPS through CloudFront |
| **Evidence clips** | Manual recording, plus detection-triggered clips; browse, play, re-tier (Standard / IA / Deep Archive) and delete from the browser |
| **ONVIF detection** | Motion, cell-grid motion and human-shape detection drive automatic clip capture |
| **ONVIF device control** | IR-cut filter (day/night) from either GUI; WS-Discovery + one-click camera registration from a local admin app |
| **Optional audio** | Per-camera, off by default, for both cameras — [`AUDIO.md`](AUDIO.md) |
| **Durable outage buffering** | Records to a USB stick while AWS is unreachable and backfills on recovery — [`OUTAGE.md`](OUTAGE.md) |
| **Remote control** | Outbound MQTT over 443 (ALPN), so it traverses HTTPS-only firewalls |
| **Device identity** | One X.509 certificate and an IoT role alias — **no static AWS keys on the Pi** |

### Two GUIs, deliberately not one

**Cloud client** (`client/index.html`) — static, S3-hosted behind CloudFront,
Cognito-authenticated, reachable from anywhere. Live view, recording, clips, per-camera
settings.

**Local admin** (`adapter/onvif-admin/`) — a small Flask app on the Pi, LAN-only. It
exists because **WS-Discovery is UDP multicast and only works from the camera's own LAN
segment** — the cloud client and Lambda structurally cannot reach it.

---

## Measured results

The numbers this project exists to produce. Method and caveats in the linked docs.

| | Result |
|---|---|
| **Outage resilience** | A 5-minute WAN outage lost **72.6 %** of its footage before durable buffering, **0.2 %** after — gap-fill 27.4 % → **99.8 %** ([`measurements/reconnect_timeline.md`](measurements/reconnect_timeline.md)) |
| **Outage detection** | 4 s, against a 120 s pre-roll |
| **CPU, ONVIF passthrough** | ~3.5 % (no decode/encode stage at all) |
| **CPU, USB transcode** | 28 % → **13 %** by using the Pi's hardware V4L2 blocks instead of software elements |
| **Bitrate is driven by light, not motion** | Illumination moves bitrate **2.4–4.2×**; motion ~34 % ([`COSTS-1.4.md`](COSTS-1.4.md) §3) |
| **Duty cycle is the largest lever** | Measured 0.28 % overnight vs 12.8 % on a busy afternoon — ~20× |
| **Detection false positives** | Zero across 9.01 h of heartbeat-backed silence |

Three bugs worth knowing about, because they were all silent and none was caught by
"it ran without errors":

- `v4l2h264enc` negotiates **Baseline** H.264 even though the hardware default is High.
  `ffmpeg` played it; browsers rendered black.
- `kvssink` synthesises the DTS that GStreamer audio buffers lack from a counter **shared
  with the video track**, so the wrong audio sample rate silently loses half the audio.
- AAC sent over RTSP becomes LATM, which mangles the codec private data — KVS ingests it
  happily and then refuses to play it back.

The pattern behind all three: **KVS's ingest path is more permissive than its playback
path**, and `ffmpeg` is more permissive than a browser's MSE decoder. Decode a frame and
open a browser; "no errors" proves nothing.

---

## Hardware

| | |
|---|---|
| Raspberry Pi 4B | **4 GB** (what this was built on; 8 GB makes the SDK build easier) |
| Storage | SD card for the OS, plus a **USB stick** if you want outage buffering (57 GB here) |
| `cam-01` | Any UVC USB webcam (built on an AVerMedia PW310 — MJPG only, so it must be transcoded) |
| `cam-02` | Any ONVIF/RTSP camera that emits H.264 (tested: a Hikvision-derived rebrand) |
| Network | Wired or Wi-Fi; **no router configuration at all** |
| OS | Raspberry Pi OS (Debian trixie), 64-bit |

An AWS account is required. The demo runs well under $5/month; see [Cost](#cost).

**Nothing is pinned to a specific unit.** The camera's video node and microphone are
discovered at startup (`adapter/bin/resolve-usb-camera.sh`), because the old `by-id` path
embedded the camera's USB *serial number* and so broke on a different unit of the same
model. The ONVIF camera lives in the registry, not in code. Paths derive from
`$VMS_HOME`, falling back to the repo root the scripts live in. What genuinely does not
move is the AWS account — see [Rebuilding elsewhere](#rebuilding-on-a-different-aws-account).

---

## Install on a Raspberry Pi 4B (4 GB)

Full detail is in [`LAUNCH.md`](LAUNCH.md) Part A and the guide's §1–§8. This is the
shape of it, with the 4 GB-specific parts called out.

### 1. Stability hardening — do this *before* building anything

On 4 GB, the SDK build will OOM-kill the machine without this. It is not optional.

```bash
sudo apt install -y earlyoom
# prefer killing compiler processes over sshd/systemd
sudo systemctl enable --now earlyoom

sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile
sudo swapon -p 10 /swapfile
echo "/swapfile none swap sw,pri=10 0 0" | sudo tee -a /etc/fstab
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-low-swappiness.conf
```

Exact `earlyoom` arguments: `LAUNCH.md` A1.

### 2. Environment

```bash
export VMS_HOME=$HOME/Projects/VideoSafeZone   # wherever you cloned the repo
export KVS_SDK=$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp
export GST_PLUGIN_PATH=$KVS_SDK/build
export LD_LIBRARY_PATH=$KVS_SDK/open-source/local/lib:$LD_LIBRARY_PATH
export AWS_REGION=eu-central-1
```

Persist to `~/.bashrc`. **Note that systemd units do not source `.bashrc`** — every unit
sets the paths it needs explicitly, as literal absolute paths (`LAUNCH.md` A8). The
adapter's own scripts and Python modules don't need `VMS_HOME` exported: they fall back
to the repo root they live in.

### 3. Build the KVS Producer SDK — budget 1.5–2.5 hours

```bash
sudo apt install -y cmake m4 git build-essential pkg-config \
  libssl-dev libcurl4-openssl-dev liblog4cplus-dev \
  gstreamer1.0-plugins-base-apps gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gstreamer1.0-tools gstreamer1.0-rtsp \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
```

> `gstreamer1.0-omx-generic`, which the upstream instructions ask for, **does not exist on
> Debian trixie**.

**Three upstream source patches are required**, and they go in at two different moments,
so the build runs in two stages. The exact commands, with a proof for each stage, are in
`LAUNCH.md` A3:

1. **Before anything is built — patches 1 and 2.** They stop the nested OpenSSL build from
   running one compiler per core, which `-j1` and `-DPARALLEL_BUILD=OFF` don't reach, and
   from cloning OpenSSL's huge test submodules. Skip them and earlyoom kills the build
   mid-OpenSSL: `build.log` shows a burst of `Terminated signal terminated program cc1`.
2. **Stage 1: `cmake` configure.** With `-DBUILD_DEPENDENCIES=ON` this compiles the
   dependencies and downloads the kvspic source.
3. **Patch 3** (GCC 14 compatibility). Its file only exists after stage 1.
4. **Stage 2: `make -j1`.**

On 4 GB, build **single-threaded and pinned** (`taskset -c 1,2`), as a `systemd-run
--user` unit with lingering enabled, so it survives a dropped SSH or VS Code session.

**Checkpoint:** `gst-inspect-1.0 kvssink` prints element details, not "No such element."

### 4. Python environment

```bash
python3 -m venv "$VMS_HOME/venv-adapter"
"$VMS_HOME/venv-adapter/bin/pip" install boto3 awsiotsdk onvif-zeep-async WSDiscovery flask requests lxml
```

Proof that the venv works, and what each package is for: `LAUNCH.md` A4.

**MediaMTX** — download the pinned release binary into `$VMS_HOME/mediamtx/`, extracting
*only* the binary: the tarball's default `mediamtx.yml` would overwrite this repo's own
config (`LAUNCH.md` A5).

### 5. AWS resources

Created once per account, in this order — full commands in the guide's §3/§6/§8, every
policy document in [`cloud/`](cloud/):

KVS stream → IAM role `KVSAdapterRole` + role alias → IoT Thing + X.509 certificate +
thing policy → DynamoDB tables `cameras` and `clips` → Cognito user pool → Lambdas →
API Gateway → S3 + CloudFront for the client.

The certificate lands in `$VMS_HOME/certs/` and is **gitignored**. It is the only
credential on the device; everything else is vended short-lived through the role alias.
A fresh clone therefore has no `certs/`: copy it from the previous Pi or create a new
certificate for the existing Thing, then prove it against both AWS endpoints —
`LAUNCH.md` A7. The AWS CLI (the operator's tool, used for that and for deploys) isn't
in Raspberry Pi OS; install and credential it per `LAUNCH.md` A6.

### 6. Optional — USB buffer for outage recording

```bash
sudo mkfs.ext4 -m 0 -L vms-buffer /dev/sdX1
sudo mkdir -p /mnt/vms-buffer
# fstab by UUID, with nofail so a missing stick never blocks boot:
echo "UUID=<uuid>  /mnt/vms-buffer  ext4  defaults,noatime,nofail,x-systemd.device-timeout=10  0  2" \
  | sudo tee -a /etc/fstab
sudo mount /mnt/vms-buffer && sudo chown "$USER:$USER" /mnt/vms-buffer
mkdir -p /mnt/vms-buffer/live /mnt/vms-buffer/outage
touch /mnt/vms-buffer/.vms-buffer-ok        # sentinel: nothing arms without it
```

The sentinel is not belt-and-braces. If the stick is unplugged but the mountpoint still
exists, recording would land on the SD card — and with tens of GB free it *fits*, which is
worse than failing.

### 7. Start it

The unit files aren't in git — create them first with `LAUNCH.md` A8 (it writes the
literal paths of *this* clone into each unit, since systemd doesn't expand `$VMS_HOME`).

```bash
systemctl --user enable --now kvs-camera-init kvs-mediamtx kvs-camera-publish \
                              kvs-agent onvif-admin kvs-event-watcher \
                              kvs-outage-buffer kvs-outage-uploader
```

**A partial launch fails silently, not obviously.** Verify with `LAUNCH.md` Part C — every
unit can report `active` while the stream is dead.

---

## Daily use

```bash
# does media actually flow?  (`is-active` is not proof of anything)
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam01

# cloud path
ffprobe "$(aws kinesis-video-archived-media get-hls-streaming-session-url ... )"
```

- **Browser client** — the CloudFront URL. Sign in, Start a camera, watch, record, browse
  clips, set audio and outage buffering per camera.
- **Local admin** — `http://<pi-ip>:8080`. Discover and register ONVIF cameras, IR
  control, local preview, live `systemctl`-backed status.
- **Stop when finished.** `sudo systemctl stop kvs-cam01.service kvs-cam02.service` —
  these are what cost money.

Two systemd managers are in play and they cannot see each other: **system** units
(`kvs-cam0N.service`, the paid producers, need `sudo`) and **user** units (everything
else, `systemctl --user`). A unit in one cannot `Requires=` a unit in the other.

---

## Cost

[`COSTS-1.4.md`](COSTS-1.4.md) is authoritative. Headline, at 24 h retention:

| | one camera, 1 month |
|---|---|
| `cam-02` sub-stream (0.052 Mbps) | **~$0.16** |
| `cam-01` 720p15 (0.623 Mbps est.) | **~$1.87** |
| `cam-02` main 4 MP (1.211 Mbps est.) | **~$3.64** |
| Weekend testing, 6 h | **~$0.02** |

Keep a $10 monthly budget alarm. **The realistic failure mode is a producer left running
for a week, not a design error** — which is exactly why Start/Stop is an explicit action.

---

## Repository map

```
adapter/            on-device Python and pipelines
  agent.py            MQTT control agent (start/stop/IR)
  camera_control.py   shared camera logic; the hyphen-stripping helpers live here
  event_watcher.py    ONVIF detection → clip triggers
  outage_buffer.py    outage supervisor (arming, capture, retention)
  outage_uploader.py  merge + backfill to S3
  mediamtx_api.py     MediaMTX control-API helper
  onvif-admin/        local Flask GUI
  bin/                GStreamer pipelines + operator tools
client/index.html   the cloud browser client (single file)
cloud/lambda/       one file per Lambda
cloud/iam/          one policy document per role
cloud/iot/          IoT rules and thing policy
mediamtx/           MediaMTX binary and config
measurements/       recorded results, not prose
```

**A naming quirk worth knowing before reading the code:** the camera identifier is
hyphenated (`cam-01`) everywhere except MediaMTX path names and systemd unit suffixes
(`cam01`, `kvs-cam01.service`). `camera_control.py`'s `mediamtx_path_name()` /
`unit_name()` are the single conversion point — a naive `f"kvs-{camera_id}.service"` once
produced a nonexistent unit that `systemctl` silently no-op'd against.

---

## Documentation

| File | What it is |
|---|---|
| [`Demo-AWS-Video-revCosts4.md`](Demo-AWS-Video-revCosts4.md) | **The canonical build guide.** A narrative log of the real build, including bugs and how they were diagnosed. When in doubt about *why* something is built a certain way, read this |
| [`LAUNCH.md`](LAUNCH.md) | Operational runbook — what to run, in order, and how to verify |
| [`COSTS-1.4.md`](COSTS-1.4.md) | The cost model. Authoritative for any bitrate or dollar figure |
| [`Camera-Features.md`](Camera-Features.md) | What the ONVIF camera actually does, marked **verified** vs *advertised* |
| [`AUDIO.md`](AUDIO.md) | Optional audio: design, the two silent bugs, withdrawn claims |
| [`OUTAGE.md`](OUTAGE.md) | Durable outage buffering: design, measurements, open questions |
| [`OUTBOUND-CLOUD.md`](OUTBOUND-CLOUD.md) | The outbound-only architectural thesis |
| [`NETWORK.md`](NETWORK.md) | Network topology and the Wi-Fi/uplink trade-off |
| [`measurements/`](measurements/) | Raw recorded results |

`COSTS-1.3.md` and `Demo-AWS-Video-MCh-15.md` are superseded earlier revisions, kept for
the history of what changed and why.

---

## Deliberate non-goals

Scoping discipline, stated explicitly:

- **AI analytics.** Rekognition or a Greengrass inference component would demonstrate
  integration, not engineering.
- **Two-way audio (talkdown).** Needs a reverse audio path and the ONVIF backchannel; it
  argues for a WebRTC redesign rather than an addition.
- **Fleet OTA via IoT Jobs.** Scoped and specified (guide §16.3d), not built. Rollback is
  the part that matters, and half of it is worse than none.
- **NDAA compliance, enclosure, certification.** Product concerns, not architecture.

---

## Rebuilding on a different AWS account

Every AWS identifier in `LAUNCH.md` — account number, API Gateway ID, Cognito pool,
CloudFront distribution, bucket names, IoT endpoint — is specific to the account this was
built in. The commands as written will fail with "already exists" against that account and
will not work against another.

To rebuild: follow the guide's §3 (KVS + IAM + IoT), §6 (Lambda + API Gateway + Cognito)
and §8 (client + CloudFront), then replace the constants at the top of
`client/index.html`, `adapter/agent.py` and `adapter/bin/*.sh`. The policy documents in
[`cloud/iam/`](cloud/iam/) and [`cloud/iot/`](cloud/iot/) are reusable as-is apart from
the account number.
