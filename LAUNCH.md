# LAUNCH.md — Cloud Adapter operational runbook

Companion to `Demo-AWS-Video-revCosts4.md` (the narrative build guide). This file is the
short version: what to run to actually get the system up, after everything in the guide
has already been built once. If something here doesn't work, the guide has the full
story — including the real bugs and fixes found while building this — search it for the
matching section number.

**Current live values for this deployment** (Account `596633517506`, region
`eu-central-1`) are baked into the commands below. If you rebuild this from scratch on a
different AWS account, every ID here changes — see the guide's §1–§8 for how each one is
created.

---

## Part A — One-time setup

Skip this section entirely if the Pi already has everything built (check with
`ls $VMS_HOME/vendor/*/build/libgstkvssink.so` — if that file exists, the SDK is already
built and you only need **Part B**).

### A1. System stability hardening (§1.4) — do this before anything else

```bash
sudo apt install -y earlyoom
sudo tee /etc/default/earlyoom > /dev/null <<'EOF'
EARLYOOM_ARGS="-r 60 -m 20 -s 95 --avoid '(^|/)(sshd|systemd|systemd-.*|init)$' --prefer '(^|/)(cc1plus|cc1|g\+\+|gcc|cpp|as|ld|make|cmake)$'"
EOF
sudo systemctl enable --now earlyoom

sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile
sudo swapon -p 10 /swapfile
echo "/swapfile none swap sw,pri=10 0 0" | sudo tee -a /etc/fstab
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-low-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-low-swappiness.conf

sudo rpi-eeprom-update -a && sudo reboot   # only if an update is actually staged
```

### A2. Environment (persist to `~/.bashrc`, then `source ~/.bashrc`)

```bash
export VMS_HOME=$HOME/MyProjects/VMS
export KVS_SDK=$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp
export GST_PLUGIN_PATH=$KVS_SDK/build
export LD_LIBRARY_PATH=$KVS_SDK/open-source/local/lib:$LD_LIBRARY_PATH
export AWS_REGION=eu-central-1
export KVS_STREAM=cam-01
export THING_NAME=adapter-01
```

**Non-interactive shells (systemd units, this file's own scripts) do NOT source
`.bashrc`** — every unit file below sets `GST_PLUGIN_PATH`/`LD_LIBRARY_PATH` explicitly
for this reason (§4.3).

### A3. Build the KVS Producer SDK (§4) — the long step, budget 1.5–2.5h

```bash
sudo apt install -y cmake m4 git build-essential pkg-config \
  libssl-dev libcurl4-openssl-dev liblog4cplus-dev \
  gstreamer1.0-plugins-base-apps gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gstreamer1.0-tools libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
# NOTE: gstreamer1.0-omx-generic from the original guide text does not exist on
# current Debian trixie — already dropped from this list.

mkdir -p "$VMS_HOME/vendor" && cd "$VMS_HOME/vendor"
git clone https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp.git
cd amazon-kinesis-video-streams-producer-sdk-cpp && mkdir -p build

# Three source patches required first — see §4.2 for why each one is needed:
#  1. dependency/libkvscproducer/kvscproducer-src/CMake/Utilities.cmake:
#     remove trailing " --parallel" from the `cmake --build .` line
#  2. dependency/libkvscproducer/kvscproducer-src/CMake/Dependencies/libopenssl-CMakeLists.txt:
#     add `GIT_SUBMODULES ""` to the ExternalProject_Add(project_libopenssl ...) block
#  3. dependency/libkvscproducer/kvscproducer-src/dependency/libkvspic/kvspic-src/CMakeLists.txt:
#     add `if(UNIX AND NOT APPLE)\n  add_definitions(-D_GNU_SOURCE)\nendif()` after the
#     SDK_VERSION/DETECTED_GIT_HASH add_definitions() calls (GCC 14 compat)

loginctl enable-linger "$USER"   # one-time; lets this survive a lost SSH/VS Code session
cd "$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp/build"
systemd-run --user --unit=kvs-build --collect \
  --working-directory="$PWD" \
  taskset -c 1,2 bash -c 'cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=ON \
    -DPARALLEL_BUILD=OFF -DCMAKE_BUILD_TYPE=Release > build.log 2>&1 && \
    make -j1 >> build.log 2>&1; echo "EXIT_CODE=$?" >> build.log'
# check on it: systemctl --user status kvs-build ; tail -f build.log
```

**Checkpoint:** `gst-inspect-1.0 kvssink` prints element details, not "No such element."

### A4. AWS resources (§3, §6, §8) — create once per AWS account

Already created for this account — see `$VMS_HOME/cloud/` for every JSON policy document
used. In order: KVS stream (`cam-01`, 24h retention) → IAM role `KVSAdapterRole` + role
alias `KVSAdapterRoleAlias` → IoT Thing `adapter-01` + X.509 cert (in
`$VMS_HOME/certs/`, gitignored) + `KVSAdapterThingPolicy` → Cognito user pool
`kvs-demo-users` → Lambdas `get-hls-url` / `publish-cmd` → API Gateway `kvs-demo-api` →
S3 static site `vms-demo-client-596633517506`. Full commands for each are in the guide's
§3/§6/§8 — do not re-run them against this account, they'd fail on "already exists."

---

## Part B — Launch (every session / after a reboot)

Everything below is a proper systemd unit — nothing here needs a manually-run background
process anymore.

**A partial launch fails silently, not obviously.** A real incident
(2026-08-20): `kvs-camera-publish` was missing from an earlier version of this list.
Everything else came up "active" and *looked* healthy — `kvs-cam01.service` was even
`activating` with `Restart=on-failure` doing its job — but with nothing actually feeding
`rtsp://127.0.0.1:8554/cam01`, the whole chain was quietly producing nothing. Run them
all, then verify with **Part C**, not just `systemctl ... is-active`.

```bash
systemctl --user enable --now kvs-camera-init     # one-shot: locks exposure/WB/focus
systemctl --user enable --now kvs-mediamtx        # RTSP server
systemctl --user enable --now kvs-camera-publish  # camera → rtsp://127.0.0.1:8554/cam01 (§2.8)
systemctl --user enable --now kvs-agent           # MQTT control agent (adapter-01)
systemctl --user enable --now onvif-admin         # local camera admin GUI, port 8080 (Part E)
systemctl --user enable --now kvs-event-watcher   # ONVIF detection -> evidence clips
```

The last two are additions since the original list. `onvif-admin` is only needed when you
want to discover/register/control cameras (Part E); `kvs-event-watcher` only does anything
for a camera whose `recordingMode` is a detection mode — it idles otherwise, at no cost.

That's it — the actual KVS producer (`kvs-cam01.service`, a **system** unit, not user) is
deliberately *not* auto-started here. It's controlled on demand by the agent, either via
MQTT or the browser client's Start/Stop buttons — and once `kvs-camera-publish` is up, its
own `Restart=on-failure` will pick it up automatically if it was already crash-looping
against a missing RTSP source. To start it directly without the agent:

```bash
sudo systemctl start kvs-cam01.service   # or: aws iot-data publish --topic adapter/adapter-01/cmd \
                                          #     --cli-binary-format raw-in-base64-out \
                                          #     --payload '{"action":"start"}' --region eu-central-1
```

**If any unit fails to start**, check in this order: `who -b` / `uptime` (did the Pi just
crash-reboot? see §1.4), `journalctl --user -u <unit> -n 50`, `free -h` (memory
pressure), `sudo systemctl is-active earlyoom nftables` (should both be `active`).

---

## Part C — Verify

**Check in this order — `systemctl ... is-active` alone is not proof of anything.** Every
unit can report `active` while the stream is genuinely dead (§2.8's incident). Only
the first two commands below actually prove media is flowing; the systemd check at the
end is a secondary sanity check, not the primary one.

```bash
# 1. camera → RTSP (Checkpoint 1) — the real proof local capture is working
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam01

# 2. KVS stream is receiving live media (Checkpoints 4/5) — needs kvs-cam01.service active
EP=$(aws kinesisvideo get-data-endpoint --stream-name cam-01 --region eu-central-1 \
  --api-name GET_HLS_STREAMING_SESSION_URL --query DataEndpoint --output text)
URL=$(aws kinesis-video-archived-media get-hls-streaming-session-url \
  --endpoint-url "$EP" --region eu-central-1 --stream-name cam-01 --playback-mode LIVE \
  --query HLSStreamingSessionURL --output text)
ffprobe "$URL"   # a fresh creation_time in the output is the actual proof, not just HTTP 200

# 3. systemd units — a secondary check, not a substitute for 1 and 2
systemctl --user list-units 'kvs-*' --no-pager
sudo systemctl is-active kvs-cam01.service
```

---

## Part D — Access the browser client (§8, Checkpoint 7)

**URL:** https://dugyd3kkt36pw.cloudfront.net  (CloudFront + TLS, §8.5.1)

**Login:** username `demo-viewer`, password `DemoViewer2026!`

The old plain-HTTP S3 website URL
(`http://vms-demo-client-596633517506.s3-website.eu-central-1.amazonaws.com`) still
works today — the bucket is still public pending the cutover in §8.5.1's last step
(swap to the OAC-only bucket policy, enable Block Public Access, `delete-bucket-website`).
Until that runs, there is still an unencrypted way to reach the page. Prefer the HTTPS
URL, and expect some browsers to complain about the HTTP one.

This is a genuinely public URL, reachable from anywhere (no VPN, no router changes, no
geographic restriction — that's the point). Sign in, press **Start** if the stream isn't
already live, wait a few seconds for the first HLS segments to land, then **Reload
player** if it doesn't auto-recover from the initial buffering.

---

## Part E — Add an ONVIF camera (local admin GUI)

**URL:** http://192.168.178.53:8080 — LAN only, no login. If it isn't up:
`systemctl --user enable --now onvif-admin`.

**Why this is a separate local app and not part of the browser client:** WS-Discovery is
UDP multicast. It only works from a process on the same LAN segment as the cameras — the
cloud client is served from S3 and reached over the internet, and Lambda has no route to
your LAN at all. Discovery and registration therefore have to run on the Pi (guide
§16.2.1).

**Prerequisite:** the camera must be on the *same broadcast domain* as the Pi. Multicast
does not cross routers or VLANs by design, so a camera on a different subnet will never
answer a scan no matter how long you wait.

### E1. Discover

1. Enter the camera's **ONVIF username / password** (for the existing camera: `admin`).
2. Press **Scan LAN**.

Each device that answers shows its XAddrs, ONVIF scopes, and — because credentials were
supplied — its manufacturer/model, media profile and a real RTSP URL. A camera already in
the registry is labelled **"Registered as cam-NN"** and its button reads *Re-register*
instead of *Register*.

Same thing from the CLI, useful when the GUI is not running:

```bash
venv-adapter/bin/python3 adapter/bin/discover-onvif.py --user admin --password *** --timeout 5
```

### E2. Register

Press **Register this camera**, check the pre-filled fields, and give it an ID matching
`cam-NN` (e.g. `cam-03`). One click then does all of this:

| Step | What happens |
|---|---|
| MediaMTX path | added **live** via its local API — no config rewrite, no restart, so other cameras keep streaming |
| systemd | `/etc/adapter/channels/camNN.env` written, then `kvs-cam@camNN.service` enabled (templated unit, guide §16.6) |
| KVS | stream `cam-NN` created, 24 h retention |
| Registry | row written to the `cameras` DynamoDB table |

That registry row is the single source of truth: the cloud client, the MQTT control plane
and every camera-aware Lambda read it, so a camera registered here works **everywhere
immediately, with no code change and no redeploy**.

### E3. Re-register an existing camera

Use this when a camera's IP moved (no DHCP reservation) or its credentials changed. It
updates the MediaMTX path source and the registry row — and deliberately **does not touch
systemd**, because `cam-01`/`cam-02` predate the `kvs-cam@` template and re-provisioning
them would start a second, conflicting producer for the same KVS stream.

### E4. Control, from the same table

| Column | Does what |
|---|---|
| **Local preview** | live video straight from MediaMTX (port 8888) — no cloud round trip, works before AWS is involved at all |
| **KVS push** | polls `systemctl is-active` for the producer — actual state, not what a button last claimed |
| **Recording** | `manual` / `motion` / `cellMotion` / `human` — consumed by `kvs-event-watcher` |
| **Start/Stop Remote** | starts/stops the KVS producer, i.e. what costs money |
| **IR Auto/Off/On** | day-night switch, where the camera supports it |

Motion analytics (sensitivity, cell mask, alarm delays) are shown **read-only** in section
3 of the page. That is not a UI shortcut: `SetVideoAnalyticsConfiguration` is a silent
no-op on this camera — it returns success and changes nothing (verified). Change those in
the camera's own web UI.

### E5. Verify — same rule as Part C

`systemctl is-active` is not proof. After registering:

```bash
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/camNN     # is the camera actually feeding MediaMTX?
systemctl is-active kvs-cam@camNN.service                   # is the producer up?
```

Then Part C's KVS check to confirm media is reaching the cloud.

### Gotchas found the hard way

- **Detection recording needs the producer running.** `clip_to_s3` cuts `ts-12s..ts+33s`
  from KVS, and KVS only returns footage it already ingested. Arming a detection mode
  while the stream is stopped produces triggers with no footage behind them — the clip
  fails and the only trace is a Lambda log. Start the stream first.
- **A clip takes ~40 s to appear** after a detection (38 s post-roll so the full window
  exists, plus Lambda time), then up to 30 s more before the browser client announces it.
  Not a fault — pressing Refresh sooner simply finds nothing.
- **Registration is not idempotent against a half-finished attempt.** If provisioning
  fails the MediaMTX path is rolled back, but check `/etc/adapter/channels/` before
  retrying with the same ID.

---

## Part F — Stop everything / cost control

Per §1.2's cost rule — never leave the producer running unattended:

```bash
sudo systemctl stop kvs-cam01.service   # stop billing (PutMedia ingest)
# camera/MediaMTX/agent can stay running; they cost nothing idle
```

Full teardown (deletes the KVS stream — recreating it takes seconds, see §11):

```bash
"$VMS_HOME/teardown.sh"   # if present; otherwise see guide §11 for the manual steps
```

---

## Known traps not obvious from a cold read

- **`kvssink: no element "kvssink"`** — you're in a shell that never sourced `.bashrc`
  (any systemd unit, most non-interactive contexts). Export `GST_PLUGIN_PATH`/
  `LD_LIBRARY_PATH` explicitly (§4.3).
- **`create-stream`/`ListFragments`/`GetHLSStreamingSessionURL` → AccessDenied** —
  you're using `kvs-demo-producer`'s deliberately scoped-down credentials (or the
  adapter's certificate) for something that needs your own admin AWS identity. `unset
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY` to fall back to your default profile.
- **`AWS_ERROR_MQTT_UNEXPECTED_HANGUP` connecting the agent** — check nothing in the
  connection declares a Last Will with `retain=True`; this IoT Core account/policy
  rejects the CONNECT outright for retained LWTs (§7.2).
- **`UnrecognizedClientException` / `security token invalid`** — check for a stray
  `AWS_SESSION_TOKEN` left from an earlier, unrelated credential export in the same
  shell; `unset` it.
- **A sudden reboot mid-build** — see §1.4 in full; the short version is `earlyoom` +
  swap + firmware update + CPU-pinning the build away from the WiFi IRQ cores
  (`isolcpus=1,2` on this kernel) fixed it.
- **"Is the stream alive?" → no, but every `systemctl` check said `active`** —
  `kvs-cam01.service` crash-loops silently against a 404 if `kvs-camera-publish` isn't
  also running; it has no way to tell "no camera feed" apart from any other transient
  failure. Always verify with Part C's `ffprobe` commands, not unit status alone (§2.8).
