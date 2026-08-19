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
```

You already have the AWS CLI, an IoT Core endpoint and a working certificate/policy
model from the earlier telemetry pipeline. Reuse the account; create *new* Thing and
certificate for the adapter so the policies stay cleanly scoped.

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

### 2.2 Pin the device node

`/dev/video0` shifts when other devices enumerate — and on a Pi it will shift, because
`bcm2835-codec` claims `/dev/video10`–`/dev/video16`. Use the stable path:

```bash
ls -l /dev/v4l/by-id/
# usb-AVerMedia_TECHNOLOGIES__Inc._Live_Streamer_CAM_310_...-video-index0
export CAM=/dev/v4l/by-id/usb-AVerMedia_TECHNOLOGIES__Inc._Live_Streamer_CAM_310_XXXX-video-index0
```

Put that in the systemd unit later, not `/dev/video0`.

### 2.3 Lock exposure — this matters more than it sounds

UVC auto-exposure lengthens integration time in dim light, and the camera silently drops
from 30 fps to 15 or even 7.5 fps to accommodate it. If you don't lock it, your latency
measurements in §10 will drift with the daylight and your GOP-vs-latency graph will be
noise.

```bash
v4l2-ctl -d "$CAM" --list-ctrls

# manual exposure, fixed gain, fixed white balance, no autofocus hunting
v4l2-ctl -d "$CAM" \
  --set-ctrl=auto_exposure=1 \
  --set-ctrl=exposure_time_absolute=250 \
  --set-ctrl=white_balance_automatic=0 \
  --set-ctrl=gain=32

# confirm the camera is actually delivering 30 fps
v4l2-ctl -d "$CAM" --set-fmt-video=width=1280,height=720,pixelformat=MJPG \
                   --set-parm=30 --stream-mmap --stream-count=300 --stream-to=/dev/null
```

That last command prints a running fps figure. If it reads 15 when you asked for 30, the
exposure is still too long — lower `exposure_time_absolute` and add light.

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

### 2.5 Capture → encode → publish to MediaMTX

Keeping MediaMTX in the design is deliberate. It preserves the **RTSP boundary** that a
real Hikvision would present, so Phases 3–9 remain untouched and the topology still
matches `OUTBOUND-CLOUD.md` §19. The camera-facing side is the only thing that changed.

```bash
cd ~ && mkdir -p mediamtx && cd mediamtx
curl -L -o mediamtx.tar.gz \
  https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_linux_arm64v8.tar.gz
tar xzf mediamtx.tar.gz && ./mediamtx &
```

`~/bin/publish-cam01.sh`:

```bash
#!/usr/bin/env bash
CAM=/dev/v4l/by-id/usb-AVerMedia_TECHNOLOGIES__Inc._Live_Streamer_CAM_310_XXXX-video-index0

gst-launch-1.0 -v \
  v4l2src device="$CAM" ! \
  image/jpeg,width=1280,height=720,framerate=15/1 ! \
  jpegdec ! videoconvert ! video/x-raw,format=I420 ! \
  v4l2h264enc extra-controls="controls,video_bitrate=1000000,h264_i_frame_period=30,repeat_sequence_header=1" ! \
  video/x-h264,level=(string)4 ! \
  h264parse config-interval=-1 ! \
  rtspclientsink location=rtsp://127.0.0.1:8554/cam01 protocols=tcp
```

If the PW310 does not offer MJPG at 15 fps (check §2.1 output), request 30 and decimate
after decode — `drop-only` prevents frame duplication:

```bash
  ... ! jpegdec ! videorate drop-only=true ! video/x-raw,framerate=15/1 ! videoconvert ! ...
```

Source-side 15 fps is preferable when available: it halves the USB bandwidth as well as
the encode load. See §2.7 for why 15 and not 30 or 10.

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

**Checkpoint 1:** the PW310's live picture is served as H.264 over RTSP on the Pi, at a
stable 15 fps with a 2-second IDR interval.

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
budget **45–90 minutes** on a Pi 4B. Do it once, then never again.

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
cd ~ && git clone --recursive \
  https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp.git
cd amazon-kinesis-video-streams-producer-sdk-cpp
mkdir -p build && cd build

cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=ON -DCMAKE_BUILD_TYPE=Release
make -j2      # -j4 will OOM on a 4 GB Pi during the OpenSSL/curl builds
```

Use `-j2`. The parallel dependency builds are memory-hungry and `-j4` on a 4 GB board
tends to end in the OOM killer halfway through.

### 4.3 Register the plugin

```bash
cat >> ~/.bashrc <<'EOF'
export KVS_SDK=$HOME/amazon-kinesis-video-streams-producer-sdk-cpp
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

Create a user `kvs-demo-producer` with this inline policy (substitute your ARN):

```json
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
    "Resource": "arn:aws:kinesisvideo:eu-central-1:<ACCOUNT>:stream/cam-01/*"
  }]
}
```

### 5.2 Run the pipeline

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
      iot-certificate="iot-certificate,endpoint=c2xxxx.credentials.iot.eu-central-1.amazonaws.com,cert-path=/home/pi/certs/adapter.cert.pem,key-path=/home/pi/certs/adapter.private.key,ca-path=/home/pi/certs/cacert.pem,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"
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
User=pi
Environment=GST_PLUGIN_PATH=/home/pi/amazon-kinesis-video-streams-producer-sdk-cpp/build
Environment=LD_LIBRARY_PATH=/home/pi/amazon-kinesis-video-streams-producer-sdk-cpp/open-source/local/lib
ExecStart=/home/pi/bin/stream-cam01.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Put the `gst-launch-1.0` command from §6.5 into `~/bin/stream-cam01.sh`.

### 7.2 Control agent

```bash
python3 -m venv ~/venv-adapter && source ~/venv-adapter/bin/activate
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
    endpoint="xxxxx-ats.iot.eu-central-1.amazonaws.com",
    port=443,                       # ALPN x-amzn-mqtt-ca — traverses HTTPS-only firewalls
    cert_filepath="/home/pi/certs/adapter.cert.pem",
    pri_key_filepath="/home/pi/certs/adapter.private.key",
    ca_filepath="/home/pi/certs/AmazonRootCA1.pem",
    client_id=THING,
    keep_alive_secs=30,
    clean_session=False,
    will=mqtt.Will(topic=STATE_T,
                   qos=mqtt.QoS.AT_LEAST_ONCE,
                   payload=json.dumps({"online": False}).encode(),
                   retain=True),
)
conn.connect().result()
conn.subscribe(topic=CMD_T, qos=mqtt.QoS.AT_LEAST_ONCE, callback=on_message)[0].result()
conn.publish(topic=STATE_T, payload=json.dumps({"online": True}),
             qos=mqtt.QoS.AT_LEAST_ONCE, retain=True)
threading.Event().wait()
```

The SDK's `mtls_from_path` connection already implements exponential-backoff reconnect
(§14 of the source doc) — you do not write that yourself, but you should be able to say
what it does.

Test from the console's MQTT test client:

```json
{"action": "start"}
```

**Checkpoint 6:** publishing to `adapter/adapter-01/cmd` starts and stops the stream, and
killing the agent with `kill -9` makes the LWT `{"online": false}` appear.

### 7.3 Prove the 443 claim

Don't assert it — demonstrate it. Block 8883 outright and confirm the adapter still works:

```bash
sudo iptables -A OUTPUT -p tcp --dport 8883 -j DROP
sudo systemctl restart adapter-agent.service
journalctl -u adapter-agent -f          # should connect normally

# confirm the negotiated ALPN protocol
openssl s_client -connect xxxxx-ats.iot.eu-central-1.amazonaws.com:443 \
  -alpn x-amzn-mqtt-ca -brief </dev/null 2>&1 | grep -i alpn
```

Leave that iptables rule in place permanently on the demo box. It makes the firewall
claim unfalsifiable — the prototype provably needs nothing but outbound 443.

---

## 8. Phase 7 — Browser client

Reuse your existing API Gateway + Lambda pattern; only the payload changes.

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

Execution role needs `kinesisvideo:GetDataEndpoint`,
`kinesisvideo:GetHLSStreamingSessionURL`, `kinesisvideo:DescribeStream` on the stream ARN.

Guard the API Gateway route with a **Cognito user pool authorizer** — the "authorization"
and "session management" bullets of §20 are otherwise just words on a slide.

### 8.2 Client

Single HTML file, hls.js from CDN:

```html
<video id="v" controls autoplay muted playsinline width="960"></video>
<button onclick="cmd('start')">Start</button>
<button onclick="cmd('stop')">Stop</button>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
const API = "https://xxxx.execute-api.eu-central-1.amazonaws.com/prod";
async function load() {
  const r = await fetch(`${API}/hls`, {headers: {Authorization: idToken}});
  const {url} = await r.json();
  if (Hls.isSupported()) { const h = new Hls(); h.loadSource(url); h.attachMedia(v); }
  else { v.src = url; }                       // Safari plays HLS natively
}
async function cmd(action) {
  await fetch(`${API}/cmd`, {method: "POST",
    headers: {Authorization: idToken},
    body: JSON.stringify({action})});
}
load();
</script>
```

The `/cmd` route points at a second Lambda calling `iot-data.publish()` onto
`adapter/adapter-01/cmd`.

**Checkpoint 7:** live video in the browser, on mobile data, with no router changes.
That last detail is the demo. Show it on a phone with Wi-Fi off.

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
vms-cloud-adapter/
├── README.md                  # architecture diagram + the numbers from §10
├── adapter/
│   ├── agent.py
│   ├── publish-cam01.sh        # PW310 → v4l2h264enc → MediaMTX
│   ├── camera-init.sh          # v4l2-ctl exposure/WB lock
│   ├── stream-cam01.sh         # MediaMTX → kvssink
│   ├── cam01-publish.service
│   ├── kvs-cam01.service
│   └── requirements.txt
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
ExecStart=/home/pi/bin/stream-channel.sh
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

---

## 17. Appendix — audio track from the PW310 microphone

Optional, but it makes the demo feel like a product rather than a lab rig. The PW310 has
a built-in microphone that shows up as a separate ALSA card.

### 17.1 Find the card by name, not by number

Card numbers shift on reboot — `hw:3,0` is not stable.

```bash
arecord -l
arecord -L | grep -i -A2 cam
# use the persistent form:
arecord -D hw:CARD=CAM310,DEV=0 -f S16_LE -r 48000 -c 2 -d 5 test.wav
aplay test.wav
```

### 17.2 Muxed A/V straight into kvssink

KVS accepts H.264 video plus AAC audio in one stream. For this variant, bypass the RTSP
hop — carrying synchronised audio through MediaMTX adds complexity for no architectural
gain:

```bash
sudo apt install -y gstreamer1.0-libav

gst-launch-1.0 -v \
  v4l2src device="$CAM" ! image/jpeg,width=1280,height=720,framerate=30/1 ! \
    jpegdec ! videoconvert ! video/x-raw,format=I420 ! \
    v4l2h264enc extra-controls="controls,video_bitrate=1500000,h264_i_frame_period=60,repeat_sequence_header=1" ! \
    video/x-h264,level=(string)4 ! h264parse config-interval=-1 ! queue ! kvs.video_0 \
  alsasrc device=hw:CARD=CAM310 ! audioconvert ! audioresample ! \
    audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=64000 ! aacparse ! queue ! kvs.audio_0 \
  kvssink name=kvs stream-name="cam-01" aws-region="eu-central-1" \
    iot-certificate="iot-certificate,endpoint=...,cert-path=...,key-path=...,ca-path=...,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"
```

### 17.3 Caveats

- AAC is required; KVS will not accept raw PCM or Opus in the archived-media path.
- Audio adds ~64 kbps — negligible against 1.5 Mbps of video, but it does change the HLS
  manifest, so re-test the browser client after enabling it.
- A/V sync depends on both sources sharing the pipeline clock. If lip-sync drifts, set
  `alsasrc provide-clock=false` and let the video source drive.
- Check whether recording audio is lawful for your use case before putting it in a
  portfolio video of a real space — in Germany this is not a formality.
