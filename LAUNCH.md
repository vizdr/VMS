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

Skip what's already done. On a Pi that has been set up before, this lists what's
missing and which step creates it (needs A2's environment):

```bash
for s in "A3 $KVS_SDK/build/libgstkvssink.so" \
         "A4 $VMS_HOME/venv-adapter/bin/python3" \
         "A5 $VMS_HOME/mediamtx/mediamtx" \
         "A7 $VMS_HOME/certs/adapter.private.key" \
         "A8 $HOME/.config/systemd/user/kvs-agent.service"; do
  set -- $s; [ -e "$2" ] && echo "ok       $1  $2" || echo "MISSING  $1  $2"
done
command -v aws >/dev/null && echo "ok       A6  aws CLI" || echo "MISSING  A6  aws CLI"
```

If everything is there you only need **Part B** — unless the repo was cloned to a new
folder, in which case re-run **A8** first so the unit files point at it. A fresh Pi runs
A1 → A8 in order, except that A3's build runs for hours in the background, so A4–A7 fit
inside it.

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

#### A1b. Two things a *current* Pi OS image gets wrong for this project

Neither bites on the Pi this project was built on, because its older image happens to
satisfy both. A fresh install will not. Check, don't assume.

**Persistent journal.** A current image ships
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` with `Storage=volatile`
to spare the SD card. Every reboot then loses all logs — a failed build leaves no trace —
and `journalctl --user -u <unit>` prints *"No journal files were found"* for **every**
user unit, which most checks in this file depend on. Creating `/var/log/journal` alone
does **not** work; journald ignores it while that drop-in is in force
(FoundAndFixed.md #33).

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nSystemMaxFileSize=20M\n' \
  | sudo tee /etc/systemd/journald.conf.d/90-vms-persistent.conf
sudo systemctl restart systemd-journald && sudo journalctl --flush
```

**Proof:** `ls /var/log/journal/*/system.journal` exists, and
`journalctl --user -u kvs-agent -n1` returns a line rather than "No journal files".

**Passwordless sudo.** `agent.py` and the admin GUI run `sudo systemctl start|stop` with
no terminal attached. A current image grants `(ALL : ALL) ALL` **with** a password, so
Start/Stop fails silently from the GUI. The obvious check is also misleading: the image
sets `Defaults timestamp_type=global`, so a password typed anywhere in the last ~15
minutes makes a bare `sudo -n true` succeed in every session — including services. Use
`sudo -k -n true`, which ignores the cache (FoundAndFixed.md #34).

Grant only what the adapter needs, rather than blanket NOPASSWD, and validate before
installing:

```bash
sudo visudo -c -f /etc/sudoers.d/020_vms-adapter    # check BEFORE it takes effect
sudo -k -n true && echo "passwordless sudo OK" || echo "Start/Stop will fail"
```

### A2. Environment — persisted to `~/.bashrc`

```bash
cat >> ~/.bashrc <<'EOF'
export VMS_HOME=$HOME/Projects/VideoSafeZone   # wherever you cloned the repo
export KVS_SDK=$VMS_HOME/vendor/amazon-kinesis-video-streams-producer-sdk-cpp
export GST_PLUGIN_PATH=$KVS_SDK/build
export LD_LIBRARY_PATH=$KVS_SDK/open-source/local/lib:$LD_LIBRARY_PATH
export AWS_REGION=eu-central-1
export KVS_STREAM=cam-01
export THING_NAME=adapter-01
EOF
source ~/.bashrc
```

**Proof:** `echo "$VMS_HOME" && ls "$VMS_HOME/LAUNCH.md"` prints the path, then the file.

**Non-interactive shells (systemd units, this file's own scripts) do NOT source
`.bashrc`** — every KVS producer unit in A8 sets `GST_PLUGIN_PATH`/`LD_LIBRARY_PATH`
explicitly for this reason (§4.3). `VMS_HOME` itself is the exception: the adapter's
scripts (`adapter/bin/*.sh`) and Python modules (`adapter/*.py`) use it if exported and
otherwise fall back to the repo root they live in, so certs, venv and helper paths resolve
without it. Unit files are different — they need **literal** absolute paths; see A8.

### A3. Build the KVS Producer SDK (§4) — the long step, budget 1.5–2.5h

Start this first: it runs detached for hours, and A4–A7 can be done while it builds.

```bash
sudo apt install -y cmake m4 git build-essential pkg-config \
  libssl-dev libcurl4-openssl-dev liblog4cplus-dev \
  gstreamer1.0-plugins-base-apps gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly \
  gstreamer1.0-tools gstreamer1.0-rtsp \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
# gstreamer1.0-rtsp is a SEPARATE package and is not pulled in by plugins-good/bad.
# Without it `rtspclientsink` does not exist, publish-cam01.sh fails with
# "no element rtspclientsink", and cam-01 never reaches MediaMTX -- while every
# other unit still reports active (FoundAndFixed.md #40).
# NOTE: gstreamer1.0-omx-generic from the original guide text does not exist on
# current Debian trixie — already dropped from this list.

mkdir -p "$VMS_HOME/vendor" && cd "$VMS_HOME/vendor"
[ -d amazon-kinesis-video-streams-producer-sdk-cpp ] || \
  git clone https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp.git
mkdir -p "$KVS_SDK/build"
```

**Three source patches** — §4.2 has the story behind each. Skipping 1 or 2 is not a
slow-build problem, it is a failed build: without patch 1 OpenSSL compiles with one job per
core regardless of `-j1`/`-DPARALLEL_BUILD=OFF`, memory drops under earlyoom's 20 % line,
and earlyoom (A1 tells it to prefer compilers) SIGTERMs every `cc1` at once —
`build.log` then shows a burst of `cc: fatal error: Terminated signal terminated program
cc1` and `EXIT_CODE=1`. Nothing is wrong with the code when you see that; a patch is missing.

Patches 1 and 2 — before anything is built:

```bash
P=$KVS_SDK/dependency/libkvscproducer/kvscproducer-src
# 1. nested build_dependency() hardcodes `--parallel`, which -DPARALLEL_BUILD=OFF never reaches
sed -i 's/--build \. --parallel/--build ./' "$P/CMake/Utilities.cmake"
# 2. stop OpenSSL fetching its huge fuzzing/test submodules (boringssl & co.)
grep -q GIT_SUBMODULES "$P/CMake/Dependencies/libopenssl-CMakeLists.txt" || \
  sed -i '/GIT_TAG *OpenSSL_1_1_1t/a\    GIT_SUBMODULES    ""' "$P/CMake/Dependencies/libopenssl-CMakeLists.txt"
```

**Proof:**

```bash
grep -c -- '--parallel' "$P/CMake/Utilities.cmake"                           # 0
grep -n GIT_SUBMODULES "$P/CMake/Dependencies/libopenssl-CMakeLists.txt"     # one line, right after GIT_TAG
```

**Stage 1 — configure.** With `-DBUILD_DEPENDENCIES=ON` the *configure* step is what
compiles log4cplus, OpenSSL, curl etc., and it is also what downloads the kvspic source
that patch 3 edits — so configure alone first, patch, then compile:

```bash
loginctl enable-linger "$USER"   # one-time; lets this survive a lost SSH/VS Code session
cd "$KVS_SDK/build"
systemd-run --user --unit=kvs-build --collect --working-directory="$PWD" \
  taskset -c 1,2 bash -c 'cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=ON \
    -DPARALLEL_BUILD=OFF -DCMAKE_BUILD_TYPE=Release > build.log 2>&1; \
    echo "CONFIGURE_EXIT=$?" >> build.log'
# check on it: systemctl --user status kvs-build ; tail -f build.log
```

**Proof:**

```bash
grep CONFIGURE_EXIT build.log                                      # CONFIGURE_EXIT=0
journalctl -u earlyoom --since today | grep -c 'sending SIGTERM'   # 0 — any kill means a patch is missing
```

Patch 3 — GCC 14 makes the SDK's implicit `pthread_getname_np` declaration a hard error.
The file exists only now:

```bash
F=$P/dependency/libkvspic/kvspic-src/CMakeLists.txt
grep -n 'project(\|add_definitions' "$F" | head
```

Add, by hand, straight after the `SDK_VERSION`/`DETECTED_GIT_HASH` `add_definitions()`
lines (not inside a multi-line `project(...)` call):

```cmake
if(UNIX AND NOT APPLE)
  add_definitions(-D_GNU_SOURCE)
endif()
```

**Proof:** `grep -n -A1 'UNIX AND NOT APPLE' "$F"` shows the block where you put it.

**Stage 2 — compile:**

```bash
cd "$KVS_SDK/build"
systemd-run --user --unit=kvs-build --collect --working-directory="$PWD" \
  taskset -c 1,2 bash -c 'make -j1 >> build.log 2>&1; echo "EXIT_CODE=$?" >> build.log'
```

**Proof** (each line is stronger evidence than the one before):

```bash
grep EXIT_CODE build.log                 # EXIT_CODE=0
ls -la "$KVS_SDK/build/libgstkvssink.so" # the plugin exists
gst-inspect-1.0 kvssink | head -5        # GStreamer loads it — needs A2's env vars
```

**Retrying after a failure: don't wipe `build/`.** Finished dependencies install to
`open-source/local/` (next to `build/`, not inside it) and are skipped on the next run, so
fix the cause and re-run the stage that failed. Only delete `open-source/local/lib<name>`
if one dependency is stuck half-built (§4.2).

### A4. Python venv (§7.2)

```bash
python3 -m venv "$VMS_HOME/venv-adapter"
"$VMS_HOME/venv-adapter/bin/pip" install boto3 awsiotsdk onvif-zeep-async WSDiscovery flask requests lxml
```

That is every third-party module `adapter/` imports: `boto3` (AWS APIs), `awsiotsdk`
(MQTT, `agent.py`), `onvif-zeep-async` + `lxml` (ONVIF), `WSDiscovery` (LAN discovery),
`flask` + `requests` (admin GUI).

**Proof** — imports, and the WSDL path the ONVIF code computes, resolved with `VMS_HOME`
*unset*, the way systemd runs it:

```bash
cd "$VMS_HOME"
venv-adapter/bin/python3 -c "import boto3, awsiot, awscrt, flask, requests, lxml, onvif, wsdiscovery; print('imports OK')"
env -u VMS_HOME venv-adapter/bin/python3 -c "
import sys, os; sys.path.insert(0, 'adapter'); import camera_control as c
print(c.WSDL_DIR, os.path.isdir(c.WSDL_DIR))"          # must end in True
```

### A5. MediaMTX binary (§2.5)

The repo tracks the project's own `mediamtx/mediamtx.yml`; only the binary is downloaded.
**The release tarball also contains a default `mediamtx.yml`** — a plain `tar xzf` (as in
the guide) silently overwrites the project config, so extract the binary by name:

```bash
cd "$VMS_HOME/mediamtx"
curl -fL -o mediamtx.tar.gz \
  https://github.com/bluenviron/mediamtx/releases/download/v1.20.1/mediamtx_v1.20.1_linux_arm64.tar.gz
tar xzf mediamtx.tar.gz mediamtx
```

`-f` makes a 404 fail loudly instead of saving a 9-byte "Not Found" as the tarball (the
§2.5 trap). v1.20.1 is the version `mediamtx.yml` was written against — upgrade on purpose,
not by accident.

**Proof:**

```bash
file mediamtx                                    # ELF 64-bit LSB executable, ARM aarch64
./mediamtx --version                             # v1.20.1
git -C "$VMS_HOME" status --short mediamtx/      # must NOT list mediamtx.yml as modified
# starts with the project config and the control API answers
# (skip if kvs-mediamtx is already running — the ports would clash):
( timeout 6 ./mediamtx >/dev/null 2>&1 & sleep 3; curl -s http://127.0.0.1:9997/v3/paths/list | head -c 200; echo )
```

### A6. AWS CLI — the operator's tool, not the adapter's

Used on the Pi by this runbook (Part B's `aws iot-data publish`, Part C's cloud-path check,
creating a device certificate in A7) and by the deploy commands in `CLAUDE.md`. **The
adapter's services never use it** — they authenticate with the device certificate (A7).
Not in the Raspberry Pi OS image; install AWS's own arm64 build:

```bash
cd "$(mktemp -d)"
curl -fsSL -o awscliv2.zip https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip
unzip -q awscliv2.zip && sudo ./aws/install
aws --version        # aws-cli/2.x ... aarch64
```

(Upgrading later: same commands with `sudo ./aws/install --update`.)

**Credentials — pick one:**

- **`aws configure sso`** (IAM Identity Center) — recommended. Short-lived, re-issued by
  `aws sso login`; nothing long-lived is written to the Pi.
- **`aws configure`** with an IAM user's access keys — works, but writes a static key to
  `~/.aws/credentials`: exactly the kind of credential this architecture keeps off the
  device. If you use it, give that user only what you run from here and delete the key
  when setup is done.
- **No credentials on the Pi at all** — run the account-level commands in **AWS
  CloudShell** (browser, already authenticated) and skip the credentials step here. The
  cloud-path check in Part C and `aws iot-data publish` in Part B then need CloudShell too.

Set the region either way: `aws configure set region eu-central-1`.

**Proof:**

```bash
aws sts get-caller-identity --query Account --output text   # 596633517506
aws configure get region                                     # eu-central-1
```

### A7. AWS resources and the device certificate

**AWS resources (§3, §6, §8) — once per AWS account.** Already created for this account —
see `$VMS_HOME/cloud/` for every JSON policy document used. In order: KVS stream (`cam-01`,
24h retention) → IAM role `KVSAdapterRole` + role alias `KVSAdapterRoleAlias` → IoT Thing
`adapter-01` + X.509 cert + `KVSAdapterThingPolicy` → Cognito user pool `kvs-demo-users` →
Lambdas `get-hls-url` / `publish-cmd` → API Gateway `kvs-demo-api` → S3 static site
`vms-demo-client-596633517506`. Full commands for each are in the guide's §3/§6/§8 — do not
re-run them against this account, they'd fail on "already exists."

**Device certificate — once per Pi.** `certs/` is gitignored, so a fresh clone has none.
Four files must end up in `$VMS_HOME/certs/`:

| File | What it is | Used by |
|---|---|---|
| `adapter.cert.pem` | device certificate for Thing `adapter-01` | everything |
| `adapter.private.key` | its private key — **cannot be re-downloaded from AWS** | everything |
| `cacert.pem` | Starfield root (`SFSRootCAG2`) — credentials endpoint | `kvssink`, `aws_device_creds.py` |
| `AmazonRootCA1.pem` | Amazon root CA 1 — MQTT data endpoint | `agent.py` |

The two CAs are **different** and not interchangeable; the wrong one gives a TLS error
that looks like a permissions problem (§6.4).

**Option A — copy from the previous Pi** (if it still exists; nothing changes in AWS):

```bash
scp -r <old-pi>:<old-VMS_HOME>/certs "$VMS_HOME/"
```

Never run `kvs-agent` on both Pis at the same time: both connect as MQTT client
`adapter-01`, and IoT Core drops the older session each time the other connects.

**Option B — create a new certificate** (old Pi gone, or you want a clean identity). Run
on the Pi with A6's CLI, or in CloudShell and then copy the two files to the Pi:

```bash
mkdir -p "$VMS_HOME/certs" && cd "$VMS_HOME/certs"
aws iot list-policies --query 'policies[].policyName'     # confirm KVSAdapterThingPolicy exists
CERT_ARN=$(aws iot create-keys-and-certificate --set-as-active \
  --certificate-pem-outfile adapter.cert.pem \
  --public-key-outfile adapter.public.key \
  --private-key-outfile adapter.private.key \
  --query certificateArn --output text)
aws iot attach-policy --policy-name KVSAdapterThingPolicy --target "$CERT_ARN"
aws iot attach-thing-principal --thing-name adapter-01 --principal "$CERT_ARN"
```

Once the proofs below pass, retire the old certificate so there's only one live identity:

```bash
aws iot list-thing-principals --thing-name adapter-01    # old ARN is the one ≠ $CERT_ARN
aws iot update-certificate --certificate-id <old-cert-id> --new-status INACTIVE
```

**Both options — CAs and permissions:**

```bash
cd "$VMS_HOME/certs"
curl -fsS -o cacert.pem        https://www.amazontrust.com/repository/SFSRootCAG2.pem
curl -fsS -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
chmod 700 . && chmod 600 adapter.private.key
```

**Proof** — four layers; (b) is the one that matters most:

```bash
cd "$VMS_HOME/certs"
# a) cert is valid and belongs to this key
openssl x509 -in adapter.cert.pem -noout -enddate
diff <(openssl x509 -in adapter.cert.pem -noout -pubkey) \
     <(openssl pkey -in adapter.private.key -pubout) && echo "key matches"

# b) credentials endpoint (kvssink, boto3): cert active + attached to adapter-01 + allowed to
#    assume KVSAdapterRole. Prints only the expiry, never the secret. 403 = attach step missing.
curl -fsS --cert adapter.cert.pem --key adapter.private.key --cacert cacert.pem \
  -H "x-amzn-iot-thingname: adapter-01" \
  https://c38gt2us7mrsmf.credentials.iot.eu-central-1.amazonaws.com/role-aliases/KVSAdapterRoleAlias/credentials \
  | python3 -c 'import json,sys; print("credentials OK, expire", json.load(sys.stdin)["credentials"]["expiration"])'

# c) MQTT data endpoint (agent.py), with the other CA — TLS handshake only, no MQTT session
openssl s_client -connect a3dp4umq4qv6ul-ats.iot.eu-central-1.amazonaws.com:8443 \
  -CAfile AmazonRootCA1.pem -cert adapter.cert.pem -key adapter.private.key </dev/null 2>/dev/null \
  | grep 'Verify return code'            # Verify return code: 0 (ok)

# d) end to end through the project's own code (needs A4), VMS_HOME unset like under systemd
cd "$VMS_HOME" && env -u VMS_HOME venv-adapter/bin/python3 -c "
import sys; sys.path.insert(0, 'adapter'); from aws_device_creds import get_session
print(get_session().client('sts').get_caller_identity()['Arn'])"   # …assumed-role/KVSAdapterRole/…
```

### A8. Install the systemd units — once per Pi, again whenever the clone moves

Part B only *enables and starts* units; this step creates their files. Unit files live
outside the repo and are not in git, so a fresh Pi (or a fresh clone) has none of them
until this step runs.

**Prerequisites — the units point at these, and none of them come with `git clone`:**
the built SDK (A3), the venv (A4), the MediaMTX binary (A5) and the device certificate
(A7), each with its proof passing. Writing the units first is harmless, but a unit whose
target is missing just fails and retries every 5 s until it appears.

**Two managers, two directories.** Which one a unit belongs to decides where its file
goes, which `systemctl` flavour controls it, and where its logs are:

| Unit | Manager | File | Runs |
|---|---|---|---|
| `kvs-camera-init` | user | `~/.config/systemd/user/kvs-camera-init.service` | `adapter/bin/camera-init.sh` (one-shot) |
| `kvs-mediamtx` | user | `~/.config/systemd/user/kvs-mediamtx.service` | `mediamtx/mediamtx` |
| `kvs-camera-publish` | user | `~/.config/systemd/user/kvs-camera-publish.service` | `adapter/bin/publish-cam01.sh` |
| `kvs-agent` | user | `~/.config/systemd/user/kvs-agent.service` | `adapter/agent.py` |
| `onvif-admin` | user | `~/.config/systemd/user/onvif-admin.service` | `adapter/onvif-admin/app.py` |
| `kvs-event-watcher` | user | `~/.config/systemd/user/kvs-event-watcher.service` | `adapter/event_watcher.py` |
| `kvs-outage-buffer` | user | `~/.config/systemd/user/kvs-outage-buffer.service` | `adapter/outage_buffer.py` |
| `kvs-outage-uploader` | user | `~/.config/systemd/user/kvs-outage-uploader.service` | `adapter/outage_uploader.py` |
| `kvs-cam01` | **system** | `/etc/systemd/system/kvs-cam01.service` | `adapter/bin/stream-cam01.sh` |
| `kvs-cam02` | **system** | `/etc/systemd/system/kvs-cam02.service` | `adapter/bin/stream-cam02.sh` |
| `kvs-cam@` | **system** | `/etc/systemd/system/kvs-cam@.service` (+ `/etc/adapter/channels/<path>.env` per instance) | `adapter/bin/stream-channel.sh` |

- **user** → `systemctl --user …`, no sudo, logs in `journalctl --user -u <unit>`. Starts
  at boot without a login only because of `loginctl enable-linger` (A3).
- **system** → `sudo systemctl …`, logs in `journalctl -u <unit>`. These are the KVS
  producers — the units that cost money while running (guide §1.2), so they are installed
  but **not enabled**; the agent/GUIs start them on demand. `kvs-cam@<path>` instances are
  created by the ONVIF admin GUI through `adapter/bin/provision-camera.sh` (Part E), never
  by hand.
- A unit in one manager cannot `Requires=`/`After=` a unit in the other — they are separate
  systemd instances (guide §16).

**systemd does not expand `$VMS_HOME`** — or `~`, or any shell variable. It never reads
`.bashrc`, and `ExecStart=`, `WorkingDirectory=`, `Environment=` and `EnvironmentFile=` are
taken literally, so `ExecStart=$VMS_HOME/adapter/bin/stream-cam01.sh` fails with
"Executable path is not absolute". Every path in a unit file must be the real absolute
path, e.g. `/home/vladimir/Projects/VideoSafeZone/adapter/bin/stream-cam01.sh`. (The
scripts and Python modules the units launch don't need `VMS_HOME` in their environment —
they find the repo root from their own location, see A2.)

The commands below handle that for you: the heredocs are **unquoted** (`<<EOF`, not
`<<'EOF'` as in the guide), so bash substitutes `${VMS_HOME}` and `${USER}` *while writing
the file*, and the unit on disk contains the literal path. Run them from a shell where A2's
`VMS_HOME` is set, and confirm first:

```bash
echo "$VMS_HOME"; ls "$VMS_HOME/adapter/agent.py"   # must print the path, then the file
```

User units — the first three are the guide's §2 units verbatim; the other five were never
written down in the guide and are reconstructed from the code (entry points, working
directories, imports):

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/kvs-camera-init.service <<EOF
[Unit]
Description=Lock PW310 exposure/WB/focus before streaming starts
Before=kvs-mediamtx.service

[Service]
Type=oneshot
ExecStart=${VMS_HOME}/adapter/bin/camera-init.sh
RemainAfterExit=yes

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/kvs-mediamtx.service <<EOF
[Unit]
Description=MediaMTX RTSP/HLS server for camera capture
After=network.target

[Service]
Type=simple
WorkingDirectory=${VMS_HOME}/mediamtx
ExecStart=${VMS_HOME}/mediamtx/mediamtx
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/kvs-camera-publish.service <<EOF
[Unit]
Description=PW310 capture/encode -> publish to MediaMTX (rtsp://127.0.0.1:8554/cam01)
After=kvs-camera-init.service kvs-mediamtx.service
Requires=kvs-camera-init.service kvs-mediamtx.service

[Service]
Type=simple
WorkingDirectory=${VMS_HOME}/adapter/bin
ExecStart=${VMS_HOME}/adapter/bin/publish-cam01.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# Python daemons: one template, five units. All run under the venv's interpreter
# (a bare python3 wouldn't see awsiotsdk/boto3/onvif), from the directory they live in.
py_unit() {  # name  description  working-dir  script  [extra [Unit] lines]
cat > ~/.config/systemd/user/$1.service <<EOF
[Unit]
Description=$2
$5

[Service]
Type=simple
WorkingDirectory=$3
ExecStart=${VMS_HOME}/venv-adapter/bin/python3 -u $4
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
}
py_unit kvs-agent           "MQTT control agent (adapter-01)" \
        "${VMS_HOME}/adapter"             "${VMS_HOME}/adapter/agent.py"
py_unit onvif-admin         "Local ONVIF admin GUI (port 8080)" \
        "${VMS_HOME}/adapter/onvif-admin" "${VMS_HOME}/adapter/onvif-admin/app.py"
py_unit kvs-event-watcher   "ONVIF detections -> evidence clips" \
        "${VMS_HOME}/adapter"             "${VMS_HOME}/adapter/event_watcher.py"
py_unit kvs-outage-buffer   "Durable outage buffering supervisor (OUTAGE.md)" \
        "${VMS_HOME}/adapter"             "${VMS_HOME}/adapter/outage_buffer.py" \
        "After=kvs-mediamtx.service"
py_unit kvs-outage-uploader "Backfill buffered outage footage to S3 (OUTAGE.md)" \
        "${VMS_HOME}/adapter"             "${VMS_HOME}/adapter/outage_uploader.py"

systemctl --user daemon-reload
```

System units — `sudo tee` writes the file, but the heredoc is still expanded by *your*
shell first, so `${VMS_HOME}` and `${USER}` are yours, not root's:

```bash
KVS_SDK_DIR=${VMS_HOME}/vendor/amazon-kinesis-video-streams-producer-sdk-cpp

for cam in 01 02; do
sudo tee /etc/systemd/system/kvs-cam${cam}.service > /dev/null <<EOF
[Unit]
Description=KVS producer for cam-${cam}
After=network-online.target

[Service]
Type=simple
User=${USER}
Environment=GST_PLUGIN_PATH=${KVS_SDK_DIR}/build
Environment=LD_LIBRARY_PATH=${KVS_SDK_DIR}/open-source/local/lib
ExecStart=${VMS_HOME}/adapter/bin/stream-cam${cam}.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
done

# %i stays literal — it's a systemd specifier (the instance name, e.g. cam03), not a
# shell variable, so it survives the unquoted heredoc untouched.
sudo tee /etc/systemd/system/kvs-cam@.service > /dev/null <<EOF
[Unit]
Description=KVS producer for %i
After=network-online.target

[Service]
Type=simple
User=${USER}
EnvironmentFile=/etc/adapter/channels/%i.env
Environment=GST_PLUGIN_PATH=${KVS_SDK_DIR}/build
Environment=LD_LIBRARY_PATH=${KVS_SDK_DIR}/open-source/local/lib
ExecStart=${VMS_HOME}/adapter/bin/stream-channel.sh
Restart=on-failure
RestartSec=5
CPUAccounting=true
MemoryAccounting=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
```

`kvs-cam@.service` above adds `User=` and the two `Environment=` lines that the guide's
§16.6 sketch omits — without them the producer runs as root and fails with
`No such element "kvssink"`, since `GST_PLUGIN_PATH` isn't set.

**Check what was written:**

```bash
grep -h 'ExecStart\|WorkingDirectory\|Environment' \
  ~/.config/systemd/user/{kvs-,onvif-}*.service /etc/systemd/system/kvs-cam*.service
# every path must be absolute and exist — no '$', no '~', no 'MyProjects'
systemd-analyze --user verify ~/.config/systemd/user/kvs-agent.service
systemctl --user cat kvs-agent        # what systemd actually loaded
sudo -n true && echo "passwordless sudo OK"   # agent + admin GUI call `sudo systemctl`
                                              # non-interactively (RPi OS default grants it)
```

**If the clone moves** (new folder, new Pi, different user): update `VMS_HOME` in
`~/.bashrc`, `source ~/.bashrc`, re-run this whole step (it overwrites the files), then
`systemctl --user restart` the running user units. Stale unit paths fail quietly —
`Restart=on-failure` just keeps retrying a missing file — so the `grep` check above is
the quick way to spot them.

---

### A9. Hardware that differs — devices, the LAN, and the AWS account

`VMS_HOME` (A2) solves *filesystem* portability. Three other classes of value are tied to
the machine you built on, and each moves differently.

**Camera and microphone — discovered, nothing to configure.** These used to be pinned to
one physical unit:

```bash
CAM=/dev/v4l/by-id/usb-Generic_AVerMedia_PW310_Webcam_200901010001-video-index0
AUDIO_DEV=hw:CARD=Webcam,DEV=0
```

The `by-id` path embeds the camera's **USB serial number**, so it breaks on a *different
unit of the same model* — not just a different model. `by-path` is serial-free but encodes
the physical USB port, so it breaks when you move the plug. Neither survives new hardware.

`adapter/bin/resolve-usb-camera.sh` now finds both at startup and
`publish-cam01.sh`/`camera-init.sh` use it. A UVC camera exposes two `/dev/video*` nodes
and only one captures, so it asks which node advertises a capture format rather than
guessing by index; for audio it matches "has a capture PCM and is not the Pi's onboard
sound" rather than the card *name*, and emits `hw:CARD=<id>` rather than `hw:<N>` because
card numbers are reassigned on reboot (§18.1).

**Proof — run it directly; it prints what the pipelines will use:**

```bash
adapter/bin/resolve-usb-camera.sh
#   CAM_DEVICE=/dev/v4l/by-id/usb-..._Webcam_...-video-index0
#   AUDIO_CARD=hw:CARD=Webcam,DEV=0
```

Nothing printed means nothing was found — check the camera is plugged in with
`v4l2-ctl --list-devices`. With two USB cameras attached it picks the first and says so on
stderr; export `CAM_DEVICE` / `AUDIO_CARD` to choose, or set `audioDevice` on the camera's
registry row, which takes precedence over discovery.

**The LAN — registry, not code.** The ONVIF camera's IP, port, credentials and RTSP URL
live in the `cameras` DynamoDB table, written by the local admin GUI (Part E). A camera
that moves to a new address is re-registered there; nothing in the repo is edited. The one
exception is `mediamtx/mediamtx.yml`, which carries `cam02`'s source URL as a static path
entry — re-registering rewrites it live through the control API, so edit the file only
when setting up a camera that has never been registered.

**The AWS account — one file, `/etc/adapter/adapter.env`.** Account-specific identity
(region, IoT Thing, role alias, both IoT endpoints, the evidence bucket) is **not** in the
code. `adapter/config.py` reads that file for Python; `adapter/bin/adapter-config.sh`
reads the same file for the shell scripts. Moving to another AWS account is an edit to one
file plus creating the resources (guide §3, §6, §8).

Install it from the tracked template and check the endpoints against the account you are
actually pointing at, rather than trusting the template's values:

```bash
sudo install -D -m 644 config/adapter.env.example /etc/adapter/adapter.env
sudoedit /etc/adapter/adapter.env

aws iot describe-endpoint --endpoint-type iot:CredentialProvider   # -> IOT_CRED_ENDPOINT
aws iot describe-endpoint --endpoint-type iot:Data-ATS             # -> IOT_DATA_ENDPOINT
```

**Proof — both readers agree, and a missing key fails loudly rather than defaulting:**

```bash
venv-adapter/bin/python3 -c \
  "import sys;sys.path.insert(0,'adapter');import config;print(config.IOT_DATA_ENDPOINT)"
( source adapter/bin/adapter-config.sh && echo "$IOT_DATA_ENDPOINT" )

ADAPTER_CONFIG=/nonexistent bash -c 'source adapter/bin/adapter-config.sh'
#   adapter-config: AWS_REGION is not set: add it to /nonexistent (...)
```

There are deliberately **no built-in defaults**. A missing key raises rather than silently
talking to whichever account a stale default happened to name — the failure that would be
hardest to notice and most expensive to get wrong.

Not covered by this file: the browser client (`client/index.html` carries the API Gateway
and Cognito IDs it is built against) and the `cloud/` IAM documents, which name the
account in ARNs. `grep -rn 596633517506 .` finds those.
---

## Part B — Launch (every session / after a reboot)

Everything below is a proper systemd unit — nothing here needs a manually-run background
process anymore. The unit files themselves are created in **A8**; if `systemctl --user
enable` says `Unit … not found`, that step hasn't been run on this Pi.

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
systemctl --user enable --now kvs-outage-buffer   # durable outage buffering (OUTAGE.md)
systemctl --user enable --now kvs-outage-uploader # backfills buffered footage to S3
```

**`enable --now`, not `start`** — every line above. A unit that is only `start`ed runs
until the next reboot and then silently does not come back. That is not hypothetical:
`kvs-outage-buffer` was started rather than enabled when it was first built, and a reboot
five days later left outage buffering switched off with nothing to indicate it. Part C's
checks pass in that state, because everything they look at is still healthy.

The last four are additions since the original list. `onvif-admin` is only needed when you
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

### If the camera has audio enabled (guide §18)

Audio is off by default. When it is on, two extra checks matter, because both of its
failure modes are **silent** at the level of step 1–3 above — fragments persist, both
tracks appear, and `ffprobe` is happy:

```bash
# 4. frames being rejected? want exactly 0.
#    Anything above zero is the shared-DTS trap (§18.3) and you are losing audio.
journalctl -u kvs-cam01.service --since "-60 s" | grep -c 0x30000005

# 5. is the audio actually all arriving, and is it real?
#    delivered kb/s well below the configured bitrate = frames being dropped;
#    RMS at the noise floor with a high flat factor = a dead or clipping mic.
ffmpeg -i "$URL" -t 20 -c copy -y /tmp/s.mp4
ffprobe /tmp/s.mp4                     # expect BOTH streams
ffmpeg -i /tmp/s.mp4 -vn -af astats=metadata=1 -f null - 2>&1 | grep -E 'RMS|Flat'
```

Note that step 2 is the step that catches codec-private-data errors: a stream can ingest
perfectly and still fail `GetHLSStreamingSessionURL` with
`InvalidCodecPrivateDataException`. And as always, finish in a **browser** — MSE is
stricter than `ffmpeg` and has caught two regressions here that `ffmpeg` passed.

To toggle audio: tick "Record audio with video" in the cloud client, or "with audio" in
the local admin table. It applies on the camera's **next Start**, by design (§18.7).

### If outage buffering is enabled (OUTAGE.md)

Off by default. When on, footage is buffered to the USB stick while AWS is unreachable and
backfilled into **Evidence clips** on recovery. Two user units do this — both must be up:

```bash
systemctl --user is-active kvs-outage-buffer kvs-outage-uploader

# armed only while that camera's producer runs; check what MediaMTX was actually told:
curl -s http://127.0.0.1:9997/v3/config/paths/get/cam02 | python3 -m json.tool | grep record

# the rolling window should stay BOUNDED (~4 segments = 120s / 30s). Growing without
# limit means retention is broken and the stick will fill silently.
ls /mnt/vms-buffer/live/cam02/*.mp4 | wc -l

# captures waiting to upload (empty in steady state)
ls -d /mnt/vms-buffer/outage/*/ 2>/dev/null
```

**The stick must be mounted or nothing is armed** — the supervisor checks `ismount` plus
the `/mnt/vms-buffer/.vms-buffer-ok` sentinel every tick, because an unplugged stick with
the mountpoint still present would send MediaMTX's writes to the SD card, and 25 GB free
means a long outage *fits*, which is worse than failing.

To test it, use `adapter/bin/awsblock.sh on|off` — **not** §10.2's `iptables` snippet,
which is IPv4-only and silently ineffective here. Then
`adapter/bin/gap-fill.py --stream cam-02 --last 600`.

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
