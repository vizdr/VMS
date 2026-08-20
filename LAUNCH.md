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

**All four matter — a partial launch fails silently, not obviously.** A real incident
(2026-08-20): `kvs-camera-publish` was missing from an earlier version of this list.
Everything else came up "active" and *looked* healthy — `kvs-cam01.service` was even
`activating` with `Restart=on-failure` doing its job — but with nothing actually feeding
`rtsp://127.0.0.1:8554/cam01`, the whole chain was quietly producing nothing. Run all
four, then verify with **Part C**, not just `systemctl ... is-active`.

```bash
systemctl --user enable --now kvs-camera-init     # one-shot: locks exposure/WB/focus
systemctl --user enable --now kvs-mediamtx        # RTSP server
systemctl --user enable --now kvs-camera-publish  # camera → rtsp://127.0.0.1:8554/cam01 (§2.8)
systemctl --user enable --now kvs-agent           # MQTT control agent (adapter-01)
```

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

**Check in this order — `systemctl ... is-active` alone is not proof of anything.** All
four units can report `active` while the stream is genuinely dead (§2.8's incident). Only
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

**URL:** http://vms-demo-client-596633517506.s3-website.eu-central-1.amazonaws.com

**Login:** username `demo-viewer`, password `DemoViewer2026!`

This is a genuinely public URL, reachable from anywhere (no VPN, no router changes, no
geographic restriction — that's the point). Sign in, press **Start** if the stream isn't
already live, wait a few seconds for the first HLS segments to land, then **Reload
player** if it doesn't auto-recover from the initial buffering.

---

## Part E — Stop everything / cost control

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
