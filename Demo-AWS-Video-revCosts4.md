# Demo: Cloud Adapter with Raspberry Pi 4B + AWS

A buildable, portfolio-grade implementation of the cloud-mediated outbound-connection
architecture described in `OUTBOUND-CLOUD.md`.

**Target:** a Raspberry Pi 4B acting as a *Cloud Adapter*. It pulls RTSP from a local
camera (simulated), pushes media outbound to Kinesis Video Streams, takes commands over
an outbound MQTT session to AWS IoT Core, and serves a browser client through
API Gateway — with **zero inbound ports opened** on the home router.

---

## 0. Target architecture

```text
                        ┌──────────────────────────── AWS eu-central-1 ────┐
                        │                                                  │
   ┌──────────┐         │   ┌──────────────┐        ┌───────────────────┐  │
   │  PW310   │  UVC    │   │  IoT Core    │        │ Kinesis Video     │  │
   │ USB cam  │────┐    │   │ MQTT 443 ALPN│        │ Streams "cam-01"  │  │
   │  MJPG    │    │    │   │  Shadow/LWT  │        │ retention 24 h    │  │
   └──────────┘    │    │   └──────▲───────┘        └────────▲──────────┘  │
   /dev/video0     │    │          │                         │             │
                   ▼    │          │ MQTT/TLS                │ PutMedia    │
   ┌────────────────────┴──┐       │ (outbound)              │ (outbound)  │
   │  Pi 4B  "raspi"       ├───────┘                         │             │
   │  192.168.178.53       ├─────────────────────────────────┘             │
   │ v4l2h264enc + MediaMTX│                                               │
   │  agent.py  (control)  │   ┌──────────────┐    ┌──────────────────┐    │
   │  gst + kvssink (media)│   │ API Gateway  │───▶│ Lambda           │    │
   └───────────────────────┘   │ + Cognito    │    │ get-hls-url      │    │
            │                  └──────▲───────┘    │ clip-to-s3       │    │
       Home router                    │            └────────┬─────────┘    │
       NAT, no port forward           │                     ▼              │
                                      │            ┌──────────────────┐    │
   ┌──────────────┐   HTTPS           │            │ S3 + DynamoDB    │    │
   │   Browser    │───────────────────┘            │ evidence clips   │    │
   │  hls.js UI   │                                └──────────────────┘    │
   └──────────────┘                                                        │
                        └──────────────────────────────────────────────────┘
```

Every arrow crossing the router is **outbound-initiated**. That is the whole thesis.

---

## 1. Prerequisites and cost guardrails

Do this before anything else. KVS bills on ingest **and** storage **and** consumption, and
a forgotten `gst-launch` running overnight is the classic way to get a surprise bill.

### 1.1 Budget alarm

```bash
aws budgets create-budget \
  --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --budget '{
      "BudgetName":"kvs-demo",
      "BudgetLimit":{"Amount":"10","Unit":"USD"},
      "TimeUnit":"MONTHLY",
      "BudgetType":"COST"
  }'
```

Add an SNS notification at 50 % in the console if you want the e-mail.

### 1.2 Cost arithmetic for this demo

Design the demo at **720p15 / 1.0 Mbps**, not 1080p30 (reasoning in §2.7):

```text
1.0 Mbps ÷ 8          = 0.125 MB/s
        × 3600        = 450 MB/hour ≈ 0.45 GB/hour
```

A weekend of testing in bursts — say 6 hours of actual streaming — is under 3 GB
ingested, a fraction of a GB-month stored at 24 h retention, and a few GB consumed
during playback. That lands in the low single-digit euros. Continuous 24/7 for a month
would be ~325 GB and a very different conversation — which is itself the point you want
to be able to make in an interview.

**Rules for the demo:**

- Retention on the stream: **24 hours**, not the default.
- Never leave the pipeline running unattended — use the systemd unit in §7 so `stop`
  actually stops it.
- Run `./teardown.sh` (§10) when finished for the day.

### 1.3 Environment

```bash
# On the Pi
export AWS_REGION=eu-central-1
export KVS_STREAM=cam-01
export THING_NAME=adapter-01
export VMS_HOME=$HOME/MyProjects/VMS
```

You already have the AWS CLI, an IoT Core endpoint and a working certificate/policy
model from the earlier telemetry pipeline. Reuse the account; create *new* Thing and
certificate for the adapter so the policies stay cleanly scoped.

**Repository root, not scattered `$HOME` clutter.** Every build artifact below — MediaMTX,
the KVS Producer SDK checkout, certificates, the Python venv, scripts — lives under
`$VMS_HOME` (`~/MyProjects/VMS`), not directly in `$HOME`. This keeps the whole prototype
in one git-tracked directory that matches §12's repository layout, makes teardown and
portfolio packaging exact, and means `certs/` and the venv can be gitignored in one place.
Where a command below still shows `~/something`, read it as `$VMS_HOME/something` unless
noted otherwise — the few exceptions (systemd `Environment=` lines) are called out
explicitly, since systemd doesn't expand `$VMS_HOME`.

### 1.4 System stability hardening (do this before §4's SDK build)

**Do this before attempting §4.** A Pi 4B running VS Code Remote-SSH's server stack
(1.3+ GB of Node processes: extension host, Pylance, Copilot) plus a from-source C++ build
is genuinely oversubscribed on 4 GB of RAM. Without the hardening below, the KVS SDK build
in §4.2 reliably crashed the whole Pi — not just failed the build — via three distinct
mechanisms discovered the hard way on 2026-08-20. Apply all four; each one closes a
different failure mode, not variations on the same one.

**1. `earlyoom` — intervene before the kernel's blunt last-resort killer does.**

```bash
sudo apt install -y earlyoom
sudo tee /etc/default/earlyoom > /dev/null <<'EOF'
EARLYOOM_ARGS="-r 60 -m 20 -s 95 --avoid '(^|/)(sshd|systemd|systemd-.*|init)$' --prefer '(^|/)(cc1plus|cc1|g\+\+|gcc|cpp|as|ld|make|cmake)$'"
EOF
sudo systemctl enable --now earlyoom
sudo systemctl restart earlyoom
```

`-m 20 -s 95` acts on low RAM alone (20% available), essentially ignoring swap level —
**this specific threshold matters**. An earlier, looser `-s 50` (act only once swap is
*also* below 50% free) was tried to rescue a few large individual compiles, and instead
let available memory crater from 64% to 12% in a single 60-second window before the
looser condition could catch it — a full crash. `-m20 -s95` reliably intervenes early
enough that it hasn't missed one since. `--avoid` protects `sshd`/`systemd` so remote
access survives even when something else is sacrificed; `--prefer` targets compiler
processes first, since a killed `cc1plus` just needs `make` re-run, unlike a killed VS
Code server process.

**2. Real swap headroom, tuned so it doesn't itself become the crash.**

```bash
sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile
sudo swapon -p 10 /swapfile
echo "/swapfile none swap sw,pri=10 0 0" | sudo tee -a /etc/fstab

echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-low-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-low-swappiness.conf
```

The default `zram` swap (RAM-compressed, ~2 GB) gives no headroom once RAM itself is
exhausted. A disk-backed swapfile does — but **heavy sustained swapping to an SD card is
itself a crash mechanism**: it happened once, mid-build, with no OOM-killer log and no
panic — just a silent cutoff, correlated with `brcmfmac` (WiFi driver) SDIO timeout
messages right before it. Low `vm.swappiness` keeps the kernel preferring fast page-cache
reclaim over slow disk I/O; the swapfile's low priority (`10`, vs. `zram`'s `100`) means
it's only ever a last resort. Verify with `swapon --show` — during a healthy build, the
disk swapfile should show `0B` used; if it starts climbing, expect trouble.

**3. Update the bootloader/EEPROM firmware — check even if the OS is current.**

```bash
sudo rpi-eeprom-update          # check
sudo rpi-eeprom-update -a       # stage the update
sudo reboot                     # required to apply
```

Found over a year out of date on this build (2025-05-08 installed vs. 2026-05-17
available) despite the OS itself being current — firmware updates track separately.
Raspberry Pi firmware releases regularly include power/USB/SDIO stability fixes.

**4. Keep heavy build load off the CPUs that service network interrupts.**

This kernel is tuned for real-time work — check your own `/proc/cmdline` for
`isolcpus=`/`irqaffinity=`; this Pi carries `isolcpus=1,2 irqaffinity=0,3`, meaning all
hardware interrupts (including the WiFi chip's) are confined to cores 0 and 3, while 1
and 2 sit isolated from the default scheduler. An unpinned build competes directly with
WiFi interrupt servicing on the same cores. Put the build on the isolated cores instead —
they're otherwise idle for our purposes, and `cpuset` isn't delegated to user cgroups on
this system, so `systemd-run --property=AllowedCPUs=` silently does nothing; use
`taskset`, which works via direct syscall affinity instead:

```bash
taskset -c 1,2 <your build command>   # inherited by all child processes via fork()
```

(This is folded into the `systemd-run` invocation in §4.2.)

**With all four in place:** the SDK build in §4.2 completed with zero crashes and zero
`earlyoom` interventions on its final, successful run — a build that had crashed the Pi
outright on five separate attempts beforehand.

---

## 2. Phase 1 — PW310 as the local camera

The AVerMedia PW310 replaces the synthetic source. This is better for the portfolio — it
exercises real V4L2 capture, real encoding on the adapter, and produces honest
glass-to-glass latency figures — but it introduces three problems the file loop didn't
have: **format**, **encoding**, and **exposure stability**.

### 2.1 Identify the device and its formats

```bash
lsusb | grep -i aver
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Read the output carefully; the whole phase branches on it.

| What you see | What it means |
|---|---|
| `MJPG` at 1280x720/30 | Expected. You must decode and re-encode to H.264. |
| `YUYV` only at high res | Uncompressed — USB 2.0 will cap you at ~640x480 or 10 fps. |
| `H264` listed | Jackpot: pass through with no encoding at all (see §2.5). |

The PW310 is a UVC device and almost certainly exposes MJPG + YUYV. At 720p30, YUYV is
~27 MB/s, which will not fit a USB 2.0 bus — so **MJPG is the working format**, and a
decode/encode step is unavoidable.

**Verified on the actual unit (2026-08-20):**

```text
[0]: 'MJPG' (Motion-JPEG, compressed)
        Size: Discrete 1280x720
                Interval: Discrete 0.033s (30.000 fps)   # only interval offered at 720p
[1]: 'YUYV' (YUYV 4:2:2)
        Size: Discrete 1280x720
                Interval: Discrete 0.125s (8.000 fps)    # confirms the USB2 bandwidth argument
```

No `H264` format at all, so the passthrough shortcut in §2.5 is not available on this unit.
More importantly: **MJPG at 1280x720 offers only 30 fps — no native 15 fps step at this
resolution** (lower resolutions do offer 15/10/5 fps). This means the 15 fps-at-source
branch of §2.5 does not apply here; the 30fps-capture-then-decimate branch is mandatory,
not a fallback. §2.5 below reflects that as the primary path.

### 2.2 Pin the device node

`/dev/video0` shifts when other devices enumerate — and on a Pi it will shift, because
`bcm2835-codec` claims `/dev/video10`–`/dev/video16`. Use the stable path:

```bash
ls -l /dev/v4l/by-id/
export CAM=/dev/v4l/by-id/usb-Generic_AVerMedia_PW310_Webcam_200901010001-video-index0
```

Put that in the systemd unit later, not `/dev/video0`.

**Verified on the actual unit:** the descriptor string is
`usb-Generic_AVerMedia_PW310_Webcam_200901010001-` (not the
`AVerMedia_TECHNOLOGIES__Inc._Live_Streamer_CAM_310_...` string an earlier draft of this
guide assumed — USB descriptor strings vary by firmware/vendor batch, so always confirm
with `ls -l /dev/v4l/by-id/` rather than copying a literal example). Two entries appear,
`-video-index0` and `-video-index1`; **use `index0`**, the capture node — `index1` is the
UVC metadata/still-image node, not a second video stream.

### 2.3 Lock exposure — this matters more than it sounds

UVC auto-exposure lengthens integration time in dim light, and the camera silently drops
from 30 fps to 15 or even 7.5 fps to accommodate it. If you don't lock it, your latency
measurements in §10 will drift with the daylight and your GOP-vs-latency graph will be
noise.

```bash
v4l2-ctl -d "$CAM" --list-ctrls
```

**Control names are camera-specific — don't copy a generic list blindly.** The PW310 on
this build exposes standard UVC controls, but with two differences worth checking for on
any camera before writing the lock command:

- **No `gain` control exists on this unit.** `exposure_time_absolute` is the only manual
  light-level lever available; don't reference a `gain` control that isn't there.
- **`exposure_dynamic_framerate`** is a real, separate control here (default: enabled) —
  this *is* the mechanism described above that silently drops fps in dim light. It must be
  disabled explicitly, in addition to switching `auto_exposure` to manual.

`exposure_time_absolute`, `white_balance_temperature`, and `focus_absolute` all start
`flags=inactive` until their parent auto-control is switched off — set the automatics
first, confirm the flags clear, then set the manual values in a second call:

```bash
# 1. disable the automatics (order matters — these gate the manual controls below)
v4l2-ctl -d "$CAM" \
  --set-ctrl=auto_exposure=1 \
  --set-ctrl=exposure_dynamic_framerate=0 \
  --set-ctrl=white_balance_automatic=0 \
  --set-ctrl=focus_automatic_continuous=0

# 2. confirm exposure_time_absolute / white_balance_temperature / focus_absolute
#    no longer show flags=inactive
v4l2-ctl -d "$CAM" --list-ctrls

# 3. now set the manual values (250 = 25ms, a reasonable indoor starting point;
#    this camera's range is 50-10000)
v4l2-ctl -d "$CAM" \
  --set-ctrl=exposure_time_absolute=250 \
  --set-ctrl=white_balance_temperature=4600 \
  --set-ctrl=focus_absolute=120

# confirm the camera is actually delivering 30 fps
v4l2-ctl -d "$CAM" --set-fmt-video=width=1280,height=720,pixelformat=MJPG \
                   --set-parm=30 --stream-mmap --stream-count=300 --stream-to=/dev/null
```

That last command prints a running fps figure — expect it to start low (0 dropped/warming
up) and converge toward 30 within the first ~10 frames; verified on this unit at
23.71 → 30.53 fps, settling at 30. If it settles below 30, the exposure is still too long —
lower `exposure_time_absolute` and add light.

This sequence is codified in `adapter/bin/camera-init.sh` — run once at boot, before the
publish pipeline starts, since these settings don't persist across power cycles.

### 2.4 Hardware H.264 encoding on the Pi 4B

The Pi 4B's VideoCore VI has an H.264 encoder exposed through V4L2 M2M as
`/dev/video11`, driven by the `v4l2h264enc` GStreamer element. Use it. Software `x264enc`
at 720p30 will eat 50–70 % of the Pi's CPU and leave nothing for the KVS producer.

```bash
sudo apt install -y gstreamer1.0-rtsp v4l-utils
gst-inspect-1.0 v4l2h264enc | head -20
v4l2-ctl -d /dev/video11 --list-ctrls-menus     # see the exact control names/enums
```

> **Note:** `omxh264enc` is gone from Bullseye onward, and the Pi 5 dropped the hardware
> encoder entirely. `v4l2h264enc` on a Pi 4B is the correct path — one more reason this
> demo belongs on the 4B.

**Verified working** on this build (kernel 6.18, Debian trixie): `v4l2h264enc` registers
at primary rank, and `/dev/video11` exposes exactly the controls the §2.5 pipeline needs —
`video_bitrate`, `h264_i_frame_period`, `repeat_sequence_header` — plus `h264_level`
already defaulting to `4`, matching the caps workaround below. One extra control worth
knowing about: **`video_gop_size`** (default `60`, separate from `h264_i_frame_period`).
The §2.5 pipeline doesn't set it, relying on `h264_i_frame_period` alone — if the §10.1
IDR-period sweep doesn't behave as expected, check whether `video_gop_size` needs setting
too.

### 2.5 Capture → encode → publish to MediaMTX

Keeping MediaMTX in the design is deliberate. It preserves the **RTSP boundary** that a
real Hikvision would present, so Phases 3–9 remain untouched and the topology still
matches `OUTBOUND-CLOUD.md` §19. The camera-facing side is the only thing that changed.

```bash
mkdir -p "$VMS_HOME/mediamtx" && cd "$VMS_HOME/mediamtx"
curl -L -o mediamtx.tar.gz \
  https://github.com/bluenviron/mediamtx/releases/download/v1.20.1/mediamtx_v1.20.1_linux_arm64.tar.gz
tar xzf mediamtx.tar.gz && ./mediamtx &
```

> **Known trap:** the asset naming above is what MediaMTX currently ships (verified
> 2026-08-20). An earlier draft of this guide referenced
> `mediamtx_linux_arm64v8.tar.gz` under `releases/latest/download/` — that name no longer
> exists; GitHub 404s past the redirect and `curl -L` silently writes a 9-byte "Not Found"
> body instead of failing loudly. If this breaks again, check the real asset names with:
> `curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep browser_download_url`

`adapter/bin/publish-cam01.sh` (paths below are relative to `$VMS_HOME`):

```bash
#!/usr/bin/env bash
CAM=/dev/v4l/by-id/usb-Generic_AVerMedia_PW310_Webcam_200901010001-video-index0

gst-launch-1.0 -v \
  v4l2src device="$CAM" ! \
  image/jpeg,width=1280,height=720,framerate=30/1 ! \
  jpegdec ! videorate drop-only=true ! video/x-raw,framerate=15/1 ! \
  videoconvert ! video/x-raw,format=I420 ! \
  v4l2h264enc extra-controls="controls,video_bitrate=1000000,h264_i_frame_period=30,repeat_sequence_header=1" ! \
  "video/x-h264,level=(string)4" ! \
  h264parse config-interval=-1 ! \
  rtspclientsink location=rtsp://127.0.0.1:8554/cam01 protocols=tcp
```

**Verified working** on this unit, 2026-08-20 — confirmed via Checkpoint 1 below.

**Two fixes against an earlier draft of this guide, both worth knowing generally, not just
for this camera:**

1. **The 30fps-capture-then-decimate branch is the primary path here, not a fallback.**
   §2.1 showed this PW310 offers no native 15 fps step at 1280x720 (only 30 fps at that
   resolution) — so the source-side-15fps variant an earlier draft led with doesn't apply.
   Always check your own `--list-formats-ext` output before assuming a camera offers your
   target fps natively; when it does, requesting it directly at capture does still halve
   USB bandwidth and encode load versus decimating after decode (§2.7 explains why 15 and
   not 30 or 10 fps is the target either way).
2. **`"video/x-h264,level=(string)4"` must be quoted in a shell script.** Unquoted, bash's
   lexer treats the bare `(` as a subshell opener mid-word and throws
   `syntax error near unexpected token '('` — this reproduces identically wherever this
   caps string appears unquoted (also present in §18.2's audio pipeline, fixed there too).
   It's specific to running the caps string from a script/non-interactive shell, not to
   this camera.

Three details that will cost you an hour each if missed:

- **`h264_i_frame_period=30`** — IDR every 2 s **at 15 fps**. The control is in *frames*,
  the latency is in *seconds*: change the frame rate and you must change this too, or your
  fragment duration silently doubles. This is the KVS fragment length and the dominant
  term in your end-to-end latency — the variable you sweep in §10.1.
- **`repeat_sequence_header=1`** — puts SPS/PPS in front of every IDR. Without it,
  fragments arrive without codec private data and KVS rejects them.
- **`video/x-h264,level=(string)4`** — a well-known caps-negotiation workaround for
  `v4l2h264enc`. Drop it only if negotiation succeeds without it.

**Software fallback** if `v4l2h264enc` misbehaves on your kernel:

```bash
  ... ! videoconvert ! x264enc speed-preset=ultrafast tune=zerolatency \
        bitrate=1000 key-int-max=30 ! ...
```

Measure both (§10.3) — the CPU delta is a genuinely interesting result for an embedded
engineer, not filler.

**If §2.1 showed native `H264`,** skip decode and encode entirely:

```bash
gst-launch-1.0 v4l2src device="$CAM" ! \
  video/x-h264,width=1280,height=720,framerate=15/1 ! \
  h264parse config-interval=-1 ! rtspclientsink location=rtsp://127.0.0.1:8554/cam01
```

You then lose control of the GOP unless the camera exposes `h264_i_frame_period` as a
UVC control — check before committing to this path.

### 2.6 Verify before touching AWS

```bash
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam01
ffplay  -rtsp_transport tcp -fflags nobuffer rtsp://127.0.0.1:8554/cam01
```

Expect `Video: h264`, 1280x720, 15 fps. If this is wrong, everything downstream fails in
confusing ways.

**Checkpoint 1 — met, verified 2026-08-20:**

```text
Stream #0:0: Video: h264 (Baseline), yuv420p(progressive), 1280x720, 15 fps, 15.17 tbr, 90k tbn
```

RTP session stats over the same window showed `packets-sent` climbing steadily and
`bitrate≈1.2 Mbps` (close to the configured 1.0 Mbps target plus RTP/RTCP overhead),
confirming sustained streaming rather than a single preroll frame.

One divergence worth noting: `/dev/video11`'s `h264_profile` control defaults to `High`,
but the negotiated stream came out **Baseline** — nothing in the pipeline explicitly
requests this, it's presumably `v4l2h264enc` negotiating down against the
`level=(string)4` caps filter. Not a problem — Baseline is actually the safer choice for
broad HLS/browser compatibility later — but worth knowing if you go looking for where
"High" went.

### 2.7 Frame-rate policy — why 15 fps

The Cloud Adapter Mini caps cloud upload at **10 fps**. That is not a hardware limit; it
is a bandwidth and storage decision taken across a large paying fleet. Copying it blindly
would be cargo-culting, and running 30 fps would be ignoring the single most important
economic lever in the system. **15 fps is the defensible middle**, for five reasons:

**1. It is an exact decimation of the source.** The PW310 delivers 30 fps; 15 is every
second frame. No resampling, no cadence jitter, no `videorate` duplicating frames to hit
a non-integer ratio. (10 fps is also exact from 30 — every third frame — so this argument
alone doesn't decide it.)

**2. Bandwidth falls, but not by half.** Expect **30–40 %**, not 50 %. Halving the frame
rate doubles the temporal distance between frames, so P-frames carry more residual and
cost more each; the I-frames, unchanged in size, also become a larger share of the
bitrate. Hence the pipeline drops from 1.5 Mbps to 1.0 Mbps rather than to 0.75 Mbps.
Being able to explain *why the saving is sublinear* is worth more than the saving itself.

**3. It doubles your channel count where it actually binds.** The VideoCore VI has one
H.264 encoder block shared across all channels (§16.6). Halving the frame rate roughly
halves the per-channel demand on it, and on `jpegdec` — which is the CPU hot spot in the
transcode path. This is the change with the largest effect on the §16.6 saturation
number.

**4. 10 fps looks broken in a live demo.** A pragmatic point, not an engineering one. A
reviewer watching you wave at the camera at 10 fps sees visible stutter and may read it
as a fault in your pipeline rather than a deliberate policy. 15 fps reads as smooth
enough to be intentional. Videoloft's users see 10 fps in a product context where it is
documented and expected; your demo has no such framing.

**5. It preserves evidentiary usefulness.** At 10 fps a person crossing the frame at
walking pace yields noticeably fewer usable frames, and fast motion — a vehicle, a
swinging door — can fall between samples. 15 fps gives more margin for the event-clip use
case in §9 while still being far from broadcast rates.

**The real answer is to parameterise it.** Put the frame rate in the channel config, and
report the curve rather than a single value:

| fps | `h264_i_frame_period` | Bitrate | GB/day/ch | CPU % (transcode) | Max channels |
|---|---|---|---|---|---|
| 30 | 60 | 1.5 Mbps | 16.2 | | |
| 15 | 30 | 1.0 Mbps | 10.8 | | |
| 10 | 20 | 0.8 Mbps | 8.6 | | |

Fill the last three columns from your own measurements. A candidate who can show that
table, and explain why the bitrate column isn't proportional to the first, has
demonstrated more than one who matched the product's number.

> **Keep the IDR period pinned to 2 seconds** whenever you change fps, or your latency
> measurements in §10.1 will move for reasons that have nothing to do with the variable
> you think you are testing.

### 2.8 Persist this across reboots — don't skip it

**A real incident (2026-08-20):** hours into working on later phases, "is the stream
alive?" got the answer "no" — not because of anything downstream, but because
`camera-init.sh` → MediaMTX → `publish-cam01.sh` (this whole section) had only ever been
run manually, never turned into a systemd unit. Everything built *on top* of it — the
`kvs-cam01.service` producer (§7.1), the control agent (§7.2) — was correctly persisted
and auto-recovering, but with nothing feeding local RTSP, `kvs-cam01.service` just
crash-looped (`Restart=on-failure`) against a 404 with nothing to show for it until
someone thought to check the bottom of the chain. **Persist all three pieces, not just
the cloud-facing ones** — a partial persistence story is worse than an obviously manual
one, because it fails silently instead of just not starting:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/kvs-camera-init.service <<'EOF'
[Unit]
Description=Lock PW310 exposure/WB/focus before streaming starts
Before=kvs-mediamtx.service

[Service]
Type=oneshot
ExecStart=/home/vladimir/MyProjects/VMS/adapter/bin/camera-init.sh
RemainAfterExit=yes

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/kvs-mediamtx.service <<'EOF'
[Unit]
Description=MediaMTX RTSP/HLS server for camera capture
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/vladimir/MyProjects/VMS/mediamtx
ExecStart=/home/vladimir/MyProjects/VMS/mediamtx/mediamtx
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/kvs-camera-publish.service <<'EOF'
[Unit]
Description=PW310 capture/encode -> publish to MediaMTX (rtsp://127.0.0.1:8554/cam01)
After=kvs-camera-init.service kvs-mediamtx.service
Requires=kvs-camera-init.service kvs-mediamtx.service

[Service]
Type=simple
WorkingDirectory=/home/vladimir/MyProjects/VMS/adapter/bin
ExecStart=/home/vladimir/MyProjects/VMS/adapter/bin/publish-cam01.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now kvs-camera-init kvs-mediamtx kvs-camera-publish
```

The `After=`/`Requires=` ordering on `kvs-camera-publish` matters — without it, systemd
may start the publish script before MediaMTX is actually listening, and you get the exact
same failure this section exists to prevent, just moved one layer down.

**Verify the whole local chain, not just that the units are "active":**

```bash
systemctl --user is-active kvs-camera-init kvs-mediamtx kvs-camera-publish
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam01   # the actual proof
```

---

## 3. Phase 2 — Create the KVS stream

```bash
aws kinesisvideo create-stream \
  --stream-name "$KVS_STREAM" \
  --data-retention-in-hours 24 \
  --media-type "video/h264" \
  --region "$AWS_REGION"

aws kinesisvideo describe-stream --stream-name "$KVS_STREAM" --region "$AWS_REGION"
```

Note the `StreamARN` — you need it for the IAM policies below.

**Checkpoint 2:** `describe-stream` returns `Status: ACTIVE`.

---

## 4. Phase 3 — Build the KVS Producer SDK on the Pi

This is the longest step. The SDK builds several open-source dependencies from source;
budget **45–90 minutes** on a Pi 4B under ideal conditions — see §4.2 for why the real
number is usually higher. **Apply §1.4's hardening first** — on this hardware, skipping
it meant this step crashed the Pi outright rather than just failing the build. Do it
once, then never again.

### 4.1 Dependencies

```bash
sudo apt install -y \
  cmake m4 git build-essential pkg-config \
  libssl-dev libcurl4-openssl-dev liblog4cplus-dev \
  gstreamer1.0-plugins-base-apps gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gstreamer1.0-tools gstreamer1.0-omx-generic libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev
```

### 4.2 Build

```bash
cd "$VMS_HOME/vendor" && git clone --recursive \
  https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp.git
cd amazon-kinesis-video-streams-producer-sdk-cpp
mkdir -p build && cd build

cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=ON -DPARALLEL_BUILD=OFF \
         -DCMAKE_BUILD_TYPE=Release
make -j1
```

**`--recursive` on the `git clone` is now a harmless no-op.** As of SDK v3.6.0,
`.gitmodules` is empty — dependencies are pulled via CMake during configure
(`-DBUILD_DEPENDENCIES=ON`), not git submodules. Safe to drop `--recursive`, but leaving
it in place doesn't hurt anything.

**`-DPARALLEL_BUILD=OFF` is not optional on a 4 GB Pi — capping `make -j` alone does
not protect you.** This cost real debugging time (2026-08-20), so the mechanism is worth
understanding: `CMake/Utilities.cmake`'s `build_dependency()` function — which drives the
from-source builds of `log4cplus`, `openssl`, `curl`, etc. — invokes each one via
`cmake --build . --parallel` with **no explicit job count**. A bare `--parallel` resolves
to `-j$(nproc)` as a literal command-line argument, which **always overrides** an
inherited `MAKEFLAGS` or `CMAKE_BUILD_PARALLEL_LEVEL` environment variable — so setting
those, or passing `-j1`/`-j2` only to the *outer* `make`, does nothing to constrain the
*dependency* sub-builds. On this Pi 4B (4 GB RAM), that let `log4cplus`'s own build spawn
**370 concurrent tasks** (it wasn't just compiling the library — it was also building
`log4cplus`'s entire bundled test suite in parallel: `fileappender_test`, `filter_test`,
`socket_test`, `unit_tests`, and a dozen more independent binaries, each spinning up its
own `cc1plus`). Combined with the VS Code Remote-SSH server's own memory footprint, this
reliably triggered repeated OOM kills and — before the hardening in §1 was in place —
full system reboots (kernel OOM-killer fired on `cc1plus`, followed by a silent
watchdog/brownout-style crash with no further log trace).

`PARALLEL_BUILD` is a real, supported CMake `option()` (`CMakeLists.txt` line 18,
`ON` by default) that gates that exact `--parallel` flag. Passing `-DPARALLEL_BUILD=OFF`
on the *outer* configure call propagates down and forces every dependency sub-build to
build single-threaded too. Verified fix: dependency build cgroup task count dropped from
**370 → 14**, and `earlyoom` interventions dropped from double digits per attempt to
**zero** for the remainder of the build.

**Budget more than the original 45–90 minutes if you apply this fix** — forcing every
dependency to build single-threaded (not just the outer SDK) is meaningfully slower, but
it is the difference between a build that completes and one that crashes the box partway
through. If `-DPARALLEL_BUILD=OFF` combined with `make -j2` for the *outer* build proves
stable on your hardware, `-j2` is worth trying before settling for full `-j1`
throughout — but validate it the same way: watch `earlyoom`'s kill count
(`journalctl -u earlyoom | grep -c 'sending SIGTERM to process'`) across the attempt, not
just whether it finishes.

**`-DPARALLEL_BUILD=OFF` only fixes the top-level `build_dependency()`. There is a
second, separate copy of the same function with the identical bug, and it isn't gated by
any option at all.** OpenSSL isn't built by the SDK's own top-level `CMakeLists.txt` —
it's built by a *nested*, independently-vendored dependency at
`dependency/libkvscproducer/kvscproducer-src/`, which has its own
`CMake/Utilities.cmake` with its own `build_dependency()`. That copy hardcodes
`cmake --build . --parallel` with **no `PARALLEL_BUILD` check whatsoever** — our outer
flag never reaches it. This is why `log4cplus` (built by the top-level function) respected
`-j1` cleanly while `OpenSSL`'s build still spawned **398 concurrent tasks** and crashed
the box, even with the top-level fix already applied. Patch it directly:

```bash
# in dependency/libkvscproducer/kvscproducer-src/CMake/Utilities.cmake, around line 89:
#   COMMAND ${CMAKE_COMMAND} --build . --parallel
# remove the trailing "--parallel" so it defaults to sequential:
sed -i 's/--build \. --parallel/--build ./' \
  "$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp/dependency/libkvscproducer/kvscproducer-src/CMake/Utilities.cmake"
```

**OpenSSL's `ExternalProject_Add` also fetches four large, unnecessary git submodules —
`boringssl`, `krb5`, `pyca-cryptography`, `wycheproof`.** These are OpenSSL's own
optional differential-fuzzing/test-vector dependencies, not required to build the library
`make install_sw` actually needs. Left unconstrained, cloning `boringssl` alone (a huge
repository) caused **repeated system crashes** during the clone/checkout itself — likely
sustained SD-card I/O pressure, the same failure mode as the swap-thrashing crash in §1.4,
just triggered by git instead of swap. Fix it in
`dependency/libkvscproducer/kvscproducer-src/CMake/Dependencies/libopenssl-CMakeLists.txt`
by adding one line to the `ExternalProject_Add(project_libopenssl ...)` block:

```cmake
ExternalProject_Add(project_libopenssl
    GIT_REPOSITORY    https://github.com/openssl/openssl.git
    GIT_TAG           OpenSSL_1_1_1t
    GIT_SHALLOW       TRUE
    GIT_PROGRESS      TRUE
    GIT_SUBMODULES    ""     # <-- add this: skips submodule init/update entirely
    PREFIX            ${CMAKE_CURRENT_BINARY_DIR}/build
    ...
```

**GCC 14 breaks the SDK's own source** — `Thread.c`'s use of `pthread_getname_np` (a
glibc/GNU extension) needs `_GNU_SOURCE` defined before `<pthread.h>` is included, which
this SDK version's source doesn't do. Older GCC only warned about the resulting implicit
declaration; **GCC 14 made implicit function declarations a hard error by default**, so
this fails the build outright on current Debian trixie. Fix it once, globally, rather than
patching individual source files as they're discovered — add to
`dependency/libkvscproducer/kvscproducer-src/dependency/libkvspic/kvspic-src/CMakeLists.txt`,
right after the `project(pic_project LANGUAGES C)` block's initial `add_definitions()` calls:

```cmake
if(UNIX AND NOT APPLE)
  add_definitions(-D_GNU_SOURCE)
endif()
```

**Run it detached, not in a plain background shell.** This step easily runs past any
single terminal/IDE session. A bare `&`/`nohup`/`disown` background job is not enough — it
is still tied to the login session's cgroup and gets killed the moment that session ends
(VS Code Remote-SSH reconnects, an SSH client disconnects, etc.), which silently discards
an hour of compilation. Use a `systemd --user` transient unit instead, with lingering
enabled once so it survives independent of any login session. Also pin it to the isolated
CPU cores (`taskset`) — see §1.4 for why this matters on this kernel's real-time tuning:

```bash
loginctl enable-linger "$USER"   # one-time; lets user services outlive your login session

systemd-run --user --unit=kvs-build --collect \
  --working-directory="$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp/build" \
  taskset -c 1,2 bash -c 'cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=ON \
             -DPARALLEL_BUILD=OFF -DCMAKE_BUILD_TYPE=Release > build.log 2>&1 && \
           make -j1 >> build.log 2>&1; echo "EXIT_CODE=$?" >> build.log'

# check on it any time, from any session:
systemctl --user status kvs-build
tail -f "$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp/build/build.log"
```

**If a retry is needed, don't wipe `build/` unless you have to.** `log4cplus` and
`OpenSSL` install to `open-source/local/` (a sibling of `build/`, not inside it), and
`build_dependency()` skips any dependency it can already `find_library()` there — so a
successful dependency survives `rm -rf build` and doesn't need to be rebuilt on the next
attempt. Only wipe `open-source/local/lib<name>` directly if a *specific* dependency's
build is stuck in a bad partial state.

**Realistic time budget, all fixes applied:** on a clean, uninterrupted run this is closer
to **1.5–2.5 hours** single-threaded than the original 45–90 minute estimate — genuinely
slower, but the difference between finishing and repeatedly crashing the Pi. Getting there
took several hours of debugging in practice, almost all of it the CMake/build-script fixes
above, not the compile time itself.

### 4.3 Register the plugin

```bash
cat >> ~/.bashrc <<'EOF'
export VMS_HOME=$HOME/MyProjects/VMS
export KVS_SDK=$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp
export GST_PLUGIN_PATH=$KVS_SDK/build
export LD_LIBRARY_PATH=$KVS_SDK/open-source/local/lib:$LD_LIBRARY_PATH
EOF
source ~/.bashrc

gst-inspect-1.0 kvssink | head -20
```

**Checkpoint 3:** `gst-inspect-1.0 kvssink` prints the element's properties rather than
"No such element".

---

## 5. Phase 4 — First light with static credentials

Get one frame into the cloud before adding the certificate machinery. Debugging two new
things at once is how afternoons disappear.

### 5.1 Temporary IAM user

Four steps: get your account ID, create the user, write the policy with that ID filled
in, attach it as an **inline** policy (owned by and deleted with the user — no separate
policy object to clean up later, which matches the "temporary" framing of this phase).

```bash
# 1. account ID, to fill in the policy's ARN (same pattern as §1.1's budget alarm) —
#    captured into a variable so step 3 substitutes it automatically, not a number to
#    copy-paste by hand
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "$ACCOUNT_ID"   # sanity-check it printed a real 12-digit account ID

# 2. the user
aws iam create-user --user-name kvs-demo-producer

# 3. the policy — <ACCOUNT> is a placeholder; envsubst-style substitution below fills in
#    the real value from $ACCOUNT_ID, nothing to edit by hand
cat > kvs-demo-producer-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "kinesisvideo:DescribeStream",
      "kinesisvideo:GetDataEndpoint",
      "kinesisvideo:PutMedia",
      "kinesisvideo:TagStream"
    ],
    "Resource": "arn:aws:kinesisvideo:eu-central-1:${ACCOUNT_ID}:stream/cam-01/*"
  }]
}
EOF

# 4. attach it
aws iam put-user-policy \
  --user-name kvs-demo-producer \
  --policy-name KVSProducerDemo \
  --policy-document file://kvs-demo-producer-policy.json
```

### 5.2 Run the pipeline

**The two `export` values below don't pre-exist — they come from creating an access key
for the user you just made.** That's a separate, explicit API call:

```bash
aws iam create-access-key --user-name kvs-demo-producer
```

which returns the `AccessKeyId` and `SecretAccessKey` as JSON. **The secret is shown
exactly once, at creation** — AWS never displays it again; lose it and you delete the key
and create a new one, there's no recovery. Export both directly in your shell — don't
write them to a file anywhere under `$VMS_HOME` (not even `certs/`, which is gitignored
for the X.509 material in §6, but isn't meant for this kind of credential either). They
only need to live in this one shell session:

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...

gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! kvssink stream-name="cam-01" \
            storage-size=128 \
            aws-region="eu-central-1" \
            framerate=25
```

`config-interval=-1` re-sends SPS/PPS on every IDR. Without it, the SDK may have no
codec private data at fragment boundaries and the fragments are rejected or unplayable.

### 5.3 Verify

Open the KVS console → `cam-01` → **Media playback**. You should see the test pattern
with the running clock within ~10 seconds.

**Checkpoint 4:** video visible in the console.

**Delete the IAM user's access keys once §6 works.** Do not leave them on the device;
that is exactly the anti-pattern the next phase exists to remove.

```bash
aws iam list-access-keys --user-name kvs-demo-producer   # confirm the AccessKeyId
aws iam delete-access-key --user-name kvs-demo-producer --access-key-id AKIA...
```

---

## 6. Phase 5 — Replace keys with X.509 (the important part)

This is what turns a demo into something you can defend in an interview: the adapter
carries **one identity** — its certificate — and derives temporary AWS credentials from
it. No long-lived secrets on the device.

### 6.1 IAM role the device will assume

Trust policy (`kvs-role-trust.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "credentials.iot.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

```bash
aws iam create-role --role-name KVSAdapterRole \
  --assume-role-policy-document file://kvs-role-trust.json

aws iam put-role-policy --role-name KVSAdapterRole \
  --policy-name KVSProducer --policy-document file://kvs-producer-policy.json
```

(`kvs-producer-policy.json` is the permission block from §5.1.)

### 6.2 Role alias

```bash
aws iot create-role-alias \
  --role-alias KVSAdapterRoleAlias \
  --role-arn "arn:aws:iam::<ACCOUNT>:role/KVSAdapterRole" \
  --credential-duration-seconds 3600
```

### 6.3 Thing, certificate, policies

```bash
aws iot create-thing --thing-name "$THING_NAME"

aws iot create-keys-and-certificate --set-as-active \
  --certificate-pem-outfile adapter.cert.pem \
  --public-key-outfile adapter.public.key \
  --private-key-outfile adapter.private.key
```

The IoT policy needs **both** the credentials-provider permission and the MQTT
permissions for Phase 6:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "iot:AssumeRoleWithCertificate",
      "Resource": "arn:aws:iot:eu-central-1:<ACCOUNT>:rolealias/KVSAdapterRoleAlias"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:eu-central-1:<ACCOUNT>:client/${iot:Connection.Thing.ThingName}"
    },
    {
      "Effect": "Allow",
      "Action": ["iot:Publish", "iot:Subscribe", "iot:Receive"],
      "Resource": "arn:aws:iot:eu-central-1:<ACCOUNT>:*/adapter/${iot:Connection.Thing.ThingName}/*"
    }
  ]
}
```

The `${iot:Connection.Thing.ThingName}` substitution is what makes this scale to 1000
adapters with one policy document — adapter-01 cannot touch adapter-02's topics.

### 6.4 Credentials endpoint and CA

```bash
aws iot describe-endpoint --endpoint-type iot:CredentialProvider
# → c2xxxxxxxxxxxx.credentials.iot.eu-central-1.amazonaws.com

curl -o cacert.pem https://www.amazontrust.com/repository/SFSRootCAG2.pem
```

Note this is a **different CA** from the one used for MQTT data-plane connections. Using
the wrong root certificate here produces a TLS error that looks like a permissions
problem and wastes an hour.

### 6.5 Pipeline with certificate auth

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! kvssink stream-name="cam-01" aws-region="eu-central-1" \
      iot-certificate="iot-certificate,endpoint=c2xxxx.credentials.iot.eu-central-1.amazonaws.com,cert-path=$VMS_HOME/certs/adapter.cert.pem,key-path=$VMS_HOME/certs/adapter.private.key,ca-path=$VMS_HOME/certs/cacert.pem,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"
```

**Checkpoint 5:** video still flows with no AWS keys anywhere on the device.

---

## 7. Phase 6 — Control plane over MQTT (port 443, ALPN)

Now the cloud can start and stop the media pipeline through the already-open outbound
session — §7 and §12 of the source document, made concrete.

### 7.0 Why 443 and not 8883

AWS IoT Core's native MQTT port is 8883. **Do not use it here.** Corporate and site
firewalls block 8883 often enough that a device depending on it generates support calls,
and an installer cannot promise it will be open. IoT Core also accepts MQTT on **443**
using the TLS ALPN extension (`x-amzn-mqtt-ca`), which is indistinguishable from ordinary
HTTPS to a firewall.

This is a deliberate alignment with the Videoloft Cloud Adapter Mini, whose published
protocol list is IPv4/IPv6, HTTP, HTTPS, RTSP, RTSPS, NTP — no MQTT port at all. Their
adapters multiplex control over the same HTTPS channel that carries media.

Setting `port=443` collapses this prototype's firewall requirement to **outbound 443
only**, matching that profile, while keeping the managed control plane (Shadow, Jobs,
LWT, per-thing policy) that a custom single-channel design would force you to build
yourself. See §16 for the full comparison and what it would take to converge further.

### 7.1 systemd unit for the pipeline

`/etc/systemd/system/kvs-cam01.service`:

```ini
[Unit]
Description=KVS producer for cam-01
After=network-online.target

[Service]
Type=simple
User=vladimir
Environment=GST_PLUGIN_PATH=/home/vladimir/MyProjects/VMS/vendor/amazon-kinesis-video-streams-producer-sdk-cpp/build
Environment=LD_LIBRARY_PATH=/home/vladimir/MyProjects/VMS/vendor/amazon-kinesis-video-streams-producer-sdk-cpp/open-source/local/lib
ExecStart=/home/vladimir/MyProjects/VMS/adapter/bin/stream-cam01.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`User=` and `Environment=` don't shell-expand `$VMS_HOME` — systemd unit files need the
literal absolute path, unlike the bash snippets elsewhere in this doc.

Put the `gst-launch-1.0` command from §6.5 into `$VMS_HOME/adapter/bin/stream-cam01.sh`.

**This unit's `Restart=on-failure` will crash-loop silently, with nothing to show for
it, if §2.8's three units aren't *also* persisted.** `kvs-cam01.service` is downstream of
local RTSP existing at all — it has no way to distinguish "camera pipeline never started"
from any other transient failure, so it just keeps retrying against a 404 indefinitely.
This is exactly how a real incident happened here (§2.8) — every cloud-facing piece was
correctly auto-recovering while the actual camera feed silently wasn't running at all.

### 7.2 Control agent

```bash
python3 -m venv "$VMS_HOME/venv-adapter" && source "$VMS_HOME/venv-adapter/bin/activate"
pip install awsiotsdk
```

`agent.py` — subscribe to a command topic, drive systemd, report state, and publish a
Last Will so the cloud sees ungraceful disconnects:

```python
import json, subprocess, threading
from awscrt import mqtt
from awsiot import mqtt_connection_builder

THING   = "adapter-01"
CMD_T   = f"adapter/{THING}/cmd"
STATE_T = f"adapter/{THING}/state"

def set_stream(on: bool):
    action = "start" if on else "stop"
    subprocess.run(["sudo", "systemctl", action, "kvs-cam01.service"], check=False)

def on_message(topic, payload, **kwargs):
    cmd = json.loads(payload).get("action")
    if cmd in ("start", "stop"):
        set_stream(cmd == "start")
        conn.publish(topic=STATE_T,
                     payload=json.dumps({"streaming": cmd == "start"}),
                     qos=mqtt.QoS.AT_LEAST_ONCE)

conn = mqtt_connection_builder.mtls_from_path(
    endpoint="xxxxx-ats.iot.eu-central-1.amazonaws.com",  # aws iot describe-endpoint --endpoint-type iot:Data-ATS
    port=443,                       # ALPN x-amzn-mqtt-ca — traverses HTTPS-only firewalls
    cert_filepath="/home/vladimir/MyProjects/VMS/certs/adapter.cert.pem",
    pri_key_filepath="/home/vladimir/MyProjects/VMS/certs/adapter.private.key",
    ca_filepath="/home/vladimir/MyProjects/VMS/certs/AmazonRootCA1.pem",
    client_id=THING,
    keep_alive_secs=30,
    clean_session=False,
    will=mqtt.Will(topic=STATE_T,
                   qos=mqtt.QoS.AT_LEAST_ONCE,
                   payload=json.dumps({"online": False}).encode(),
                   retain=False),
)
conn.connect().result()
conn.subscribe(topic=CMD_T, qos=mqtt.QoS.AT_LEAST_ONCE, callback=on_message)[0].result()
conn.publish(topic=STATE_T, payload=json.dumps({"online": True}),
             qos=mqtt.QoS.AT_LEAST_ONCE, retain=False)[0].result()
threading.Event().wait()
```

**`retain=True` on the Will gets the CONNECT itself rejected — `AWS_ERROR_MQTT_UNEXPECTED_HANGUP`.**
This cost real debugging time (2026-08-20): the exact same certificate, policy, and topic
work fine for a *normal* `publish()` call after connecting — the failure is specific to
declaring a **retained** Last Will at CONNECT time. AWS IoT Core appears to apply a
stricter authorization check to retained LWTs than to regular publishes, and with this
account's policy it fails outright rather than degrading gracefully — the client just
sees the TCP connection drop, with no CONNACK error code to explain why. Isolate this
class of bug the same way: strip the connection down to nothing (no Will, no subscribe)
and confirm that connects cleanly first; then add pieces back one at a time
(`will=` with `retain=False`, then `retain=True`) until the specific failing piece is
obvious, rather than debugging the full `agent.py` as one unit. `retain=False` is enough
for Checkpoint 6 — the LWT firing for anyone currently subscribed doesn't require message
retention, and enabling `awscrt.io.init_logging(awscrt.io.LogLevel.Debug, 'stderr')`
before building the connection is what actually surfaces `AWS_ERROR_MQTT_UNEXPECTED_HANGUP`'s
context (ALPN negotiation, CONNACK, or lack thereof) — the bare exception message alone
doesn't say enough to diagnose it.

Also note `conn.publish(...)` returns a **tuple** `(future, packet_id)` in this SDK
version (`awsiotsdk` 1.31.0 / `awscrt` 0.36.1), not a bare future — `.result()` needs
`[0]` first, or errors from the publish are silently swallowed.

The SDK's `mtls_from_path` connection already implements exponential-backoff reconnect
(§14 of the source doc) — you do not write that yourself, but you should be able to say
what it does.

Test from the console's MQTT test client:

```json
{"action": "start"}
```

**Verifying this properly needs a second, independent MQTT client watching the state
topic — and it can't just reuse the device certificate with a different `client_id`.**
The IoT policy's `iot:Connect` resource (`client/${iot:Connection.Thing.ThingName}`)
resolves against the Thing the certificate is attached to (`adapter-01`); connecting with
any other `client_id` on the same cert fails to authorize. Two ways around it: use the
console's MQTT test client (simplest, no code), or authenticate a second connection via
IAM/SigV4 over WebSockets instead of the certificate — this sidesteps the device policy
entirely and just needs the connecting IAM principal to have IoT permissions:

```python
from awscrt import mqtt, auth
from awsiot import mqtt_connection_builder

credentials_provider = auth.AwsCredentialsProvider.new_default_chain()
observer = mqtt_connection_builder.websockets_with_default_aws_signing(
    endpoint="xxxxx-ats.iot.eu-central-1.amazonaws.com",
    region="eu-central-1",
    credentials_provider=credentials_provider,
    client_id="observer-admin",
    clean_session=True,
)
observer.connect().result()
observer.subscribe(topic="adapter/adapter-01/state", qos=mqtt.QoS.AT_LEAST_ONCE,
                    callback=lambda topic, payload, **kw: print(topic, payload.decode()))[0].result()
```

**Checkpoint 6 — verify all three independently, not just the MQTT message:**

```bash
aws iot-data publish --topic "adapter/adapter-01/cmd" --region eu-central-1 \
  --cli-binary-format raw-in-base64-out --payload '{"action": "start"}'
```

- the observer above prints `{"streaming": true}`
- `sudo systemctl is-active kvs-cam01.service` independently reports `active` — the MQTT
  message and the real system state are two different things; check both
- `{"action": "stop"}` reverses both
- `kill -9 <agent PID>` makes `{"online": false}` appear on the observer within a few
  seconds, with no further action from you

### 7.3 Prove the 443 claim

Don't assert it — demonstrate it. Block 8883 outright and confirm the adapter still works.

**`iptables` isn't installed on current Raspberry Pi OS (Debian trixie) — it defaults to
`nftables` with no legacy compat shim present.** `sudo iptables -A OUTPUT ...` fails with
`command not found`, and because the command errors before doing anything, it's easy to
miss that **no block was ever applied** and mistake the following "still works" check for
a real proof rather than a false negative. Use `nft` directly:

```bash
sudo nft add table inet filter
sudo nft add chain inet filter output '{ type filter hook output priority 0; }'
sudo nft add rule inet filter output tcp dport 8883 drop

sudo systemctl restart kvs-agent.service    # or your agent's actual unit name
journalctl --user -u kvs-agent -f           # should connect normally, no errors

# confirm 8883 is genuinely dead — this should hang until it times out, not connect
timeout 5 bash -c 'echo | openssl s_client -connect xxxxx-ats.iot.eu-central-1.amazonaws.com:8883'

# confirm the negotiated ALPN protocol on 443
openssl s_client -connect xxxxx-ats.iot.eu-central-1.amazonaws.com:443 \
  -alpn x-amzn-mqtt-ca </dev/null 2>&1 | grep -i alpn
```

**Make the rule survive a reboot** — an ad-hoc `nft add` (like `iptables -A` would have
been) only lives in the running kernel's ruleset, and this demo box reboots more than
you'd like (§1.4). Write it into `/etc/nftables.conf` and enable the service so it's
reapplied at every boot:

```bash
sudo tee /etc/nftables.conf > /dev/null <<'EOF'
#!/usr/sbin/nft -f

flush ruleset

table inet filter {
	chain input {
		type filter hook input priority filter;
	}
	chain forward {
		type filter hook forward priority filter;
	}
	chain output {
		type filter hook output priority filter;
		# permanent proof for §7.3: the adapter needs nothing but outbound 443
		tcp dport 8883 drop
	}
}
EOF

sudo systemctl enable --now nftables
```

Leave that rule in place permanently on the demo box. It makes the firewall claim
unfalsifiable — the prototype provably needs nothing but outbound 443.

---

## 8. Phase 7 — Browser client

Reuse your existing API Gateway + Lambda pattern; only the payload changes. **The guide
text above stops at "reuse your existing pattern" — if this is your first API
Gateway + Cognito + Lambda stack, none of §8.2–§8.5 below is optional**; every piece was
built and verified end-to-end (2026-08-20), including a real `curl` test through Cognito
auth into a playable HLS URL, not just deployed-and-assumed-working.

### 8.1 Lambda `get-hls-url` (Python 3.13, arm64)

```python
import boto3, os, json

REGION = os.environ["AWS_REGION"]
STREAM = os.environ.get("STREAM_NAME", "cam-01")

def lambda_handler(event, context):
    kv = boto3.client("kinesisvideo", region_name=REGION)
    ep = kv.get_data_endpoint(
        StreamName=STREAM, APIName="GET_HLS_STREAMING_SESSION_URL"
    )["DataEndpoint"]

    kvam = boto3.client("kinesis-video-archived-media",
                        endpoint_url=ep, region_name=REGION)
    url = kvam.get_hls_streaming_session_url(
        StreamName=STREAM,
        PlaybackMode="LIVE",
        Expires=300,
    )["HLSStreamingSessionURL"]

    return {
        "statusCode": 200,
        "headers": {"Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"url": url, "expires_in": 300}),
    }
```

**The `/cmd` Lambda the client calls isn't shown anywhere in the source material** —
here's the actual implementation, `publish_cmd.py`:

```python
import boto3, os, json

REGION = os.environ["AWS_REGION"]
THING  = os.environ.get("THING_NAME", "adapter-01")
CMD_T  = f"adapter/{THING}/cmd"

def lambda_handler(event, context):
    body = json.loads(event.get("body") or "{}")
    action = body.get("action")
    if action not in ("start", "stop"):
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "action must be 'start' or 'stop'"}),
        }

    iot_data = boto3.client("iot-data", region_name=REGION)
    iot_data.publish(topic=CMD_T, qos=1, payload=json.dumps({"action": action}))

    return {
        "statusCode": 200,
        "headers": {"Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"published": action}),
    }
```

Deploy both as arm64/Python 3.13 zip packages, each with its **own** least-privilege
execution role (matches the scoping discipline used everywhere else in this guide — don't
give the URL-fetcher IoT permissions or the command-publisher KVS permissions):

```bash
cat > "$VMS_HOME/cloud/iam/lambda-trust.json" <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

cat > "$VMS_HOME/cloud/iam/get-hls-url-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["kinesisvideo:GetDataEndpoint", "kinesisvideo:GetHLSStreamingSessionURL",
               "kinesisvideo:DescribeStream"],
    "Resource": "$(aws kinesisvideo describe-stream --stream-name cam-01 --region eu-central-1 --query StreamInfo.StreamARN --output text)"
  }]
}
EOF

cat > "$VMS_HOME/cloud/iam/publish-cmd-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "iot:Publish",
    "Resource": "arn:aws:iot:eu-central-1:${ACCOUNT_ID}:topic/adapter/adapter-01/cmd"
  }]
}
EOF

for NAME_ROLE_POLICY in "GetHlsUrlLambdaRole:get-hls-url-policy.json:GetHlsUrlAccess" \
                        "PublishCmdLambdaRole:publish-cmd-policy.json:PublishCmdAccess"; do
  IFS=: read -r ROLE POLICY_FILE POLICY_NAME <<< "$NAME_ROLE_POLICY"
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "file://$VMS_HOME/cloud/iam/lambda-trust.json"
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  aws iam put-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME" \
    --policy-document "file://$VMS_HOME/cloud/iam/$POLICY_FILE"
done

sleep 8   # let IAM role propagate before Lambda creation references it — a fresh role
          # can 404 if you deploy the function immediately after creating it

cd "$VMS_HOME/cloud/lambda"
zip -q get_hls_url.zip get_hls_url.py && zip -q publish_cmd.zip publish_cmd.py

aws lambda create-function --function-name get-hls-url \
  --runtime python3.13 --architectures arm64 \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/GetHlsUrlLambdaRole" \
  --handler get_hls_url.lambda_handler --zip-file fileb://get_hls_url.zip \
  --environment "Variables={STREAM_NAME=cam-01}" --timeout 10 --region eu-central-1

aws lambda create-function --function-name publish-cmd \
  --runtime python3.13 --architectures arm64 \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/PublishCmdLambdaRole" \
  --handler publish_cmd.lambda_handler --zip-file fileb://publish_cmd.zip \
  --environment "Variables={THING_NAME=adapter-01}" --timeout 10 --region eu-central-1
```

### 8.2 Cognito user pool

```bash
POOL_ID=$(aws cognito-idp create-user-pool --pool-name kvs-demo-users \
  --region eu-central-1 --auto-verified-attributes email --query 'UserPool.Id' --output text)

CLIENT_ID=$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" \
  --client-name kvs-demo-web-client \
  --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --no-generate-secret --region eu-central-1 --query 'UserPoolClient.ClientId' --output text)
```

`--no-generate-secret` matters — a browser client can't keep a client secret
confidential, so this app client is deliberately public (the security boundary is the
username/password + the resulting short-lived JWT, same model as any SPA).

A test user, created and confirmed non-interactively for a demo (a real deployment would
use self-registration or an admin invite flow instead):

```bash
aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username demo-viewer \
  --user-attributes Name=email,Value=demo-viewer@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS --region eu-central-1

aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username demo-viewer \
  --password 'ChangeMe2026!' --permanent --region eu-central-1
```

### 8.3 API Gateway (REST API, Cognito authorizer, both routes)

```bash
API_ID=$(aws apigateway create-rest-api --name kvs-demo-api --region eu-central-1 --query id --output text)
ROOT_ID=$(aws apigateway get-resources --rest-api-id "$API_ID" --region eu-central-1 --query 'items[0].id' --output text)

AUTHORIZER_ID=$(aws apigateway create-authorizer --rest-api-id "$API_ID" \
  --name CognitoAuthorizer --type COGNITO_USER_POOLS \
  --provider-arns "arn:aws:cognito-idp:eu-central-1:${ACCOUNT_ID}:userpool/$POOL_ID" \
  --identity-source 'method.request.header.Authorization' \
  --region eu-central-1 --query id --output text)

for ROUTE in "hls:GET:get-hls-url" "cmd:POST:publish-cmd"; do
  IFS=: read -r PATH_PART METHOD FUNCTION <<< "$ROUTE"
  RES_ID=$(aws apigateway create-resource --rest-api-id "$API_ID" --parent-id "$ROOT_ID" \
    --path-part "$PATH_PART" --region eu-central-1 --query id --output text)

  aws apigateway put-method --rest-api-id "$API_ID" --resource-id "$RES_ID" \
    --http-method "$METHOD" --authorization-type COGNITO_USER_POOLS \
    --authorizer-id "$AUTHORIZER_ID" --region eu-central-1 > /dev/null

  aws apigateway put-integration --rest-api-id "$API_ID" --resource-id "$RES_ID" \
    --http-method "$METHOD" --type AWS_PROXY --integration-http-method POST \
    --uri "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:${ACCOUNT_ID}:function:${FUNCTION}/invocations" \
    --region eu-central-1 > /dev/null

  aws lambda add-permission --function-name "$FUNCTION" --statement-id apigateway-invoke \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:eu-central-1:${ACCOUNT_ID}:${API_ID}/*/${METHOD}/${PATH_PART}" \
    --region eu-central-1

  # CORS preflight — the client's fetch() calls carry an Authorization header, which
  # forces the browser to send an OPTIONS preflight first; skip this and every request
  # fails with a CORS error before it ever reaches Cognito or the Lambda
  aws apigateway put-method --rest-api-id "$API_ID" --resource-id "$RES_ID" \
    --http-method OPTIONS --authorization-type NONE --region eu-central-1 > /dev/null
  aws apigateway put-integration --rest-api-id "$API_ID" --resource-id "$RES_ID" \
    --http-method OPTIONS --type MOCK \
    --request-templates '{"application/json":"{\"statusCode\": 200}"}' --region eu-central-1 > /dev/null
  aws apigateway put-method-response --rest-api-id "$API_ID" --resource-id "$RES_ID" \
    --http-method OPTIONS --status-code 200 --response-parameters \
    '{"method.response.header.Access-Control-Allow-Headers":true,"method.response.header.Access-Control-Allow-Methods":true,"method.response.header.Access-Control-Allow-Origin":true}' \
    --region eu-central-1 > /dev/null
  aws apigateway put-integration-response --rest-api-id "$API_ID" --resource-id "$RES_ID" \
    --http-method OPTIONS --status-code 200 --response-parameters \
    "{\"method.response.header.Access-Control-Allow-Headers\":\"'Content-Type,Authorization'\",\"method.response.header.Access-Control-Allow-Methods\":\"'GET,POST,OPTIONS'\",\"method.response.header.Access-Control-Allow-Origin\":\"'*'\"}" \
    --region eu-central-1 > /dev/null
done

aws apigateway create-deployment --rest-api-id "$API_ID" --stage-name prod --region eu-central-1
```

Guard both routes with a **Cognito user pool authorizer** — the "authorization" and
"session management" bullets of §20 are otherwise just words on a slide.

**Verify the authorizer actually enforces something**, before touching the client at all:

```bash
API="https://${API_ID}.execute-api.eu-central-1.amazonaws.com/prod"
curl -s "$API/hls" -w "\n%{http_code}\n"     # expect 401, no token supplied

ID_TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id "$CLIENT_ID" \
  --auth-parameters USERNAME=demo-viewer,PASSWORD='ChangeMe2026!' --region eu-central-1 \
  --query 'AuthenticationResult.IdToken' --output text)
curl -s "$API/hls" -H "Authorization: $ID_TOKEN" -w "\n%{http_code}\n"   # expect 200 + a real HLS URL
```

If that second call 502s with "No fragments found in the stream," that's not a bug in
this stack — it means `kvs-cam01.service` isn't currently producing (§7's control agent
manages this; `PlaybackMode=LIVE` needs an actual live stream to return a session URL
against).

### 8.4 Client

Single HTML file, hls.js from CDN. **The source snippet references a bare `idToken`
variable that's never defined** — for the client to actually work, something has to
authenticate against Cognito and produce that token. Rather than pull in a full SDK just
for this, Cognito's `InitiateAuth` is a plain JSON HTTPS API — callable directly via
`fetch()`, keeping this a genuine single self-contained file:

```html
<div id="login">
  <input id="username" placeholder="username">
  <input id="password" type="password" placeholder="password">
  <button onclick="login()">Sign in</button>
</div>
<div id="app" style="display:none">
  <video id="v" controls autoplay muted playsinline width="960"></video>
  <button onclick="cmd('start')">Start</button>
  <button onclick="cmd('stop')">Stop</button>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
const API = "https://xxxx.execute-api.eu-central-1.amazonaws.com/prod";
const COGNITO_REGION = "eu-central-1";
const COGNITO_CLIENT_ID = "xxxxxxxxxxxxxxxxxxxxxxxxxx";
let idToken = null;

async function login() {
  const r = await fetch(`https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`, {
    method: "POST",
    headers: {"Content-Type": "application/x-amz-json-1.1",
              "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"},
    body: JSON.stringify({
      AuthFlow: "USER_PASSWORD_AUTH", ClientId: COGNITO_CLIENT_ID,
      AuthParameters: {USERNAME: username.value, PASSWORD: password.value},
    }),
  });
  const data = await r.json();
  if (!r.ok || !data.AuthenticationResult) { alert(data.message || "sign-in failed"); return; }
  idToken = data.AuthenticationResult.IdToken;
  login.style.display = "none"; app.style.display = "block";
  load();
}
async function load() {
  const r = await fetch(`${API}/hls`, {headers: {Authorization: idToken}});
  const {url} = await r.json();
  if (Hls.isSupported()) { const h = new Hls(); h.loadSource(url); h.attachMedia(v); }
  else { v.src = url; }                       // Safari plays HLS natively
}
async function cmd(action) {
  await fetch(`${API}/cmd`, {method: "POST",
    headers: {Authorization: idToken, "Content-Type": "application/json"},
    body: JSON.stringify({action})});
  if (action === "start") setTimeout(load, 8000);   // give KVS a moment to receive fragments
}
</script>
```

(`$VMS_HOME/client/index.html` has the full version with status messages and error
handling — this is the trimmed version showing the load-bearing pieces.)

### 8.5 Hosting the client

**Don't host this on the Pi.** The whole thesis of this project (§14) is zero inbound
ports on the adapter — putting the viewer-facing page on the Pi itself would need exactly
the port-forwarding this architecture exists to avoid, just for a different purpose. The
natural fit is a static host that's *meant* to be public: S3 static website hosting.

```bash
BUCKET="vms-demo-client-${ACCOUNT_ID}"
aws s3 mb "s3://$BUCKET" --region eu-central-1
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"
aws s3api put-bucket-policy --bucket "$BUCKET" --policy '{
  "Version": "2012-10-17",
  "Statement": [{"Effect":"Allow","Principal":"*","Action":"s3:GetObject",
                 "Resource":"arn:aws:s3:::'"$BUCKET"'/*"}]
}'
aws s3 website "s3://$BUCKET" --index-document index.html
aws s3 cp "$VMS_HOME/client/index.html" "s3://$BUCKET/index.html" --content-type text/html
```

Note the endpoint is **plain HTTP**, not HTTPS (S3 website hosting doesn't offer TLS on
its own — CloudFront in front of it would be the fix, out of scope for the MVP). This
isn't a mixed-content problem: the page's own API/Cognito calls are HTTPS regardless of
what scheme served the page, so the browser doesn't block them.

**Checkpoint 7 — verified two ways.** Backend, independent of any browser
(`curl` through Cognito auth → API Gateway → Lambda → `ffprobe` the returned URL, same
method as Checkpoints 4/5); and the actual browser client, opened on a real device —
confirmed working (2026-08-20), including a `bufferStalledError` from `hls.js` on first
load that self-recovered, a normal live-HLS startup hiccup, not a pipeline fault. Show it
on a phone with Wi-Fi off — that's the whole point of §14's thesis, made visible in one
screenshot.

**§8.6 exists because that curl-based verification, while necessary, isn't sufficient** —
it proves the backend, not the client. Every bug below only surfaced once a real person
used the real page in a real browser, including one that made the player spin forever
with no way out.

### 8.6 Client-side bugs found through real use

Four bugs, found in this order across actual browser sessions (2026-08-20), not caught by
any backend `curl` test because all four are specific to what a browser does with the
responses, not whether the responses were correct.

**1. A Lambda exception produces a browser-side `NetworkError`, not a readable error.**

*Symptom:* pressing Start/reloading sometimes showed `NetworkError when attempting to
fetch resource` in the client — a generic message with no indication of what actually
went wrong.

*Root cause:* `get_hls_url.py`'s CORS header
(`"headers": {"Access-Control-Allow-Origin": "*"}`) only gets attached on the function's
own `return` statement. When `kvs-cam01.service` wasn't producing and
`get_hls_streaming_session_url` raised `ResourceNotFoundException`, the exception
propagated *past* that return — API Gateway caught it and generated its own generic 502,
which has **no CORS headers at all**. The browser can't read a cross-origin response
missing that header, so `fetch()` throws a raw network-level error instead of resolving
with a normal (if unsuccessful) response — the actual error message never reaches the
page's own error handling.

*Fix:* wrap the whole handler body in `try`/`except`, and make every exit path — success
and failure alike — return through the same code that attaches CORS headers:

```python
try:
    ...
    return {"statusCode": 200, "headers": CORS, "body": ...}
except kv.exceptions.ResourceNotFoundException:
    return {"statusCode": 503, "headers": CORS,
            "body": json.dumps({"error": "stream is not currently live -- press Start"})}
except Exception as e:
    return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": str(e)})}
```

Applied to both Lambdas. **This class of bug is easy to miss precisely because backend
testing with `curl` doesn't reproduce it** — `curl` reads whatever body comes back
regardless of CORS headers; only a browser's same-origin policy enforces that check, so
the failure is invisible until real browser traffic hits the unhappy path.

**2. `mediaSourceRequiresReset` on every "Reload player" click.**

*Root cause:* the client's `load()` created a fresh `new Hls()` on every call and attached
it to the same `<video>` element without releasing the previous instance's `MediaSource`
first. Two overlapping `MediaSource` objects on one element is exactly what that error
means — not a KVS/HLS problem, a client bookkeeping bug.

*Fix:* track the instance in a module-level variable and destroy it first:

```js
let hls = null;
// ...
if (hls) { hls.destroy(); hls = null; }
hls = new Hls();
```

**3. The player spun forever after pressing Stop — no error, no recovery, no exit.**

*Root cause:* `hls.js`'s own recommended fatal-error recovery (`hls.startLoad()` on
`NETWORK_ERROR`) is correct *for a stream that's still live* — most fatal errors on a live
HLS stream are transient network blips that clear up on retry. But after Stop,
`kvs-cam01.service` had genuinely stopped producing, permanently — retrying a playlist
load against a stream that will *never* produce new segments again doesn't fail
cleanly, it just retries forever. The recovery logic had no way to distinguish "temporary
network hiccup, keep trying" from "deliberately stopped, stop trying."

*Fix:* an explicit `userStopped` flag, set the moment Stop is pressed, checked before any
retry logic runs:

```js
let userStopped = true;
// in the fatal-error handler:
if (userStopped) return;   // this player was torn down on purpose; ignore its errors
```

Pressing Stop now also tears the player down immediately (`hls.destroy()`, clear the
video's `src`) instead of leaving the old instance to stall out and retry on its own —
the two problems (bug 3 and the UX gap in bug 4) share one root cause and one fix.

**4. No visual difference between "loading" and "stopped."**

Even once bug 3 stopped the infinite retry, the user-visible result after Stop was a
frozen last video frame with a semi-transparent spinner sitting on top indefinitely —
functionally correct (nothing was retrying anymore) but with no way for a viewer to tell
"stopped" apart from "still loading." Fixed with an explicit overlay element (solid black,
`position: absolute; inset: 0` over the video) driven by state, not by player events:
`"Stream stopped. Press Start to watch again."` on Stop, `"Starting stream…"` immediately
on Start, cleared automatically on `Hls.Events.FRAG_BUFFERED` (first real video data
arrived) rather than guessing a fixed delay. Also needed: the `<video>` element has no
intrinsic size before any source has ever loaded, so the wrapper needs an explicit
`aspect-ratio: 16 / 9` — without it, the overlay itself collapses to the browser's tiny
default video height on first page load, before Start has ever been pressed.

---

## 9. Phase 8 — Event clips to S3

The hot-tier/cold-tier split from the KVS discussion, implemented.

### 9.1 Bucket with lifecycle

```bash
aws s3 mb s3://vms-demo-evidence-<unique>
aws s3api put-bucket-lifecycle-configuration \
  --bucket vms-demo-evidence-<unique> \
  --lifecycle-configuration '{
    "Rules":[{
      "ID":"tier-down","Status":"Enabled","Filter":{"Prefix":"clips/"},
      "Transitions":[
        {"Days":30,"StorageClass":"STANDARD_IA"},
        {"Days":90,"StorageClass":"DEEP_ARCHIVE"}]
    }]}'
```

### 9.2 Lambda `clip-to-s3`

Triggered by an IoT Rule on `adapter/+/event`:

```python
import boto3, os, json
from datetime import datetime, timedelta, timezone

S3B = os.environ["BUCKET"]

def lambda_handler(event, context):
    ts = datetime.fromisoformat(event["timestamp"])
    start, end = ts - timedelta(seconds=12), ts + timedelta(seconds=33)

    kv = boto3.client("kinesisvideo")
    ep = kv.get_data_endpoint(StreamName=event["stream"], APIName="GET_CLIP")["DataEndpoint"]
    kvam = boto3.client("kinesis-video-archived-media", endpoint_url=ep)

    clip = kvam.get_clip(
        StreamName=event["stream"],
        ClipFragmentSelector={
            "FragmentSelectorType": "PRODUCER_TIMESTAMP",
            "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
        },
    )["Payload"].read()

    key = f"clips/{event['stream']}/{ts:%Y/%m/%d}/{ts:%H%M%S}.mp4"
    boto3.client("s3").put_object(Bucket=S3B, Key=key, Body=clip,
                                  ContentType="video/mp4")

    boto3.resource("dynamodb").Table("clips").put_item(Item={
        "cameraId": event["stream"], "startTs": start.isoformat(),
        "s3Key": key, "labels": event.get("labels", []),
    })
    return {"key": key}
```

Use `PRODUCER_TIMESTAMP`, not server timestamp — after a WAN outage and backfill they
differ, and the operator means producer time.

**Checkpoint 8:** publishing an event to `adapter/adapter-01/event` produces a playable
MP4 in S3 and a row in DynamoDB.

---

## 10. Phase 9 — Measurements (what makes this a portfolio piece)

Anyone can wire services together. Numbers are what distinguish your work.

### 10.1 True glass-to-glass latency

With a real camera you get the *honest* measurement — sensor exposure, MJPG transfer,
decode, encode, and network are all included, which the synthetic loop could never show.

**Method.** Display a millisecond stopwatch on your phone or second monitor. Point the
PW310 at it. Arrange the phone and the browser window so a single photograph captures
both. The difference between the two displayed times is the end-to-end latency.

```text
   ┌─────────────┐         ┌──────────────────┐
   │  stopwatch  │◄─ cam   │  browser (HLS)   │
   │  14:32:07.4 │         │  14:32:01.9      │
   └─────────────┘         └──────────────────┘
              one photo → Δ = 5.5 s
```

Take n = 20 photographs and report median and interquartile range, not a single figure.

**Decompose it.** Measure at three points, and the contribution of each stage falls out
by subtraction:

| Measured at | Command | Isolates |
|---|---|---|
| Local RTSP | `ffplay -fflags nobuffer -rtsp_transport tcp rtsp://127.0.0.1:8554/cam01` | sensor + MJPG + decode + encode |
| KVS console playback | console **Media playback** tab | + upload + fragmentation |
| Browser HLS client | your §8 client | + packaging + player buffer |

Expect roughly 150–400 ms at the RTSP stage and 4–10 s at the browser. That gap is the
argument for KVS WebRTC, and being able to quantify it is the point.

**Then sweep one variable at a time:**

| Variable | Values to test | Expected effect |
|---|---|---|
| `h264_i_frame_period` | 15 / 30 / 75 frames (1 / 2 / 5 s at 15 fps) | fragment duration → dominant latency term |
| Frame rate | 30 / 15 / 10 fps (IDR period rescaled) | bitrate, encoder load, channel count (§2.7) |
| Encoder | `v4l2h264enc` vs `x264enc` | small latency delta, large CPU delta |
| `storage-size` | 32 / 128 / 512 MB | buffering headroom, not steady-state latency |
| `rtspsrc latency` | 0 / 200 / 500 ms | local jitter absorption only |

The IDR-period row is the one that gives you a real graph.

### 10.2 Reconnect behaviour

Simulate the WAN failure of §14 without unplugging anything:

```bash
# block outbound to AWS, keep SSH alive
sudo iptables -A OUTPUT -p tcp --dport 443  -j DROP
sudo iptables -A OUTPUT -p tcp --dport 8883 -j DROP
sleep 120
sudo iptables -D OUTPUT -p tcp --dport 443  -j DROP
sudo iptables -D OUTPUT -p tcp --dport 8883 -j DROP
```

Record: time to detect, retry intervals from the logs (you should see the backoff
doubling), time to first fragment after recovery, and whether the timeline in the console
shows a gap or a backfill.

```bash
journalctl -u kvs-cam01 -f | grep -Ei "retry|reconnect|error"
```

### 10.3 Resource cost on the adapter

The measurement that most directly showcases embedded judgement. Run the capture pipeline
twice — once with `v4l2h264enc`, once with `x264enc` — and compare:

```bash
pidstat -p $(pgrep -f publish-cam01) 1 60      # capture + encode
pidstat -p $(pgrep -f 'gst-launch.*kvssink') 1 60   # KVS producer
vcgencmd measure_temp && vcgencmd get_throttled
```

Report CPU %, RSS, and SoC temperature for each. Hardware encode should sit well under
20 % of one core; software encode at 720p30 typically lands at 50–70 %. Check
`get_throttled` — if the Pi thermally throttles during the software run, that is itself a
finding worth writing down.

Report CPU % and RSS per stream, then extrapolate: how many cameras fit on one Pi 4B
before the uplink or the CPU runs out? For a 10 Mbps domestic uplink at 1 Mbps per
stream, the answer is bandwidth-bound long before it is CPU-bound — and that is the
argument for edge event detection.

### 10.4 Fragment continuity

```bash
aws kinesis-video-archived-media list-fragments \
  --endpoint-url "$EP" --stream-name cam-01 \
  --fragment-selector '{"FragmentSelectorType":"PRODUCER_TIMESTAMP",
      "TimestampRange":{"StartTimestamp":"...","EndTimestamp":"..."}}'
```

Sum the durations against wall-clock elapsed and report the loss percentage across the
outage window.

---

## 11. Phase 10 — Teardown

`teardown.sh`:

```bash
#!/usr/bin/env bash
set -e
sudo systemctl stop kvs-cam01.service
pkill -f mediamtx || true
aws kinesisvideo delete-stream \
  --stream-arn "$(aws kinesisvideo describe-stream --stream-name cam-01 \
     --query StreamInfo.StreamARN --output text)"
echo "Stopped. S3/DynamoDB left in place — delete manually if finished."
```

Run it every evening. Recreating the stream in §3 takes ten seconds.

---

## 12. Suggested repository layout

```text
VMS/                            # $VMS_HOME — everything lives here, not scattered in $HOME
├── README.md                  # architecture diagram + the numbers from §10
├── .gitignore                 # excludes certs/, venv-adapter/, built binaries
├── adapter/
│   ├── bin/
│   │   ├── publish-cam01.sh    # PW310 → v4l2h264enc → MediaMTX (§2.5)
│   │   ├── camera-init.sh      # v4l2-ctl exposure/WB/focus lock (§2.3)
│   │   └── stream-cam01.sh     # MediaMTX → kvssink (§6.5)
│   ├── agent.py
│   ├── kvs-cam01.service
│   └── requirements.txt
├── mediamtx/                  # downloaded binary + mediamtx.yml, run from here
├── vendor/
│   └── amazon-kinesis-video-streams-producer-sdk-cpp/   # §4 build, gitignored
├── certs/                     # adapter cert/key material — gitignored, never committed
├── venv-adapter/               # python venv for agent.py — gitignored
├── cloud/
│   ├── iam/                   # role trust + producer policy JSON
│   ├── iot/                   # thing policy, role alias
│   ├── lambda/
│   │   ├── get_hls_url.py
│   │   ├── publish_cmd.py
│   │   └── clip_to_s3.py
│   └── template.yaml          # SAM — makes it reproducible, and reviewers notice
├── client/
│   └── index.html
├── measurements/
│   ├── latency_vs_gop.csv
│   ├── reconnect_timeline.md
│   └── plots.py
└── teardown.sh
```

Writing the cloud side as a SAM or CDK template rather than console clicks is worth the
extra hour: it demonstrates you think about reproducibility, and it makes teardown exact.

---

## 13. Build order and realistic time budget

| Phase | Content | Time |
|---|---|---|
| 1 | Prerequisites, budget alarm | 20 min |
| 2 | PW310 bring-up, exposure lock, encode, MediaMTX | 1.5–2 h |
| 3 | KVS stream | 10 min |
| 4 | **Producer SDK build on Pi** | 1–1.5 h (mostly waiting) |
| 5 | First light, static keys | 30 min |
| 6 | X.509 + role alias | 1.5 h (the fiddly one) |
| 7 | MQTT control agent + systemd | 1.5 h |
| 8 | Lambda + Cognito + HLS client | 2–3 h |
| 9 | Clips to S3 + DynamoDB | 1.5 h |
| 10 | Measurements and write-up | 3–4 h |

Roughly two focused weekends. **Do not skip Phase 10** — it is the part that is actually
yours, and the part nobody else's tutorial-follower will have.

---

## 14. Talking points this demo earns you

- *Why outbound?* Because NAT state (§15) makes return traffic free, and CGNAT on
  cellular sites makes inbound impossible. You have the iptables experiment to prove the
  reconnect path works.
- *Why not MQTT for video?* 128 KB payload cap and per-message pricing. You know the
  bitrate arithmetic.
- *Why KVS and not S3 directly?* Time-indexed random access and server-side HLS
  packaging for the hot window; S3 for the cold tier because KVS has no archival class.
  You built both halves.
- *Why certificate-based credentials?* One identity for control and media planes, no
  long-lived secrets on a device sitting in a customer's building, and per-thing policy
  scoping that survives to 1000 units.
- *Why MQTT on 443?* Because 8883 is blocked on real customer sites and an installer
  can't promise it's open. ALPN makes the control plane look like HTTPS, which matches
  the Cloud Adapter Mini's protocol profile — while keeping Shadow, Jobs and LWT that a
  single-channel design would force you to hand-build. You can prove it: 8883 is
  firewalled off on the demo box.
- *Why two planes at all, when Videoloft uses one?* Because a prototype benefits from a
  managed control plane, and because separating them makes the concept legible. At their
  scale the single-channel choice is better — their control path carries talkdown audio
  anyway, and one socket removes the split-brain state where control reports online while
  media is dead. See §16.
- *Why hardware encode?* The PW310 delivers MJPG; KVS needs H.264. Offloading the
  transcode to the Pi 4B's V4L2 M2M encoder frees ~50 % of a core for the producer SDK,
  and you have the `pidstat` numbers for both paths. This is exactly the SoC-selection
  argument — the encoder block is why a 4B works and a bare Cortex-A53 board wouldn't.
- *Why 15 fps?* Because frame rate is the dominant cost lever, and I can show the curve:
  bitrate falls only 30–40 % when you halve the rate, because P-frames get more expensive
  and I-frames become a larger share. The product ships 10 fps for fleet economics; 15
  keeps the demo legible while making the same point.
- *What did you measure?* True glass-to-glass latency decomposed by stage, latency as a
  function of IDR period, reconnect timing under induced WAN failure, per-stream CPU and
  bandwidth, fragment loss across an outage.

## 15. Known traps

| Symptom | Cause |
|---|---|
| Camera works, then `/dev/video0` is the codec | node numbering shifted — use `/dev/v4l/by-id/` |
| fps silently drops to 15 in the evening | UVC auto-exposure; lock it (§2.3) |
| `v4l2src` fails with "Device or resource busy" | MediaMTX/ffplay/guvcview still holding the node |
| USB errors, torn frames, `-71` in dmesg | USB 2.0 bandwidth — use MJPG not YUYV; try a powered hub |
| `v4l2h264enc` fails to link | missing the `video/x-h264,level=(string)4` caps filter |
| Green or shifted colours after `jpegdec` | missing `videoconvert` / wrong `format=I420` |
| Latency doubled after an fps change | `h264_i_frame_period` is in frames — rescale it (§2.7) |
| `videorate` duplicating frames | missing `drop-only=true` |
| `No such element "kvssink"` | `GST_PLUGIN_PATH` not pointing at the SDK `build/` dir |
| Build dies around OpenSSL/curl | `make -j4` on 4 GB — OOM killer; use `-j2` |
| Fragments rejected, console empty | missing `h264parse config-interval=-1` |
| TLS error on credentials endpoint | wrong root CA — needs SFSRootCAG2, not AmazonRootCA1 |
| MQTT connects, publish silently fails | policy resource missing the topic wildcard |
| Playback stalls after ~5 min | HLS session URL expired; client must re-fetch |
| High CPU on the Pi | re-encoding instead of `-c copy`, or main stream not sub stream |
| Costs climbing | pipeline left running; retention not set to 24 h |

---

## 16. Extending toward Cloud Adapter Mini parity

The MVP above proves the *concepts*. This section is the honest gap analysis against the
commercial product, ordered by ratio of credibility gained to effort spent. Treat it as a
roadmap in the README — knowing precisely what you have not built is worth nearly as much
as building it.

### 16.1 The hardware is already the same

The Cloud Adapter Mini's published specification is a Cortex-A72 quad at 1.5 GHz,
VideoCore VI, 4 GB LPDDR4-3200, micro-SD boot, micro-HDMI, USB-C at 5.1 V/3 A. That is a
Raspberry Pi 4B, item for item. Nothing below is blocked by hardware; the gap is entirely
software.

### 16.2 Gap analysis

| Capability | Cloud Adapter Mini | This prototype | Effort to close |
|---|---|---|---|
| Channels | 8 or 16 | 1 | **S** — parameterise the pipeline |
| Video handling | pass-through H.264/H.265 | transcode MJPG → H.264 | **S** — use an RTSP camera |
| Frame rate to cloud | capped at 10 fps | 15 fps, configurable (§2.7) | **done** |
| Recording policy | motion-triggered by default | continuous | **M** — motion detect + event upload |
| Outage buffering | 32 GB USB, auto-backfill | RAM only (`storage-size`) | **M** — disk-backed queue |
| Fleet updates | weekly remote push | none | **M** — IoT Jobs + A/B partitions |
| Camera discovery | hundreds of brands | hardcoded URL | **M** — ONVIF WS-Discovery |
| Local HDMI display | up to 4K live wall | none | **S** — a second GStreamer sink |
| PTZ / talkdown | yes | command topic only | **M** — ONVIF PTZ, reverse audio |
| Health monitoring | per-camera status | none | **S** — shadow reporting |
| Network isolation | dual NIC (Enterprise) | single LAN | **S** — second interface + routes |
| Retention tiers | 2 days to 10 years | 24 h | already covered by §9 S3 path |
| AI analytics | Smart Motion, ALPR, people counting, PPE | none | **L** — out of MVP scope |

### 16.3 The four worth actually doing

**(a) Multi-channel.** Move from one hardcoded pipeline to a JSON camera list and one
`gst` process per channel, each with its own KVS stream and systemd unit template
(`kvs-cam@.service`). Full methodology in **§16.6**. Then measure where a Pi 4B saturates — and note that it saturates
much sooner than the product because you transcode and they pass through. Publishing that
number, with the reason, is a stronger result than quietly matching their channel count.

**(b) Pass-through instead of transcode.** Borrow or buy any cheap ONVIF/RTSP camera and
run a second profile with no `jpegdec`/`v4l2h264enc` in the chain. CPU per channel drops
by an order of magnitude. Having both paths measured side by side is the clearest
demonstration in the whole project that you understand where the cost sits.

**(c) Durable outage buffering.** This is the feature the product markets hardest and the
one your prototype most conspicuously lacks. Minimum viable version: `splitmuxsink`
writing 10-second MP4 segments to a USB stick continuously, a small uploader that walks
the directory and pushes segments to S3 when connectivity returns, and a retention sweep
that deletes on success. Then re-run the §10.2 outage test and report the gap-fill
percentage before and after. That is a real engineering result.

```bash
# rough shape of the local recorder leg
... ! h264parse ! tee name=t \
  t. ! queue ! kvssink ... \
  t. ! queue ! splitmuxsink location=/mnt/usb/cam01_%05d.mp4 \
                max-size-time=10000000000
```

**(d) Fleet OTA via IoT Jobs.** Weekly remote updates across devices in customers'
buildings is the hardest unglamorous problem in this product category. A credible
demonstration is small: an IoT Job document carrying a version and an S3 URL, an agent
handler that downloads, verifies a signature, writes to the inactive slot and reboots,
plus a watchdog that reverts if the agent doesn't check in within N minutes. Rollback is
the part that matters — anyone can implement download-and-install.

### 16.4 Deliberate non-goals

State these explicitly in the README; scoping discipline reads as maturity.

- **AI analytics.** Rekognition or a Greengrass inference component would demonstrate
  integration, not engineering. Skip unless the target role is ML-adjacent.
- **Talkdown.** Needs a reverse audio path and the ONVIF backchannel; interesting, but it
  argues for a WebRTC redesign rather than an addition.
- **Their single-channel control design.** Worth having the opinion (§14), not worth
  rebuilding. If you ever did, the AWS-native form is an API Gateway WebSocket API with
  `connectionId` in DynamoDB as the device registry, and `PostToConnection` for downlink.
- **NDAA compliance, enclosure, certification.** Product concerns, not architecture.

### 16.5 A one-week version, if time is short

If you only have a few evenings beyond the MVP, do these three and stop:

1. Switch to motion-triggered recording — cheapest possible change, and with the frame
   rate policy already in place (§2.7) it completes the bandwidth-economics story that
   drives the product's defaults.
2. Add `splitmuxsink` local recording with an S3 backfill uploader (16.3c).
3. Parameterise to four channels and publish the saturation measurement (§16.6).

That covers the three things a reviewer from this industry will actually ask about:
bandwidth policy, outage resilience, and scale limits.

### 16.6 Multi-channel scaling: methodology

The single most publishable experiment available in this project. Do it properly and it
produces a result, not a demo.

#### Channel configuration

```json
{
  "channels": [
    {"id":"cam-01","url":"rtsp://192.168.178.90:554/Streaming/Channels/102","mode":"passthrough"},
    {"id":"cam-02","url":"rtsp://127.0.0.1:8554/loop02","mode":"passthrough"},
    {"id":"cam-03","url":"v4l2:///dev/video20","mode":"transcode"}
  ]
}
```

#### Templated unit — `kvs-cam@.service`

```ini
[Unit]
Description=KVS producer for %i
After=network-online.target

[Service]
EnvironmentFile=/etc/adapter/channels/%i.env
ExecStart=/home/vladimir/MyProjects/VMS/adapter/bin/stream-channel.sh
Restart=on-failure
RestartSec=5
CPUAccounting=true
MemoryAccounting=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now kvs-cam@cam-01 kvs-cam@cam-02 kvs-cam@cam-03
systemd-cgtop -1        # per-channel CPU and memory, via cgroups, for free
```

**One process per channel, not one process with N pipelines.** The cost is ~30–50 MB RSS
each. The return: a hung RTSP source or wedged encoder on one camera cannot take down the
other fifteen, `Restart=on-failure` gives per-channel supervision you didn't write, and
`CPUAccounting` yields clean per-channel measurements without chasing PIDs. Any commercial
adapter is built this way for the same reason.

The agent then drives `systemctl start kvs-cam@<id>` per channel and reports a per-channel
array in its shadow — the same shape as the health frame a single-channel design would
carry in its heartbeat (§16.7).

#### Synthesising N sources from one webcam

You have one PW310, so the load must be synthetic — and must isolate the variable under
test.

**Pass-through arm** — pre-encoded H.264, published N times, stream-copied:

```bash
for i in $(seq 1 12); do
  ffmpeg -re -stream_loop -1 -i sample720.mp4 -c copy \
    -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/loop$i &
done
```

**Transcode arm** — N MJPG sources via `v4l2loopback`, so the pipeline is byte-for-byte
the one the real camera drives:

```bash
sudo apt install -y v4l2loopback-dkms
sudo modprobe v4l2loopback devices=4 video_nr=20,21,22,23 exclusive_caps=1

for n in 20 21 22 23; do
  ffmpeg -re -stream_loop -1 -i sample720.mp4 -c:v mjpeg -q:v 5 \
    -f v4l2 /dev/video$n &
done
```

Loopback devices rather than four real webcams removes USB bus contention as a confound.
Real multi-camera USB on a Pi 4B has its own ceiling — worth a separate note, not a
confounded measurement.

#### The shared-encoder trap

The VideoCore VI has **one** H.264 encoder block. Four `v4l2h264enc` instances do not get
four encoders; they timeshare one V4L2 M2M device with finite total throughput. Expect the
transcode arm to hit a wall unrelated to CPU percentage, with confusing symptoms — frames
dropping while `top` shows idle cores.

```bash
watch -n1 'vcgencmd measure_clock v3d; vcgencmd measure_temp; vcgencmd get_throttled'
```

This is the most interesting finding the experiment can produce, and it is exactly the
SoC-selection argument: a shared fixed-function block is a hard architectural limit that
CPU headroom cannot fix.

#### Define saturation before measuring

Do not report "it managed six cameras". Fix the failure criterion in advance and report
the channel count at which it is breached:

- **Fragment continuity < 99.5 %** over a 10-minute window — sum `list-fragments`
  durations against wall clock. Primary criterion: it is what a customer experiences as
  missing footage.
- **Latency growth** — median glass-to-glass rising monotonically across the window means
  a buffer is filling and you are past the knee.
- **Producer backpressure** — the SDK logs frames dropped at GOP boundaries when upload
  cannot keep up. Grep for it.

One table per arm:

| Channels | CPU % total | v3d clock | Uplink Mbps | Fragment continuity | Median latency |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 4 | | | | | |
| 8 | | | | | |

#### The expected punchline

Three distinct ceilings, hit in this order:

1. **Uplink bandwidth.** German VDSL is commonly 40/10 or 100/40. At 1.0 Mbps per channel
   (§2.7), a 10 Mbps uplink saturates near nine channels — before the Pi is troubled.
2. **Shared encoder**, transcode arm only — likely 6–10 channels at 720p15.
3. **CPU**, mostly `jpegdec`, close behind.

The pass-through arm should pass all three on CPU and stop only at the uplink.

That is the result worth publishing: **the adapter is not the bottleneck, the uplink is**
— which is precisely why the commercial product caps frame rate and defaults to
motion-triggered recording. Deriving the product's design decisions from your own
measurements is a substantially stronger claim than matching its channel count.

### 16.7 What "multiplexed control" would mean, if you built it their way

The Cloud Adapter Mini lists HTTP, HTTPS, RTSP, RTSPS and NTP — and no MQTT port. Control
therefore rides the same HTTPS channel as media. No protocol documentation is published,
so what follows is the design space, inferred from the specification and feature list, not
their implementation.

**Plausible transports:** a single WebSocket over 443 (simplest, most likely); HTTP/2 with
control on one stream and each channel's upload on another; or a long-lived chunked
response for downlink paired with discrete POSTs for uplink (ugliest, most proxy-tolerant).

**A framed envelope** over any of them:

```json
{"t":"cmd","id":"7f3a","ch":3,"op":"ptz","args":{"pan":-0.4,"tilt":0.1}}
{"t":"ack","id":"7f3a","ok":true}
{"t":"hb","up":86400,"ch":[{"id":1,"fps":10,"st":"ok"},{"id":3,"st":"rtsp_timeout"}]}
{"t":"job","id":"9c21","op":"update","ver":"2026.31.2","url":"https://…","sig":"…"}
```

**Four problems the design creates:**

- **Head-of-line blocking.** A PTZ command queued behind a 2 MB media segment waits for
  its bytes. HTTP/2 multiplexes at the application layer but not at the transport layer —
  TCP still delivers in order. Talkdown makes this acute: downstream audio is
  latency-sensitive and would sit behind upstream video. The likely mitigation is separate
  TCP connections for control and bulk media within one logical session — "single channel"
  can mean one session identity, not literally one socket.
- **Proxy and NAT idle timeouts.** Transparent proxies close idle connections at
  60–300 s; NAT entries expire sooner. Heartbeat period becomes a deployment parameter.
  Some proxies buffer chunked responses, which breaks a COMET-style downlink silently —
  the socket stays open and commands simply never arrive. That bug appears at customer
  site #400, not in the lab.
- **Presence is harder than LWT.** A half-open TCP connection — device powered off, no FIN
  — is indistinguishable from an idle healthy one until a write fails. You need
  application heartbeats with a miss threshold, and a definition of "offline" for the
  dashboard.
- **Connection affinity.** Device 4,712's socket terminates on exactly one connection
  server. A viewer's request lands elsewhere behind the load balancer and must be routed
  to it. That means a connection registry plus an internal bus, surviving connection-server
  replacement during deploys. IoT Core does this invisibly; API Gateway WebSocket exposes
  it as `connectionId` + `PostToConnection`; building it yourself is a service with an
  on-call rota.

**What they build that IoT Core gives away:** device registry, per-device credentials,
presence semantics, desired/reported state reconciliation, job distribution with rollout
control, affinity routing, at-least-once delivery with dedup. Each is tractable; together
they are a platform team.

**What they get in return:** a media-capable control path that talkdown needs anyway, one
unambiguous connection state instead of a split-brain where control says online and media
is dead, no broker cost, and no per-message pricing at fleet scale.

**The conclusion to state, and not hedge:** their choice is correct at their scale; the
two-plane split is correct at prototype scale. The symmetry is the answer — not a
preference for either.

## 17. Migration path: moving the archive from KVS to S3

Cost model and justification in **`COSTS.md`**. Summary: KVS charges $0.0085 per GB
ingested and S3 charges nothing for ingress, which at 24/7 recording is $4.13 versus
$0.00 per camera per month. This section is the step-by-step migration.

**The migration is additive, not a replacement.** At every step both paths run
simultaneously from one capture, so nothing is ever broken and the two can be compared
directly. The end state is a hybrid, not a cutover:

```text
                    ┌─ tee ─┬─ kvssink ────────► KVS  (live only, on demand, retention 0)
   camera ─► RTSP ──┤       │
                    │       └─ splitmuxsink ──► /var/spool ─► uploader ─► S3 (archive, 24/7)
                    └─ (motion detect, later)
```

Live view keeps the managed packager because latency matters and viewing hours are few.
Continuous archive moves to S3 because it runs 720 hours a month and ingest is free.

> **Correction from observation.** This section originally assumed the live leg had to be
> KVS or WebRTC. The reference product uses neither: it runs **Low-Latency HLS with ~1 s
> partial segments** (Appendix B §19.9), reaching 2–4 s latency with no signalling
> infrastructure, no TURN, and no per-GB ingest charge. If you implement only one live
> path, LL-HLS is now the better default; keep KVS for the on-demand case only if you
> want the managed packager for comparison purposes.

---

### M0 — Decide the split before writing code

| Concern | Path | Why |
|---|---|---|
| Live view, scrub-back last few minutes | KVS, `retention=0` | sub-10 s latency, no storage charge |
| *(observed alternative — see Appendix B §19.9)* | **LL-HLS, ~1 s parts** | 2–4 s latency, no signalling, no KVS ingest at all |
| Continuous 24/7 archive | S3 | free ingress dominates everything |
| Event clips, evidence export | S3 (`evidence/` prefix) | already built in §9 |
| Timeline playback of archive | S3 + generated HLS playlist | the work of M4 |

Setting **`--data-retention-in-hours 0`** on the KVS stream turns it into a pure
pass-through: live playback still works, storage charges disappear. Do that at M8, not
before.

---

### M1 — Add the archive leg alongside KVS

Non-destructive: `tee` splits the parsed H.264 to both sinks. Nothing existing changes.

```bash
sudo mkdir -p /var/spool/vms/cam-01 && sudo chown vladimir:vladimir /var/spool/vms/cam-01

gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 ! \
  rtph264depay ! h264parse config-interval=-1 ! tee name=t \
  t. ! queue ! kvssink stream-name="cam-01" aws-region="eu-central-1" \
                 iot-certificate="..." \
  t. ! queue ! splitmuxsink muxer-factory=mpegtsmux \
                 location=/var/spool/vms/cam-01/seg_%08d.ts \
                 max-size-time=60000000000 \
                 send-keyframe-requests=true async-finalize=true
```

`max-size-time` is nanoseconds — 60 s. `splitmuxsink` cuts only on keyframes, so with the
2 s IDR period from §2.5 segments land within 2 s of target. `async-finalize=true` stops
the pipeline stalling while a segment is closed.

**Why 60 s and not 6 s:** at $0.005 per 1,000 PUTs, 6-second segments cost $2.16/month in
requests against $0.75 of storage — requests exceed storage 3×. See `COSTS.md` §6.2. This
is the single most important parameter in the whole migration.

**Checkpoint M1:** `ls -l /var/spool/vms/cam-01/` shows a new ~60 s `.ts` file each
minute, and KVS console playback still works.

---

### M2 — Bucket, key layout and scoped credentials

The key layout **is** the index. Fixed-width epoch milliseconds sort lexicographically,
so a prefix listing returns an ordered time range with no database involved.

```text
s3://vms-archive/<tenant>/<camera>/<YYYY>/<MM>/<DD>/<HH>/<epochms>_<durms>.ts

vms-archive/demo/cam-01/2026/08/20/14/1755700320000_60021.ts
```

Because start time and duration live in the key, the index is **reconstructible by
listing the bucket**. Any database on top is a cache, never the source of truth.

```bash
aws s3 mb s3://vms-archive-<unique>
aws s3api put-public-access-block --bucket vms-archive-<unique> \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

Extend the existing `KVSAdapterRole` (§6.1) rather than creating new credentials — the
adapter's X.509 certificate then authorises both planes and both storage paths:

```json
{
  "Effect": "Allow",
  "Action": "s3:PutObject",
  "Resource": "arn:aws:s3:::vms-archive-<unique>/demo/${iot:Connection.Thing.ThingName}/*"
}
```

Do **not** use presigned URLs per segment — it doubles request count and makes every
60 seconds of recording depend on the control plane.

**Checkpoint M2:** `aws s3 cp` of a test segment succeeds using only certificate-derived
credentials, and fails for any other camera's prefix.

---

### M3 — Uploader with delete-on-success

One rule carries the entire outage-resilience story: **delete only after a confirmed
200.** If the WAN is down the upload raises, the file stays in the spool, and the next
pass retries it.

`$VMS_HOME/adapter/bin/uploader.py`:

```python
import boto3, os, time, hashlib
from pathlib import Path
from datetime import datetime, timezone

SPOOL   = Path("/var/spool/vms/cam-01")
BUCKET  = os.environ["ARCHIVE_BUCKET"]
TENANT, CAM = "demo", "cam-01"
CAP_BYTES   = 24 * 1024**3          # spool ceiling

s3 = boto3.client("s3")

def key_for(start_ms, dur_ms):
    t = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    return (f"{TENANT}/{CAM}/{t:%Y/%m/%d/%H}/{start_ms}_{dur_ms}.ts")

def sweep_cap():
    files = sorted(SPOOL.glob("*.ts"), key=lambda p: p.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while total > CAP_BYTES and files:                 # oldest-first eviction
        victim = files.pop(0); total -= victim.stat().st_size; victim.unlink()

while True:
    for path in sorted(SPOOL.glob("*.ts")):
        if time.time() - path.stat().st_mtime < 5:     # still being written
            continue
        meta = probe(path)                             # start_ms, dur_ms via ffprobe
        try:
            s3.upload_file(str(path), BUCKET, key_for(**meta), ExtraArgs={
                "ContentType": "video/mp2t",
                "Metadata": {"start": str(meta["start_ms"]),
                             "dur": str(meta["dur_ms"]),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}})
            path.unlink()                              # only on success
        except Exception as e:
            print(f"upload failed, will retry: {e}")
            break                                      # preserve ordering
    sweep_cap()
    time.sleep(5)
```

The spool cap is what makes this safe on a 32 GB card: at 1 Mbps, 24 GB holds roughly two
days of outage. The commercial product ships a 32 GB USB stick for exactly this.

Run it under systemd as `vms-uploader.service` with `Restart=always`.

**Checkpoint M3:** segments appear in S3 within ~70 s of capture; `iptables`-blocking 443
causes the spool to grow and then drain completely on restore, with no gaps.

---

### M4 — Playlist Lambda: rebuilding what KVS packaged

This replaces `GetHLSStreamingSessionURL`. A Lambda lists the prefix range and emits an
HLS playlist.

```python
import boto3, os
from math import ceil
from datetime import datetime, timezone

S3, BUCKET = boto3.client("s3"), os.environ["ARCHIVE_BUCKET"]

def segments(tenant, cam, start_ms, end_ms):
    out, pag = [], S3.get_paginator("list_objects_v2")
    for hour in hours_between(start_ms, end_ms):                # 'YYYY/MM/DD/HH'
        for page in pag.paginate(Bucket=BUCKET, Prefix=f"{tenant}/{cam}/{hour}/"):
            for o in page.get("Contents", []):
                name = o["Key"].rsplit("/", 1)[-1].removesuffix(".ts")
                st, dur = (int(x) for x in name.split("_"))
                if st + dur >= start_ms and st <= end_ms:
                    out.append({"key": o["Key"], "start": st, "dur": dur})
    return sorted(out, key=lambda s: s["start"])

def playlist(segs, base):
    m = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
         f"#EXT-X-TARGETDURATION:{ceil(max(s['dur'] for s in segs)/1000)}",
         "#EXT-X-MEDIA-SEQUENCE:0"]
    prev_end = None
    for s in segs:
        if prev_end and s["start"] - prev_end > 500:            # WAN outage gap
            m.append("#EXT-X-DISCONTINUITY")
        iso = datetime.fromtimestamp(s["start"]/1000, timezone.utc).isoformat()
        m += [f"#EXT-X-PROGRAM-DATE-TIME:{iso}",
              f"#EXTINF:{s['dur']/1000:.3f},",
              base + s["key"]]
        prev_end = s["start"] + s["dur"]
    m.append("#EXT-X-ENDLIST")
    return "\n".join(m)
```

Three tags carry the whole design:

- **`EXT-X-PROGRAM-DATE-TIME`** maps media position to wall-clock time. This is the time
  index made visible to the player, and it is what lets an operator seek to 14:32 rather
  than to "12 minutes into the file". Non-negotiable for a VMS.
- **`EXT-X-DISCONTINUITY`** marks outage gaps. Omit it and players stall or mis-seek
  across the join — the most common bug in hand-rolled HLS.
- **`EXT-X-TARGETDURATION`** must be the ceiling of the longest segment, or players
  misjudge their buffer.

Serve segments through **CloudFront with signed cookies**, not presigned S3 URLs: one
cookie authorises the whole `/<tenant>/<camera>/*` path, so a two-hour playback needs one
auth artifact instead of 120. Caching means a second operator watching the same footage
costs almost nothing.

**Checkpoint M4:** the browser client plays archive footage from S3, and seeking to a
wall-clock time lands within one segment.

---

### M5 — Client switch and A/B comparison

Add a source toggle to the §8 client — `?src=kvs` or `?src=s3` — pointing at the two
Lambdas. Same `hls.js`, same player.

This is the demo moment: identical footage, identical player, two architectures, and a
cost figure attached to each. Being able to switch between them live is more convincing
than any slide.

**Checkpoint M5:** both sources play; latency and seek behaviour measured for each.

---

### M6 — fMP4 upgrade and free clip export

MPEG-TS gets M1–M5 working with packages already on the Pi. fMP4/CMAF is what you would
ship, for one decisive reason: **fMP4 segments byte-concatenate into a valid MP4.**

```python
body = s3.get_object(Bucket=B, Key=init_key)["Body"].read()
for s in segments(tenant, cam, start_ms, end_ms):
    body += s3.get_object(Bucket=B, Key=s["key"])["Body"].read()
s3.put_object(Bucket=EVIDENCE, Key=clip_key, Body=body, ContentType="video/mp4")
```

That is `GetClip`, reimplemented in six lines with no transcode and no ffmpeg. Also ~10 %
less container overhead than TS, and one file set serves both HLS and DASH.

The cost: `isofmp4mux` lives in `gst-plugins-rs`, which must be built on the Pi. Budget an
evening. Emit `#EXT-X-MAP:URI="init.mp4"` in the playlist, re-upload the init segment
whenever encoder settings change, and insert a discontinuity at that point.

**Checkpoint M6:** a concatenated clip plays in VLC and QuickTime without remuxing.

---

### M7 — Lifecycle, retention and tiering

```json
{"Rules":[
  {"ID":"archive-2d","Status":"Enabled","Filter":{"Prefix":"demo/"},
   "Expiration":{"Days":2},
   "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}},
  {"ID":"evidence-tier","Status":"Enabled","Filter":{"Prefix":"evidence/"},
   "Transitions":[{"Days":30,"StorageClass":"STANDARD_IA"},
                  {"Days":90,"StorageClass":"DEEP_ARCHIVE"}]}
]}
```

Retention becomes a per-tenant lifecycle rule — which is why a commercial price ladder
from 2 days to 2 years is so flat.

Two traps: Standard-IA bills a **30-day minimum duration** and Glacier classes 90–180
days, so tiering footage that expires at 2 days *increases* cost. And each Glacier object
carries ~40 KB overhead — archive 60 s segments, never 6 s.

**Checkpoint M7:** objects older than the retention window disappear automatically, and
the DynamoDB TTL (if used) expires on the same schedule.

---

### M8 — Cut over the KVS role

Only now, with the archive proven:

```bash
# recreate the stream as a pure live pass-through
aws kinesisvideo update-data-retention --stream-name cam-01 \
  --current-version "$(aws kinesisvideo describe-stream --stream-name cam-01 \
      --query StreamInfo.Version --output text)" \
  --operation DECREASE_DATA_RETENTION --data-retention-in-hours 0
```

Then gate the `kvssink` leg on demand: the agent starts it when a viewer connects
(§7 command topic) and stops it after an idle timeout. Storage charges go to zero and
ingest is billed only for minutes actually watched.

**Checkpoint M8:** archive continues 24/7 to S3; KVS ingest appears in CloudWatch only
while someone is viewing.

---

### M9 — Publish the comparison

The point of the exercise. Measure, don't estimate:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/KinesisVideo \
  --metric-name PutMedia.IncomingBytes --dimensions Name=StreamName,Value=cam-01 \
  --start-time 2026-08-19T00:00:00Z --end-time 2026-08-20T00:00:00Z \
  --period 3600 --statistics Sum

aws ce get-cost-and-usage --time-period Start=2026-08-01,End=2026-08-31 \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE
```

Tag everything `project=vms-demo`. Put measured-versus-modelled in the README next to the
`COSTS.md` table.

| Effort | Phase | Time |
|---|---|---|
| M1 | tee + splitmuxsink | 1 h |
| M2 | bucket, keys, scoped IAM | 1 h |
| M3 | uploader + spool cap | 2–3 h |
| M4 | playlist Lambda + CloudFront | 3–4 h |
| M5 | client toggle | 1 h |
| M6 | fMP4 (optional) | 3–4 h |
| M7 | lifecycle | 30 min |
| M8 | KVS retention 0, on-demand live | 1 h |
| M9 | measure and write up | 2 h |

Roughly one further weekend for M1–M5 and M7 — the version that makes the argument. M6
and M8 are polish.

### M10 — Add an LL-HLS live path (replaces the KVS live leg)

The observed alternative from Appendix B §19.9, and on your hardware it is a config
block rather than a build. MediaMTX — already running as the RTSP server from §2.5 —
implements Low-Latency HLS natively.

`mediamtx.yml`:

```yml
hls: yes
hlsVariant: lowLatency
hlsSegmentDuration: 1s
hlsPartDuration: 200ms
hlsSegmentCount: 7
hlsAlwaysRemux: yes
hlsAllowOrigin: '*'
hlsAddress: :8888
```

```bash
# from the LAN
firefox http://raspi:8888/cam01
```

Same RTSP source that feeds `kvssink`, second consumer, no extra encode.

#### What the manifest contains

```
#EXT-X-PART-INF:PART-TARGET=0.2
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.6,CAN-SKIP-UNTIL=24.0
#EXT-X-MAP:URI="init.mp4"
#EXT-X-PART:DURATION=0.201,URI="seg38_part0.mp4",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.200,URI="seg38_part1.mp4"
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="seg38_part2.mp4"
```

Two mechanisms remove two round trips:

- **Blocking reload** — `GET stream.m3u8?_HLS_msn=38&_HLS_part=2` means "respond when
  that part exists". The server holds the request open. No polling interval to tune, and
  the client never arrives late.
- **Preload hint** — the client requests a part that does not exist yet; the server
  streams bytes with chunked transfer encoding as the encoder produces them.

#### Latency budget

```text
part duration            0.2 s
PART-HOLD-BACK        ≥  0.6 s     (spec minimum: 3 × PART-TARGET)
encode + upload       ~0.3–0.8 s
player decode buffer  ~0.2–0.5 s
                      ───────────
total                 ~1.3–2.1 s
```

`PART-HOLD-BACK` dominates and is specified, not chosen — it is always ≥ 3 × part
duration. Shorter parts buy lower latency at the price of request count.

#### Two hard requirements

- **fMP4/CMAF only.** MPEG-TS cannot be meaningfully subdivided mid-segment, so the
  archive path's container choice does not carry over.
- **HTTP/2 in production.** Many concurrent short requests over one connection; HTTP/1.1
  head-of-line blocking hurts badly. Fine over LAN for the demo, but note it.

#### Why this replaces the KVS live leg

| | LL-HLS | KVS HLS | WebRTC |
|---|---|---|---|
| Latency | 1.5–4 s | 3–10 s | 0.3–0.5 s |
| Infrastructure | an HTTP server | managed | signalling + TURN |
| Cost model | egress only | ingest + consume + egress | channels + TURN minutes |
| Requests/camera/hour | ~7,000 at 1 s parts | few | ~0 after setup |
| CDN-cacheable | yes — parts are immutable | no | no |
| Viewer fan-out | unlimited | unlimited | quota-limited |

No per-GB ingest, no signalling infrastructure, and the parts are immutable so a CDN
actually helps — the opposite of the archive range API. The hidden cost is request
volume: at 1-second parts, 23 cameras is ~160,000 requests/hour per viewer. Acceptable
against your own origin over HTTP/2; expensive through API Gateway.

**Checkpoint M10:** the browser plays live from `:8888` at under ~2 s glass-to-glass
(measure with the §10.1 stopwatch method), while the S3 archive path continues
independently. At this point KVS is optional in the design rather than load-bearing.

| Effort | Phase | Time |
|---|---|---|
| M10 | MediaMTX LL-HLS + latency measurement | 1–2 h |

---

### What you gave up, and should say so

Server-side packaging (~300 lines you now own), dual producer/server timestamps (store
both yourself), fragment-level metadata, sub-10-second live from the archive path, and
the guarantee that someone else maintains the packager. Roughly two to four weeks of
production engineering, traded for a 4.7× reduction in per-camera COGS that breaks even
near 700 cameras over a year (`COSTS.md` §7).

---

---

## 18. Appendix A — audio track from the PW310 microphone

Optional, but it makes the demo feel like a product rather than a lab rig. The PW310 has
a built-in microphone that shows up as a separate ALSA card.

### 18.1 Find the card by name, not by number

Card numbers shift on reboot — `hw:3,0` is not stable.

```bash
arecord -l
arecord -L | grep -i -A2 cam
# use the persistent form:
arecord -D hw:CARD=CAM310,DEV=0 -f S16_LE -r 48000 -c 2 -d 5 test.wav
aplay test.wav
```

### 18.2 Muxed A/V straight into kvssink

KVS accepts H.264 video plus AAC audio in one stream. For this variant, bypass the RTSP
hop — carrying synchronised audio through MediaMTX adds complexity for no architectural
gain:

```bash
sudo apt install -y gstreamer1.0-libav

gst-launch-1.0 -v \
  v4l2src device="$CAM" ! image/jpeg,width=1280,height=720,framerate=30/1 ! \
    jpegdec ! videoconvert ! video/x-raw,format=I420 ! \
    v4l2h264enc extra-controls="controls,video_bitrate=1500000,h264_i_frame_period=60,repeat_sequence_header=1" ! \
    "video/x-h264,level=(string)4" ! h264parse config-interval=-1 ! queue ! kvs.video_0 \
  alsasrc device=hw:CARD=CAM310 ! audioconvert ! audioresample ! \
    audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=64000 ! aacparse ! queue ! kvs.audio_0 \
  kvssink name=kvs stream-name="cam-01" aws-region="eu-central-1" \
    iot-certificate="iot-certificate,endpoint=...,cert-path=...,key-path=...,ca-path=...,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"
```

### 18.3 Caveats

- AAC is required; KVS will not accept raw PCM or Opus in the archived-media path.
- Audio adds ~64 kbps — negligible against 1.5 Mbps of video, but it does change the HLS
  manifest, so re-test the browser client after enabling it.
- A/V sync depends on both sources sharing the pipeline clock. If lip-sync drifts, set
  `alsasrc provide-clock=false` and let the video source drive.
- Check whether recording audio is lawful for your use case before putting it in a
  portfolio video of a real space — in Germany this is not a formality.

---

## 19. Appendix B — observed architecture of the reference product

Evidence gathered from the public Videoloft demo (`app.videoloft.com`) using Firefox
DevTools → Network, August 2026. **One demo camera, one session.** Treat as indicative,
not as documentation; vendors change implementations without notice.

Value of this appendix: several parameters in `COSTS.md` were derived from cost
arithmetic before this capture existed. Some were confirmed, one was corrected, and one
turned out to be unobservable. Recording all three is more honest — and more useful — than
recording only the hits.

### 19.1 Playback endpoint anatomy

```
https://useast1-video.manything.com
  /stream/mpegts/1287768/7/1786952510/1786952600/1287768.7_01080-nr
  ?inputStream=true&ignoreMvhd=true&padding=true&vcodec=h264&token=<...>
       │        │       │  │        │           │
       │        │       │  │        │           └─ resolution/profile hint (_01080)
       │        │       │  │        └───────────── end epoch (s)
       │        │       │  └────────────────────── start epoch (s)
       │        │       └───────────────────────── camera index
       │        └───────────────────────────────── account id
       └────────────────────────────────────────── container as a PATH segment
```

- **Time index confirmed.** Epoch seconds address media directly, as predicted in §17 M2.
- **Container is a path segment**, so `/stream/mp4/` and others almost certainly exist —
  format is negotiated per request.
- **`ignoreMvhd`** refers to the MP4 Movie Header Box. Instructing the packager to
  disregard it implies the *stored* format is MP4-based and MPEG-TS is generated on read.
- **Arbitrary ranges.** `1786952510 mod 60 = 50` — the request is not aligned to any
  boundary. The client asks for whatever window it wants.

### 19.2 Response headers

```
content-type: video/mp2t          x-vl-codec: h264
Version: HTTP/2                   x-vl-seek: 1.144
strict-transport-security: 3600   x-vl-start: 0
access-control-allow-origin: *    x-vl-padding: []
                                  x-vl-transcoded: false
```

| Header | What it tells you |
|---|---|
| `x-vl-seek: 1.144` | Requested start fell **inside** a stored object; packager skipped 1.144 s and began at the nearest prior keyframe. Implies a GOP of ~1–2 s. |
| `x-vl-transcoded: false` | Media is **passed through**, container remuxed only. No decode/encode. The flag's existence implies transcode is available when a client can't handle the stored codec. |
| `x-vl-start`, `x-vl-padding` | Alignment and pre-roll controls, exposed to the player via `access-control-expose-headers` so it can correct its own timeline. |
| No `via:`/`age:`/`x-cache:` | **No CDN.** Arbitrary ranges are near-uncacheable, so they pay full origin egress — a rational trade for flexibility. |

### 19.3 Auth token

Base64url payload, dot, HMAC signature:

```
1325045 : 1787211353 : 1200 : 3 : L,W,A
   user      issued     TTL   ver   scopes
```

A **stateless signed token** — 20-minute TTL, verifiable at the edge with no database
lookup. Functionally equivalent to CloudFront signed URLs, hand-rolled. Because the
credential is in the URL, the short TTL is doing real work.

**Two transports, one credential.** A later capture showed the same token in header form
on API calls:

```
Authorization: ManythingToken MTMyNTA0NToxNzg3MjE0MDg3OjEyMDA6MzpMLFcsQQ..*q6eYD1...
                              → 1325045 : 1787214087 : 1200 : 3 : L,W,A
```

Same user, same TTL, same scopes — issued 2,734 s after the URL-borne one, with a
different signature. So the design is deliberate:

| Transport | Used for | Why |
|---|---|---|
| `?token=` query parameter | media fetches | MSE/`<video>` fetches cannot reliably carry custom headers; a URL-borne token survives being handed to a media element |
| `Authorization: ManythingToken` | JSON API calls | not written to access logs, not leaked via `Referer` — safer where there is no constraint |

Renewal is explicit: `GET useast1-auth-1/refresh?time=<epoch_ms>` reissues before expiry,
which is how a 53-minute session runs on 20-minute tokens.

This is a third option alongside the CloudFront signed cookies proposed in §17 M4 —
signed URL for media, bearer header for control — and it sidesteps cookie scoping
entirely. Worth considering for the S3 path.

Note `access-control-allow-origin: *` alongside `access-control-allow-credentials: true`
— a pairing browsers reject for credentialed requests. It works only because auth rides
in the URL or an explicit header rather than in cookies, making the `credentials` flag
vestigial.

### 19.4 Service topology

| Host | Role |
|---|---|
| `useast1-auth-1` | sessions, `alog` (audit log) |
| `useast1-logger-<NNNNNNN>` | per-camera status — **numbered per camera** |
| `useast1-video` | media chunks and JPEG thumbnails |
| `useast1-analytics` | motion/object markers |

Two observations worth more than the rest:

**Connection affinity is visible in DNS.** Camera `uidd=1313196.21` talks to
`logger-1772626`; `uidd=1327405.2` talks to `logger-1772631`. The client is told which
backend node holds that camera's session and addresses it directly, rather than hitting a
load balancer that must then route internally. This is exactly the problem described in
§16.7 as the hidden cost of a self-built control plane — and here is their answer,
exposed in the hostnames.

**`sessions?uidd=…&startBefore=…`** — they query *recording sessions*, not segments. One
8.65 kB response draws the entire availability bar. That is the compact-index pattern of
§17, and `startBefore` is a backwards paging cursor.

Thumbnails are separate artifacts keyed by epoch second (`180-1786952029`), so timeline
previews and the event list render without touching a video byte.

### 19.5 Measured figures

| Quantity | Value |
|---|---|
| Media response size | 9.90–13.98 MB across ~25 samples in two sessions |
| — quiescent cluster | 9.90–10.19 MB (0.88–0.91 Mbps) |
| — motion outliers | 13.83–13.98 MB (1.23–1.24 Mbps), ~+40 % |
| Response duration | 90 s (from URL epoch range) |
| **Derived bitrate** | **~0.9 Mbps** quiescent at 2 MP / 10 fps |
| Bits per pixel per frame | 0.0434 |
| Session totals | 84 req / 94.63 MB; 197 req / 373.38 MB over 53 min |
| Media share of bytes | **>98 %** |

The outliers correlate with vehicle markers on the timeline — content-driven VBR, not a
different window length. **Planning figure: budget 10–15 % above the quiescent bitrate**
for a scene with intermittent activity; a busy daytime scene would sit near the top of
the observed range continuously rather than occasionally.

The 53-minute session averaged 0.94 Mbps of fetch across its whole duration, i.e. the
player pulls close to real time rather than buffering far ahead — sensible for a
scrubbing UI, and it means viewing cost accrues roughly linearly with watch time.

That last row is the cost model in one observation: media dominates so completely that
control-plane efficiency is irrelevant to the bill. Note also that every GET is preceded
by a CORS `OPTIONS` preflight — a doubling of control-plane requests that costs nothing
against 94 MB of media.

### 19.6 Scorecard against our design

| Prediction (this plan) | Outcome |
|---|---|
| Time index in the key/URL (§17 M2) | **Confirmed** — epoch seconds in the path |
| Compact index rather than per-segment listing (§17 M4) | **Confirmed** — `sessions` endpoint |
| Not KVS (`COSTS.md` §5) | **Confirmed** — custom packager, no KVS headers or URL shape |
| Pass-through, not transcode (§16.3b) | **Confirmed** — `x-vl-transcoded: false` |
| 1.5 Mbps for 2 MP @ 10 fps | **Corrected** — measured 0.9 Mbps (`COSTS.md` §3.1) |
| 60 s segments (`COSTS.md` §6.2) | **Unobservable** — read path decoupled from storage |
| Static objects + generated HLS playlists (§17 M4) | **Differs** — dynamic range API for archive |
| fMP4/CMAF (§17 M6) | **Split** — MPEG-TS for archive, **fMP4 for live** |
| Separate live and archive paths (§17 M0) | **Confirmed** — and more cleanly separated than proposed |
| Live via KVS or WebRTC (§17 M0) | **Corrected** — they use LL-HLS with ~1 s parts (§19.9) |
| One stream profile per camera | **Corrected** — three profiles: archive, preview, full live |

The last two are worth defending rather than conceding. Pre-generated playlists over
static objects are **cacheable**; an arbitrary-range API is not, which is why they run
without a CDN and pay full origin egress. Their design buys frame-accurate range requests
and format negotiation. Ours buys CDN economics and a much simpler serving tier. Both are
defensible; the trade should be stated explicitly.

### 19.7 An observed defect

Worth recording, because reading someone else's production errors is instructive:

```
GET  https://useast1-video.manything.com/images/aithumbs/1287768/7/undefined
     Status: 500     content-length: 0
     (preceding OPTIONS for the same path: 204)
```

The literal string `undefined` is JavaScript leaking into a URL — a variable
template-interpolated before assignment, most likely a race where the UI requests an AI
thumbnail before the analytics response supplying its identifier has landed.

The console error count rose from 2 to **31** across sessions while the same request
repeated, so this **retries in a loop** rather than failing once. A permanent 500 being
retried indefinitely is the more serious half of the defect.

Two things to take from it:

- **The server answers 500, not 400.** An unparseable path parameter is a client error; a
  500 means it reached code that threw. That is a missing input-validation layer at the
  edge — it pollutes the error budget and makes on-call noisier.
- **`aithumbs` is a distinct endpoint** from the plain `images/` thumbnails, so there are
  two thumbnail pipelines: one time-sampled for the scrubber, one event-driven for Smart
  Motion detections.

### 19.8 Caveats

- One camera, one session, one demo account.
- The camera is US-Eastern; timeline renders camera-local time.
- Infrastructure is in `us-east-1` — for a German customer that is a GDPR conversation,
  and possibly why enterprise deployments are offered differently.
- Bitrate reflects a static night scene with camera-side noise reduction. A busy daytime
  scene would be materially higher.
- `manything.com` is the legacy brand; the adapter specification is hosted there too.

### 19.9 The live path — a completely different architecture

Captured from the Live grid view (23 cameras). **This inverts the assumption in §17 M0.**

| | Playback | Live |
|---|---|---|
| Protocol | custom range API | **HLS (`.m3u8`)** |
| Container | MPEG-TS | **fMP4 / CMAF** (`_partNNN.mp4`) |
| Player | custom MSE (`chunk.js`) | **hls.js** (`hls.min.js`) |
| Host | `useast1-video.manything.com` | **raw EC2 IPs** |
| Chunk granularity | 90 s windows | **~1 s parts** |

Archive uses the bespoke packager because arbitrary ranges matter for scrubbing. Live uses
off-the-shelf HLS because standards buy player compatibility for free. Neither choice is
arbitrary.

#### Low-Latency HLS

```
GET stream.m3u8?_HLS_msn=38&_HLS_part=2
                    │           └─ part index within that segment
                    └───────────── media sequence number
```

`_HLS_msn` and `_HLS_part` are the **blocking playlist reload** parameters from the
LL-HLS specification: the client requests a playlist that does not exist yet, and the
server holds the request open until that part is ready. No polling, no fixed reload
interval.

Observed part sizes **654 B – 445 kB** (mostly 7–70 kB), incrementing roughly once per
second. Implied end-to-end latency **2–4 s**.

This is the answer to "how do you serve live when your archive chunks are 90 seconds": you
don't — you run a second, independent path. And it is cheaper than both alternatives this
plan originally proposed: no KVS ingest charge, no WebRTC signalling channels, no TURN
relay minutes. `hls.js` supports it natively, so the client side is nearly free.

#### Raw IP hosts — connection affinity, again

```
174-129-1…      3-237-223-15…      18-208-145-0…
```

Dash-encoded EC2 public IPs (all AWS `us-east-1` ranges) used directly as hostnames, four
or five distinct origins across the 23 cameras. The client is handed the address of the
specific instance packaging that stream. **No load balancer in the media path.**

The reasoning holds up: LL-HLS blocking requests hold connections open for hundreds of
milliseconds, and must land on the node holding that stream's state. An ALB would add a
hop and force cross-node coordination. The cost is no TLS termination layer, no WAF, and
a client that breaks when an instance is replaced.

Together with the `logger-NNNNNNN` hostnames of §19.4, this is the clearest available
evidence for the argument in §16.7 — a self-built control plane makes you solve connection
affinity yourself, and here it is solved twice, in two different ways, both visible from
outside.

#### A third stream profile — observed, not quantified

Session totals: **3,136 requests, 490.91 MB, 60.35 minutes, 23 cameras listed.**

> **Do not divide these.** An earlier revision of this appendix computed
> 490.91 MB ÷ 23 ÷ 60 min ≈ 0.047 Mbps and called it the preview bitrate. The request
> count refutes that: at 1-second parts each *actively streaming* camera generates
> ~3,600 part requests plus ~3,600 blocking playlist requests per hour — roughly 7,000.
> 3,136 total requests cannot represent 23 cameras streaming for an hour. The observed
> part indices (`_part125`, `_part126`, `_part127`) indicate about **two minutes** of
> streaming, and the 4–5 distinct EC2 origins suggest only **4–5 concurrent streams**.
> The 60-minute figure is wall-clock time with the tab open, not streaming time.

What can be said from the part sizes directly:

```text
7–70 kB per ~1 s part   →  0.06–0.56 Mbps       typical range
445 kB outlier          →  IDR-heavy part, or a full-resolution stream
```

So the product clearly runs **more than one live profile**, and the grid tiles (~200 px,
with a `Live stills` / `Live video` toggle) are plainly not archive-quality video. The
*direction* is certain; the *magnitude* is not measurable from this capture.

| Profile | Bitrate | Purpose | Confidence |
|---|---|---|---|
| Archive | ~0.9 Mbps | 24/7 recording, evidence | measured, ~25 samples |
| Live preview | below archive, unquantified | multi-camera grid | inferred from tile size and part sizes |
| Live full | unmeasured | single-camera focus | inferred from `vcodec`/resolution selectors |

Your MVP has one profile. Profile management remains the highest-value cost optimisation
available to it — see §19.10 for why the direction is safe to act on even without a number.

### 19.10 Cost implication of the live path

**The quantified claims previously in this section have been withdrawn.** They rested on
the division corrected above; a "20× cheaper" figure and a €85/month video-wall estimate
were both artefacts of it.

What survives, and is sufficient:

- Grid tiles are visibly ~200 px and toggle between stills and video, so preview streams
  are materially smaller than the 0.9 Mbps archive stream.
- Viewing cost is dominated by egress at ~$0.09/GB regardless of architecture
  (`COSTS.md` §4.2), so **any** reduction in preview bitrate translates directly and
  linearly into reduced viewing cost.
- The one measurement that is sound: a 53-minute *playback* session of one camera moved
  373 MB ≈ $0.034 of egress (§19.5), against ~$0.74/month to record that camera on S3.
  That break-even — roughly 20 hours of archive-quality viewing per camera-month — does
  not depend on the live-path numbers.

**To measure the preview bitrate properly:** open the Live grid, let it settle, clear the
Network panel, record exactly 60 seconds, then divide transferred bytes by the number of
tiles actually streaming (count distinct part URLs, not the camera list). Filtering on
`_part` isolates media from playlist and preflight traffic.
