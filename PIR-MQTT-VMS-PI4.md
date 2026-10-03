# PIR Motion Sensor → MQTT → VMS: Analysis and Plan (Pi 4B side)

**Status (2026-10-03):** Phases 0, 1 and 3–11 are done, and Phase 12 except its one-week
soak. Phase 2's measurements and Phase 13 are open (§5).

- **Phase 0:** the Mosquitto broker and `paho-mqtt` are installed and verified on the Pi.
- **Phase 1:** the Pico firmware is running (`blink_freertos`).
- **Phase 2:** the logger `adapter/observe_pir.py` is ready; its measurements are postponed
  until the PIR module's hold time is tuned.
- **Phase 3:** the PIR switch is in the registry and the admin app.
- **Phase 4:** the session logic `adapter/pir_session.py` and its replay tool.
- **Phase 5:** the supervisor arms the ring in PIR mode and captures PIR sessions. The live
  outage regression passed.
- **Phase 6:** the watcher `kvs-pir-watcher` turns motion into local clips on the stick.
- **Phase 7:** clips requested on the LAN are uploaded to the shared clip list, labelled
  `pir`, and every upload now carries `videoCodec`.
- **Phase 8:** the admin page's PIR panel and clip list: play, keep, delete, upload; since
  then 10 clips per page, and a "Stop & close player" button.
- **Phase 9:** this project's own `pir-` cloud resources: API, function copies, `pir-control`,
  table `pir-local`, app client `pir-web` (`cloud/pir_stack.py`, `cloud/deploy-pir.sh`).
- **Phase 10:** the Pi reports a live status item and its local clip index (with thumbnails)
  to `pir-local`, and carries out upload requests made in the cloud within 30 s.
- **Phase 11:** this project's own cloud page at `https://d10sy0s307vyid.cloudfront.net`
  (bucket `pir-client-596633517506`, on `pir-api`): everything the old page did, plus the PIR
  switch, the Pi's live status and its clip list with Upload to AWS; since then both clip
  lists page by 10, like the successor's.
- **Phase 12:** the PIR ring and each session's footage live in RAM; the stick receives only
  the finished clips (measured 1.09× the kept footage, down from 3.3×). The outage regression
  passed again (gap-fill 99.72 %). **The one-week soak is postponed, so PIR mode still stays
  off when nobody is around.**
- **Phase 13 (open):** hardening and the remaining documentation. Several of its
  documentation items were done along the way; §5 marks which.

**Written:** 2026-10-02 · **Revised:** 2026-10-03: VMS-side decisions, remote control from
this project's own cloud page, the `pir-` cloud resources, successor compatibility (§4, §6, §7);
then the build itself, phase by phase, with each phase's results in §5 and what changed against
the plan in §7

**Where this file comes from.** The plan was first written in `blink_freertos`, the Pico 2 W
firmware repository, as `PIR-MQTT-VMS.md`. It was copied into this repository because most
of the work lands here, and renamed `PIR-MQTT-VMS-PI4.md` to tell the two copies apart.
**This copy is authoritative for the Pi 4B / VMS side**: the design (§3), successor
compatibility (§4) and every phase except Phase 1. The firmware work (Phase 1) belongs to
`blink_freertos` and is summarised here so the plan reads end to end. Its counterpart there
is **`PIR-MQTT-VMS-Pico.md`** (prepared 2026-10-03, replacing `PIR-MQTT-VMS.md`). It is
authoritative for the firmware and for the MQTT interface the Pico publishes, and carries
everything in this revision that changes what the Pico must do (§3.2, §3.5, §7).

**Repositories involved:**

- **this repository (VMS)**, [vizdr/VMS](https://github.com/vizdr/VMS): the video
  management system on the Raspberry Pi 4B (USB camera `cam-01`, ONVIF camera `cam-02`,
  AWS KVS)
- **`blink_freertos`**: the Pico 2 W firmware with the Makeblock Me PIR Motion Sensor v1.1
- **`VideoSafeZone`** (the successor): shares this AWS account, is developed for **ONVIF
  cameras only**, and is **not modified** by this plan. §4 is the contract that keeps it so.

**Goal:**

- Motion start/stop events from the PIR sensor on the Pico 2 W reach the Raspberry Pi 4B
  with minimal delay over a local MQTT broker.
- There they become **one of the selectable triggers for recording the USB camera**
  (`cam-01`). In PIR mode the camera is recorded **locally, from a ring buffer on the Pi**,
  by the same mechanism as durable outage buffering (`OUTAGE.md`).
- PIR messages that arrive **while a recording is already running are ignored.**
- **Clips the user selects are copied to AWS**, chosen in the local admin app or in this
  project's cloud page.
- PIR mode can be switched, and the PIR state watched, **locally and remotely**:
  - locally in the admin app;
  - remotely in this project's own cloud page, available **only in this project**;
  - the state is also open to any other process on the Pi.
- In both projects' cloud clip lists, an uploaded PIR clip is **labelled `pir`**. The
  successor offers no way to select PIR mode.

---

## Contents

1. [How we got here: three options](#1-how-we-got-here-three-options)
2. [Analysis](#2-analysis)
3. [Design](#3-design)
4. [Successor compatibility](#4-successor-compatibility)
5. [Step-by-step plan](#5-step-by-step-plan)
6. [Decisions](#6-decisions)
7. [Consistency record: what earlier plans said and what changed](#7-consistency-record-what-earlier-plans-said-and-what-changed)
8. [Verified facts and sources](#8-verified-facts-and-sources)

[Appendix A: Sizing POST_SEC](#appendix-a-sizing-post_sec) ·
[Appendix B: Mosquitto on the Pi 4B, setup manual](#appendix-b-mosquitto-on-the-pi-4b-setup-manual)

---

## 1. How we got here: three options

| Option | PIR edge → consumer | Verdict |
|---|---|---|
| **A. Pico → AWS IoT Core** (new `…/motion` topic, DynamoDB, API, web UI on S3) | ~0.5 s to the database, plus 1–5 s of browser polling | Too slow for a live view; cloud-only consumers |
| **B. Pico → Mosquitto on the Pi 4B** (local web or QML GUI, own camera service) | ~5–30 ms, pushed to subscribers (with Wi‑Fi power save off) | Fast. Its own camera service would conflict with VMS, see §2.3 |
| **C. B + VMS integration** (PIR becomes a VMS recording trigger) | ~5–30 ms to the watcher; the clip is cut locally from the ring buffer | **Chosen.** This document |

Even in option C, recording quality doesn't depend on sub-second trigger latency. The ring
buffer already holds the last 2 minutes, so every clip starts **12 s before** the trigger,
from footage already on disk. Low latency matters for the live GUI panel and for any other
local consumer. For recording, it only has to stay well inside the ring's reach, which it
does by orders of magnitude.

The remaining fixed delay is the **PIR module's own detection time** (100 ms to a few s). No
transport removes that.

The cloud page's view of the PIR state is deliberately slower: status is written to AWS on
change and polled by the page every 10 s (§3.10). Local consumers subscribe to the broker
directly and see events within milliseconds.

---

## 2. Analysis

### 2.1 Pico 2 W firmware: current state and constraints

As analysed in `blink_freertos` on 2026-10-02, and re-checked against its GitHub `main` on
2026-10-03. The current table, with three findings from that check (64-bit `boot_ms`, the
warm-up form of `pir/state`, the separate ISR queue), is in `PIR-MQTT-VMS-Pico.md` §2.

| Item | Current state (file) | Consequence |
|---|---|---|
| PIR driver | `pir.c/h`: raw GPIO IRQ on the shared `IO_IRQ_BANK0` (coexists with CYW43), level-based edge handling, S1 mode pin | Done. Nothing to change |
| PIR service | `pir_task.c/h`: queue, 30 s warm-up, start/stop state, `seq` = `motion_count` (start and its stop share it), weak hooks | Hooks need a **timestamp parameter** (today: `pir_on_motion_start(void)`, `pir_on_motion_stop(duration_ms)`) |
| Weak hooks | One strong override per program | **One owner** of the PIR events: the new LAN MQTT task. Any further consumer (e.g. AWS) is fed from the broker, not from a second override |
| MQTT client | lwIP `apps/mqtt` raw API, already used by `aws_iot_task.c` (TLS to AWS) | Reuse it for a **second client** to Mosquitto. No coreMQTT and no sockets (`LWIP_SOCKET 0`) |
| MQTT client limits | `MQTT_OUTPUT_RINGBUF_SIZE` 256 B, `MQTT_REQ_MAX_IN_FLIGHT` 4 (`mqtt_opts.h`) | Bursts can return `ERR_MEM`. Events must be **retried, not dropped** |
| lwIP heap | `MEM_SIZE 4000` (`lwipopts.h`). `mqtt_client_new()` takes ~0.5 KB from it (`mem_calloc`: 256 B ring buffer, 128 B rx buffer, request list) | A second client plus a larger ring buffer needs `MEM_SIZE` ≈ 16000 |
| TCP connections | `MEMP_NUM_TCP_PCB` default 5 | Enough for AWS plus the local broker |
| Timers | Each MQTT client runs a cyclic `sys_timeout`; `MEMP_NUM_SYS_TIMEOUT 16` | Raise by 2 for headroom |
| Wi‑Fi power save | Already off: `cyw43_wifi_pm(&cyw43_state, CYW43_NONE_PM)` (`wifi_task.c:54`) | Nothing to do on the Pico |
| Time | SNTP from `pool.ntp.org` (`time_task.c`); `aws_iot_task` blocks in `time_wait_synced()` | The local path **must not wait for SNTP**, or it stops working without internet. Every message carries `boot_ms`, which is what the Pi uses to age events (§3.5); UTC `ts` is added only when `time_is_synced()` |
| Shared lwIP core lock | An AWS TLS **reconnect** runs the mbedTLS handshake inside lwIP | Can delay local publishes during that handshake (estimate: ~1 s). Rare, only on reconnect. With D6 the direct AWS path stays, and §3.5's ageing places delayed events at their true time, so the stall can't shift a clip |
| JSON payloads | `snprintf` into fixed buffers; the length goes straight to `mqtt_publish()` | Add the `len < 0 || len >= sizeof(buf)` guard to every payload |

### 2.2 Raspberry Pi 4B (Raspberry Pi OS Trixie): packages

Checked against the Debian archive for trixie.

| Need | Package (trixie) | Note |
|---|---|---|
| Broker | `mosquitto` 2.0.21, `mosquitto-clients` | |
| Python MQTT client | `python3-paho-mqtt`, `python3-asyncio-mqtt` | VMS runs its Python code in `venv-adapter`, so the client goes there with pip, not from apt. **Chosen and installed: `paho-mqtt` 2.1.0** (Phase 0, step 5). It uses the 2.x callback API |
| QML GUI (optional) | `python3-pyside6.qtquick` (PySide6 6.8.2), `qml6-module-qtquick-controls`, `qt6-wayland` | **Qt's own MQTT module is not packaged in trixie.** Use paho-mqtt as the backend |

### 2.3 VMS: what the PIR path builds on

| Fact (VMS source) | Consequence for the PIR |
|---|---|
| **cam‑01 (USB) is owned by MediaMTX.** A GStreamer pipeline reads `/dev/video*` and publishes into MediaMTX (`adapter/bin/publish-cam01.sh`, unit `kvs-camera-publish`), independently of the KVS producer `kvs-cam01` | No second process may open the camera, so a `motion` daemon or our own ffmpeg recorder is ruled out. PIR footage comes from **MediaMTX's own recorder**, which sees cam‑01 whether or not the KVS producer runs |
| **VMS has two recording paths.** (i) Evidence clips are cut from KVS afterwards: `event_watcher.py` publishes to `adapter/<thing>/event` → IoT Rule → `clip_to_s3.py`. That needs `kvs-cam01` uploading, which costs money. (ii) Outage buffering: MediaMTX records 30 s fMP4 segments to `/mnt/vms-buffer/live/<path>/`; `outage_buffer.py` keeps a rolling 120 s and sets segments aside when they are needed; `outage_uploader.py` merges and uploads them | PIR uses **path (ii)** (D3). It records locally and costs nothing in AWS until a clip is uploaded |
| **Outage arming is gated on producer-active, deliberately:** an ungated 2-minute window is "close to the worst case for a consumer USB stick's crude FTL" (`OUTAGE.md` §3.5) | PIR mode needs **ungated** recording of cam‑01, which reopens exactly that wear question. See §2.4 and D7 (ring in RAM) |
| **The supervisor is the single owner of MediaMTX recording config.** It reconciles against the live config every 5 s, because MediaMTX doesn't persist API changes (`outage_buffer.py`, docstring point 3) | PIR arming goes **into the supervisor**, as a second reason to record. A second process setting `record` on the same path would fight it, and every flip rebuilds the recorder and leaves a keyframe seam (`OUTAGE.md` §3.3) |
| **No MediaMTX API call happens at a trigger moment** (`OUTAGE.md` §3.3, §4.2) | Same rule for PIR: the ring is armed once, when PIR mode is switched on. A PIR session only touches files |
| **A segment is complete iff a later one exists** (`OUTAGE.md` §4.3). Segment filenames are **local time** (`segment_start()` docstring) | A clip can be assembled about one segment (≤ ~35 s) after its end. Reuse `completed_segments()` and `segment_start()` rather than re-deriving them |
| **`outage_uploader.merge()`** concatenates segments with ffmpeg and **must** transcode audio to AAC: MediaMTX writes LPCM/G.711, which no browser plays inside MP4 | Reuse it for PIR clips, extended with in/out points so it can trim (§3.6) |
| **`outage_uploader.upload_and_register()`** uploads to `clips/<cameraId>/…`, writes a `clips` row with `labels` under `attribute_not_exists`, and keeps the local copy until a confirmed upload. Its grants (`cloud/iam/outage-backfill-policy.json`) are live and drift-free. It does **not** write `videoCodec`, which every other row in the shared table carries | Uploading a PIR clip needs no new IAM. The `-outage.mp4` key suffix needs a parameter, and `videoCodec` must be added, for PIR and outage clips alike (§4.3, rule 3) |
| **Detection modes:** `recordingMode` ∈ {`manual`, `motion`, `cellMotion`, `human`} per camera. `set_camera_mode.py` and the admin app reject every non-manual mode without `onvifHost`. `event_watcher.py` acts only on modes in its `MODE_TOPICS` and only with `onvifHost` | PIR is **not a new `recordingMode` value**. It is a separate flag, `pirRecording` (A2), and `recordingMode` stays `manual` for cam‑01. A value the successor doesn't know would reach its code; a new field doesn't (§4.2) |
| **Shared AWS account** (`FoundAndFixed.md` #48). The successor deploys the same Lambda names and the same client bucket, which has no versioning. The `cameras` and `clips` tables, the evidence bucket, the Cognito pool, the IoT Thing `adapter-01` and the device role are shared too | **No shared resource is modified.** This project gets its own `pir-` resources (§3.9), with its own copies of the Lambdas its page needs (option (b)). The shared data is governed by the contract in §4.3 |
| **Gating:** `ClipGate` (rising edge + 60 s cooldown); `adapter/bin/replay-gate.py` replays recorded event logs through it | "Ignore while recording runs" is a **different gate**: busy for exactly the clip session. Keep the replay-testable pattern |
| **Recording state is not visible on the Pi.** Manual recording lives only in the cloud client (start time held in the browser; `record_clip` is called at Stop) and is a KVS recording | With D2, only PIR sessions count as "recording runs". A manual KVS recording and a PIR session can cover the same moment; that is accepted |
| **Configuration:** `/etc/adapter/adapter.env` read by `adapter/config.py`. It reads its **six identity keys at import time** and raises `ConfigError` if one is missing; `config.get(key)` itself is a lazy lookup | New MQTT keys are read with `config.get()` **at use**, in `pir_watcher.py` and the admin app, and never become module-level constants in `config.py`. Otherwise every VMS process breaks on a Pi without a broker. The admin app must start without them (its PIR panel shows "not configured") |
| **No hardware clock.** The Pi boots with the last saved time until NTP syncs. Observed 2026-10-03: booted 08:02, clock read 22:11 the previous evening until the 08:03:07 sync | The watcher acts on events only once `timedatectl show -p NTPSynchronized` says `yes`. Event times and segment filenames have to be in the same, correct clock |
| **Ports in use:** MediaMTX 8554 (RTSP), 8888 (HLS), 8889 (WebRTC), 1935 (RTMP), 8890 (SRT), 127.0.0.1:9997 (API); onvif-admin 8080 | Mosquitto took 1883 in Phase 0, listening on all interfaces (LAN only, nothing forwarded on the router). 9001 is still free if WebSockets is ever added. 8883 must not be used (B.9) |
| **Conventions:** one process per concern, registry as the single source of truth, user systemd units, heartbeat state files in `$XDG_RUNTIME_DIR/vms/` (`aws_state.py`), `--dry-run`, `FoundAndFixed.md`. Lesson from 2026-10-03: the measurement harness `onvif-observe` was left as an enabled unit and kept appending to its git-tracked log at every boot | The PIR integration follows all of them. The Phase 2 logger runs bounded, never as an enabled unit |

### 2.4 Storage and flash-wear arithmetic

From the bit rates measured in `OUTAGE.md` §3.4 (cam‑01 720p15: 1.00 Mbit/s, 1.26 Mbit/s
with audio) and the 57 GB stick at `/mnt/vms-buffer`:

| Where the ring lives | Stick writes per day | Per year | Full-drive writes / year |
|---|---|---|---|
| On the stick, cam‑01 audio off | 10.8 GB | 3.9 TB | ~69 |
| On the stick, cam‑01 audio on | 13.6 GB | 5.0 TB | ~87 |
| In RAM; the stick gets session footage only (duty cycle 2–13 %) | 0.2–1.4 GB | 0.07–0.5 TB | ~1–9 |

- The 2–13 % duty-cycle range is the ONVIF figure measured on cam‑02 (`COSTS-1.4.md` §7.2:
  2.0 % over a 9.5 h night, 12.8 % on a busy afternoon). The PIR's own figure comes from
  Phase 2.
- A ring in RAM needs 120 s plus one open 30 s segment at 1.26 Mbit/s: **≈ 24 MB**. The Pi
  has 3.7 GB, with 2.1 GB available.
- Local clips at 12.8 % duty cycle take ~1.4 GB/day, so 14 days ≈ 20 GB. That fits above
  the supervisor's disk floor (15 % of the stick = 8.6 GB).
- **Measured in Phase 12** (cam‑01 at 1.07 Mbit/s, audio off): the RAM ring holds 13–15 MB.
  "Session footage only" needs the sessions staged in RAM too: copying whole 30 s segments to
  the stick and writing the trimmed clip again came to **3.3×** the kept footage; staging
  brought it to **1.09×**. With no motion the stick sees almost nothing (92 KB in 4 min).

---

## 3. Design

### 3.1 Architecture

```
LOCAL (Pi 4B)
Pico 2 W ──MQTT 1883──► Mosquitto ──localhost──► kvs-pir-watcher (new user unit)
 pir/event {seq,event,boot_ms,ts}                  │ event age from boot_ms (§3.5)
 pir/state (retained, heartbeat 30 s)              │ ClipSession per camera
 status    (retained, LWT)                         │ ("ignore while recording runs")
 ◄── pir/cmd/retrigger (admin app, user vms)       ▼ session journal on the stick
                                   /mnt/vms-buffer/pir/<sessionId>/state.json
 MediaMTX records cam01 continuously while         │
 pirRecording is on (armed by kvs-outage-buffer)   │ supervisor links the session's
   └─► ring: live/cam01/ (30 s fMP4, 120 s kept) ──┤ segments out of the ring
                                                   ▼ watcher: merge + trim + thumbnail
                                   pir/<sessionId>/clip.mp4 ─► admin app: play, keep, delete,
                                                   │                     upload
                                                   ▼
CLOUD, VMS only (pir- resources, §3.9)
 kvs-pir-watcher ──HTTPS, device cert──► pir-local: status item + clip index (§3.10, §3.11)
 kvs-outage-uploader ◄── polls requests ── pir-local
        └── upload ──► S3 clips/cam-01/…-pir.mp4 + clips row (shared data, §4.3)

 browser ─► CloudFront ─► pir-client page ─► pir-api ─┬─► pir-control ─► cameras.pirRecording
                                                      │                  pir-local
                                                      └─► pir-<copies> ─► shared data:
                                                          cameras, clips, KVS, evidence bucket

Other local consumers (QML kiosk, scripts) subscribe to the same broker.
aws_iot_task on the Pico (telemetry over TLS to AWS IoT Core) stays as it is (D6).
```

One owner per concern:

| Component | Owns | New or changed |
|---|---|---|
| Pico `lan_mqtt_task` | PIR events on the broker | new (`blink_freertos`) |
| Mosquitto | transport, retained state, persistent sessions | new system service |
| `kvs-outage-buffer` (`outage_buffer.py`) | MediaMTX record config; ring retention; copying ring segments into captures, outage and PIR | extended: arms when `pirRecording` is on; links session segments |
| `kvs-pir-watcher` (`pir_watcher.py`) | MQTT subscription, event ageing, `ClipSession`, session journals, merge + trim + thumbnail, local PIR clip retention, the status item and clip metadata in `pir-local` | new user unit |
| `kvs-outage-uploader` (`outage_uploader.py`) | uploads to S3 + `clips` row; upload results, locally and in `pir-local` | extended: uploads PIR clips requested from either GUI |
| onvif-admin (`app.py`) | local PIR panel, local clip list, the PIR on/off switch, local upload requests, retrigger command | extended |
| `pir-control` (Lambda) | the cloud's PIR on/off switch, status read, clip index read, cloud upload requests | new, VMS only |
| `pir-client` page + `pir-api` + `pir-<copies>` | this project's whole cloud GUI | new, VMS only (§3.9) |

### 3.2 Topic contract

| Topic | QoS / retained | Publisher | Payload |
|---|---|---|---|
| `home/pico2w-01/status` | 1 / **retained**, LWT = `offline` | Pico | `online` / `offline` |
| `home/pico2w-01/pir/event` | 1 / no | Pico | `{"seq":42,"event":"start","boot_ms":123456,"ts":1759312345123}`; `stop` adds `"duration_ms"`. `boot_ms` is the edge time from the ISR, on the Pico's uptime clock. `ts` only when SNTP has synced |
| `home/pico2w-01/pir/state` | 1 / **retained** | Pico | `{"motion":true,"changed_ms":…,"count":42,"dropped":0,"boot_ms":…}`. During the sensor's 30 s warm-up: `{"warming_up":true,"boot_ms":…}`. `boot_ms` is the Pico's uptime **at publish**; `dropped` (optional) counts events lost to a full queue on the Pico. Published on connect (before the event queue drains), after each event, and **every 30 s** as a heartbeat |
| `home/pico2w-01/pir/cmd/retrigger` | 1 / no | admin app (user `vms`) → Pico | `1` / `0` → `pir_task_set_retrigger()` |

Rules:

- **Only `pir/event` triggers recording.** Retained `pir/state` is replayed to every new
  subscriber, so it must never open a session. Otherwise a watcher restart while someone
  stands in the hall would create a phantom clip.
- **`seq` pairs a start with its stop** and exposes gaps. QoS 1 may deliver a message twice,
  so consumers deduplicate by `(seq, event)`.
- **Every message carries `boot_ms`.** It is the Pico's own clock and is how the watcher knows
  how old an event is (§3.5), with or without SNTP. It is **64-bit milliseconds since boot
  and never wraps**: the Pico widens its 32-bit ISR timestamp, which would otherwise wrap
  after 49.7 days and look like a reboot (`PIR-MQTT-VMS-Pico.md` §3.3).
- The retrigger command stays LAN-only. The cloud page switches PIR recording on and off; it
  doesn't send commands to the Pico.

### 3.3 The clip session: "ignore while recording runs"

One `ClipSession` per camera whose `pirRecording` is on:

```
IDLE ──start(seq=N), fresh (§3.5), ring recording──► RECORDING(N, t0)
RECORDING: start/stop with seq≠N, or a repeated start(N) ─► IGNORED (logged + counted)
           stop(N) at t1                                 ─► POST_ROLL until t1 + POST_SEC
           t0 + MAX_SEC reached, or status=offline       ─► close at that moment
POST_ROLL end ─► IDLE
   in the background: wait until the ring segments up to the clip end are captured
   (≤ ~35 s) ─► merge + trim [t0 − 12 s, end] ─► thumbnail ─► local clip + index row
```

- **"Recording runs" means only an open PIR session (D2):** from the triggering `start`
  until the clip's end (`stop` + POST_SEC, or the cap). Everything that arrives in between is
  ignored.
  - In single-trigger PIR mode, that means the repeated start/stop pairs.
  - In retriggerable mode (the firmware default), it is mostly QoS 1 duplicates.
  - The background assembly after the clip's end doesn't count as recording.
  - A manual cloud recording doesn't count either.
- **POST_SEC** is the extra footage kept after `stop`. Starting value ≈ 4–5 s; how to size it
  is in [Appendix A](#appendix-a-sizing-post_sec).
- **A ring check replaces the old producer check.** If the supervisor hasn't armed cam‑01, or
  the newest ring segment is older than two segment durations, log *"skipped: ring not
  recording"*, report it in the status (§3.10) and stay IDLE. The KVS producer's state no
  longer matters.
- **Events keep flowing during POST_ROLL and assembly.** Both run as background tasks, as the
  publish wait does in `event_watcher.py`, so the subscription keeps draining.
- **Session state is journalled on the stick** (`pir/<sessionId>/state.json`, atomic write).
  Together with the broker's persistent session (§3.8), a watcher restart in the middle of a
  session resumes it: the journal restores RECORDING or POST_ROLL, and a `stop` published
  meanwhile is delivered from the broker's queue.

### 3.4 Clip window: follows the motion

**Window:** t0 − 12 s … t1 + POST_SEC, capped at t0 + MAX_SEC.

The earlier fixed 45 s variant existed only to avoid changing `clip_to_s3.py`. Local clips
involve neither KVS nor that Lambda, so the window simply follows the motion (D1).

**Choosing POST_SEC.** VMS needs 33 s of post-roll on ONVIF clips because ONVIF detectors give
no end-of-activity signal. The PIR does: in retriggerable mode its `stop` already comes one
hold time after the last movement. POST_SEC therefore only covers keyframe rounding and
anything the camera sees beyond the PIR's field. Start at about 4–5 s; the full reasoning,
the formula and the measurement procedure are in [Appendix A](#appendix-a-sizing-post_sec).

**Choosing MAX_SEC = 180 s.** GetClip's limits no longer apply. The cap is now a product
choice:

- it keeps one clip at ≈ 23 MB (28 MB with audio), quick to review and to upload;
- it bounds what a stuck `start` (a lost `stop`) can record.

Presence longer than the cap is decision D4.

### 3.5 Event age and timestamps

Every event is placed on the Pi's clock using the Pico's `boot_ms`, and dropped if it is too
old for the ring to still hold its footage.

- **Clock reference.** For each Pico the watcher keeps
  `offset = min(pi_receive_time − boot_ms)` over the messages of the last 10 minutes. The
  heartbeat keeps samples coming even when nothing moves.
  - Delays only ever *add* to `receive − boot_ms`, so the minimum comes from the
    least-delayed message. It is accurate to the LAN latency (~5–30 ms).
  - The 10-minute window follows crystal drift (≤ 30 ppm ≈ 18 ms per 10 min).
- **Event time** = `offset + boot_ms`, on the Pi's wall clock, the same clock as the segment
  filenames. **Age** = receive time − event time.
- **Drop if age > STALE_SEC = 60 s.** A stale `start` opens nothing; it is logged and counted
  as `stale`. A stale `stop` still closes its open session, at its event time.
- **Otherwise the clip is cut at the event time, not the receive time.** An event delivered
  late, from the Pico's queue after a reconnect or from the broker after a watcher restart,
  still lands where it happened. SNTP on the Pico no longer matters for clips.
- **Reference validity.**
  - It is reset when **`pir/state`'s** `boot_ms` goes backwards. The Pico publishes
    `pir/state` in order and stamps it at publish time, and `boot_ms` is 64-bit and never
    wraps, so that only happens when the Pico reboots.
  - **Never detect a reboot from events.** An event's `boot_ms` is its edge time, so an event
    that waited in the Pico's queue legitimately carries an older value than the latest
    `pir/state`. Treating that as a reboot misreads every late event: it reset the reference
    and split the log into phantom boots when this rule was first tested
    (`adapter/observe_pir.py`, 2026-10-03).
  - An event belongs to the boot whose first `pir/state` arrived before it. After a
    reconnect the Pico publishes `pir/state` before draining its queue, and a reboot empties
    that queue, so nothing from an earlier boot can arrive after a later boot's first state.
  - A `pir/state` in its warm-up form is a valid clock reference too: the sensor isn't
    ready, but the Pico's clock is.
  - It is persisted with the Pico's last `boot_ms`, so a watcher or Pi restart keeps it as
    long as the Pico hasn't rebooted.
  - If no valid reference exists, events are dropped as *un-ageable* until a message
    received ≥ 5 s after the (re)connect establishes one. A message that late can't be
    broker backlog.
  - On connect the Pico publishes `pir/state` before draining its own queue, so after a
    Pico reboot the first message is a fresh reference.

> **Invariant: STALE_SEC + 12 s (pre-roll) + 30 s (one segment) + 5 s (supervisor tick) <
> ring retention (120 s).** The ring only guarantees `retention − segment` of history, and
> the clip needs 12 s before the event. 60 + 12 + 30 + 5 = 107 s. If the ring retention is
> ever shortened, STALE_SEC must shrink with it.

### 3.6 Local recording: ring, capture, clip

**Ring.** For each camera with `pirRecording` on, the supervisor arms MediaMTX recording
**regardless of producer state**:

```
want = {outageBufferSec > 0 and producer active}  ∪  {pirRecording}
       (both also require buffer_ready() and disk_ok())
```

One recorder per path serves both features: 30 s segments, the same 120 s retention.
**Where the ring lives** (Phase 12, D7):

- **armed for PIR** (`pir` or `outage+pir`): **in RAM**, `$XDG_RUNTIME_DIR/vms/ring/<path>/`
  (~13–24 MB per camera), with `recordDeleteAfter: 10m` as a backstop: MediaMTX's cleaner
  then bounds the ring near 95 MB if the supervisor dies, instead of filling the 374 MB
  runtime tmpfs that every heartbeat file and systemd's runtime state share. It can't race a
  capture: RAM segments leave the ring by copy within one 5 s tick, and the supervisor prunes
  at 120 s;
- **armed only for the outage:** on the stick, `live/<path>/`, `recordDeleteAfter: 0s`,
  unchanged;
- with under 64 MB free in the runtime tmpfs, the PIR ring falls back to the stick.

Everything that reads the ring looks in both places (`ring_segments()`), because a camera's
ring moves when it is re-armed for another reason, and footage left in the old place may still
belong to a session or an outage. Moving a segment out of the RAM ring (an outage capture) is a
durable copy: written as `.name.part`, fsynced, renamed, then the RAM file is deleted. So the
outage path still makes no MediaMTX call at T0.

**Capturing a session.**

- The watcher's journal states the window `[from, to]`; `to` stays open while RECORDING.
  **The journal format the supervisor reads** (built in Phase 5; the watcher must write it):

  ```json
  {"sessionId": "cam-01-20261003T134324Z", "cameraId": "cam-01",
   "from": 1791034959.23, "to": null, "status": "recording"}
  ```

  - `from` and `to` are epoch seconds on the Pi's clock: `from` = t0 − pre-roll, `to` = the
    clip's end, `null` while the session is open;
  - the supervisor needs only `cameraId`, `from` and `to`; any other field (`status`, …)
    is the watcher's own;
  - write it atomically (temporary file, then rename).
- Each tick, the supervisor hard-links every completed ring segment that overlaps the window
  into the session. Always a link, never a copy, because the target is on the source's
  filesystem:
  - a segment in the **RAM ring** is **staged in RAM**, `$XDG_RUNTIME_DIR/vms/pir/<sessionId>/<path>/`
    (Phase 12). Copying it to the stick instead measured **3.3×** the kept footage in stick
    writes: whole 30 s segments for ~18 s clips, and then the trimmed clip written again;
  - a segment already **on the stick** (an outage capture, or a ring on the stick) is linked
    into `pir/<sessionId>/<path>/`;
  - it looks in the ring (both places) **and in outage captures** (`outage/*/<path>/`,
    `outage/*/tail/<path>/`), captures first, so footage that is already on the stick is
    linked from there. During an outage, completed segments leave the ring within one tick,
    so a session's pre-roll may already be in a capture.
  - It runs **before** the outage sweep in each tick, so a segment is linked before an
    outage capture moves it.
  - A journal still open longer than pre-roll + MAX_SEC + 60 s (the watcher died
    mid-session) is captured up to its cap and then left alone.
- The supervisor records its progress in `pir/<sessionId>/sweep.json`, rewritten only when it
  changes:

  ```json
  {"sessionId": "...", "through": 1791035053.98, "coverageStart": 1791034933.97,
   "segments": ["2026-10-03_15-42-13-974721.mp4", "..."], "stagedInRam": ["..."],
   "complete": true, "cappedBySupervisor": false, "updated": "..."}
  ```

  `through` is the end of the latest captured segment. `complete` is true once `to` is set
  and `through` ≥ `to`, which is when the watcher may merge. `coverageStart` later than
  `from` would mean the ring didn't reach back that far (e.g. it had only just been armed).
- **Link, don't move.** A segment can belong to two adjacent sessions, because the next
  session's pre-roll may lie in the previous session's last segment. It can also belong to
  an outage capture, whose move out of the ring leaves the link intact. The ring's own
  retention is unaffected.
- **Staging stays bounded** (`tend_pir_staging()`, every tick): the staging of a merged,
  failed or deleted session is removed (the watcher removes it after a merge; this catches a
  crash in between). A session still unmerged 10 min after capture (the watcher is down or
  stuck) has its footage moved to the stick session folder, where the merge also looks.
- **The trade-off, accepted 2026-10-03:** a session not yet merged lives only in RAM, so a
  reboot or power cut in that window (at most a few minutes per session) loses it. The watcher
  then marks it failed with "its footage was staged in RAM and is gone".

**Assembling.**

- When `sweep.through` ≥ the clip end, the watcher merges the session's segments with
  `outage_uploader.merge()`, extended with concat-demuxer `inpoint`/`outpoint`.
- The trim is a stream copy, so it cuts on keyframes: within 2 s at GOP 30 / 15 fps.
- Output: `pir/<sessionId>/clip.mp4`. The merge reads the staged segments from RAM and any
  linked on the stick; both are deleted after `ffprobe` accepts the clip. The clip and its
  thumbnail are all a session writes to the stick.
- `startTs`, `durationSec` and `videoCodec` are measured from the file, as for outage clips.
- **Thumbnails (D13):** one 160×90 JPEG at t0 + 2 s by default, up to three (start, middle,
  end) as a tunable; ~5–8 KB each. Saved as `thumb-<n>.jpg` and copied into the clip's index
  row (§3.11), so the cloud page can show what a clip contains without fetching it.

**One writer per file.** No file is edited by two processes, for the same reason `aws_state.py`
writes its own heartbeat file:

- the watcher writes `state.json`, `clip.mp4` and the thumbnails;
- the supervisor writes `sweep.json`;
- the admin app writes the `keep` and `upload-requested` markers;
- the uploader writes `uploaded.json`, and `cloud-request.json` when it picks up a request made
  in the cloud (§3.11).

One exception, and only once a clip's AWS copy is gone: the uploader then also removes the
request marker `upload-requested` (§3.11, "When the AWS copy is deleted").

**Storage layout:**

```
$XDG_RUNTIME_DIR/vms/ring/<path>/                ring of a PIR camera, RAM  supervisor (MediaMTX writes)
$XDG_RUNTIME_DIR/vms/pir/<sessionId>/<path>/     staged session footage     supervisor (links)
/mnt/vms-buffer/live/<path>/                     ring of an outage-only cam supervisor (MediaMTX writes)
/mnt/vms-buffer/outage/<id>/…                    outage captures, unchanged supervisor
/mnt/vms-buffer/pir/<sessionId>/state.json       session journal            watcher
/mnt/vms-buffer/pir/<sessionId>/sweep.json       capture progress           supervisor
/mnt/vms-buffer/pir/<sessionId>/<path>/          footage already on the stick, until merged
/mnt/vms-buffer/pir/<sessionId>/clip.mp4         the local clip             watcher
/mnt/vms-buffer/pir/<sessionId>/thumb-<n>.jpg    thumbnails                 watcher
/mnt/vms-buffer/pir/<sessionId>/keep             pinned by the user         admin app
/mnt/vms-buffer/pir/<sessionId>/upload-requested request from the LAN       admin app
/mnt/vms-buffer/pir/<sessionId>/uploaded.json    S3 key and time            uploader
/mnt/vms-buffer/pir/<sessionId>/cloud-request.json a cloud request, seen     uploader
```

`sessionId` = `<cameraId>-<UTC start, yyyymmddThhmmssZ>`.

**Ring location (D7, decided: RAM before PIR mode is left on unattended).** On the stick, the
ring costs 69–87 full-drive writes a year (§2.4), the load `OUTAGE.md` §3.5 gated away. In
RAM, under `$XDG_RUNTIME_DIR/vms/ring/` (tmpfs; lingering is on, so it exists from boot), it
costs ≈ 24 MB, and the stick only receives session footage. Moving it to RAM has three
consequences:

- the supervisor copies rather than links or renames across filesystems (≈ 4 MB per segment);
- `recordPath` becomes per-camera, changed by one record PATCH (one recorder rebuild) when
  PIR mode changes, never at a trigger;
- for a camera in PIR mode, an outage capture's power-loss exposure grows from 1 s (the part
  duration) to the open segment (≤ 30 s).

Bring-up ran with the ring on the stick (Phase 5); since Phase 12 a PIR camera's ring is in RAM.

**Local retention (D8, decided):**

- clips older than 14 days are deleted unless they carry `keep`;
- **a clip with a pending upload request, from either GUI, is exempt** from the age rule;
- as free space nears the supervisor's floor (15 % = 8.6 GB on this stick), the oldest
  unkept clips go first, and pending ones last;
- when a clip is deleted, the watcher sets `deletedAt` and a TTL on its index row (§3.11), so
  the cloud page stops offering it.

The floor matters twice: below it the supervisor **disarms recording**, which would silently
stop PIR recording. Retention has to act before that.

**Coexistence with outage buffering.** Both features on cam‑01 at once is supported: they
share the ring, and an outage capture and a PIR session can hold links to the same segment.
An upload requested during an outage simply fails and is retried, as the uploader already
does.

### 3.7 Uploading selected clips

- **Two request sources, one executor (D9, decided: both GUIs):**
  - **LAN:** the admin app's "Upload to AWS" writes `upload-requested`;
  - **cloud:** the page's "Upload to AWS" makes `pir-control` set `uploadRequestedAt` on the
    clip's index row (§3.11);
  - `kvs-outage-uploader` executes both. It is the only writer of results.
- **Upload:** `upload_and_register(suffix="pir")`:
  - key `clips/cam-01/YYYY/MM/DD/HHMMSS-pir-s<seq>.mp4`. The motion's `seq` is there because
    two sessions can start in the same second, and a second-resolution key would let one
    upload overwrite another in S3;
  - `clips` row `cameraId`, `startTs`, `s3Key`, `labels: ["pir", "pir:seq <N>"]`,
    `durationSec`, **`videoCodec`** (from `ffprobe` of the clip, `h264` for cam‑01). The
    same function also adds `videoCodec` for outage uploads (§4.3, rule 3);
  - the conditional put prevents double registration.
- **Results:** `uploaded.json` locally, and `uploadedKey`, `uploadedAt` or `uploadError` on
  the index row.
- **Copy, not move (D10).** The local clip stays until local retention removes it. Deleting in
  a cloud clip list doesn't delete the local copy, and vice versa. Once the AWS copy is
  deleted, the clip shows as "on the Pi only" again within 5 minutes and can be uploaded again
  (§3.11).
- **Cost:** S3 storage of uploaded clips only. PIR mode causes no KVS ingest at all. The
  cloud live view still needs Start (the producer), exactly as today.
- **Visibility:** an uploaded PIR clip appears with the label `pir` in this project's page
  and in the successor's page. The successor's page lists every `clips` row of a camera in
  the shared registry.

### 3.8 Registry and configuration

**Registry** (`cameras` table, shared; the contract in §4.3 applies):

- capability attribute `pirTopic` (cam‑01: `home/pico2w-01/pir/event`);
- **switch `pirRecording`** (boolean, absent = off). `recordingMode` is untouched and stays
  `manual` for cam‑01 (A2);
- gating: `pirRecording` can be switched on only with `pirTopic` set;
- **two writers, the same way audio and outage settings work:** the admin app (LAN) and
  `pir-control` (cloud). Both use field-level `UpdateItem` on `pirRecording` only. The Pi
  picks the change up within 60 s.

**`/etc/adapter/adapter.env`:** `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD_FILE`.
Read with `config.get()` at use, in `pir_watcher.py` and the admin app; never added as
module-level constants in `config.py` (§2.3).

**Watcher MQTT session:** a fixed client id, a persistent session (`clean_session=False`)
and QoS 1 subscriptions. QoS 1 events published while the watcher was down are delivered
afterwards, and §3.5 ages them.

**Registry cache:** the watcher reuses the supervisor's cached registry
(`~/.local/state/vms/cameras-cache.json`). Local recording has to keep working with AWS
unreachable, and only the cache is available then.

### 3.9 This project's own cloud resources (the `pir-` namespace)

Decisions B3 (own API), C2 (own page on its own bucket and distribution) and option (b) (own
copies of the Lambdas the page needs) make this project's cloud GUI independent of
everything the successor deploys.

**Namespace rule.** Every AWS resource that only this repository deploys is named `pir-…`,
**whether or not it is PIR-specific**. The `vms-` prefix doesn't qualify, because the
shared buckets already carry it (`vms-demo-client-…`, `vms-demo-evidence-…`). The successor
is ONVIF-only and will never create a `pir-` resource; none existed on 2026-10-03.

| Resource | Purpose | Decision |
|---|---|---|
| Bucket `pir-client-596633517506` + CloudFront distribution + OAC | This project's cloud page: a fork of `client/index.html` plus the PIR panel | C2 |
| Cognito app client `pir-web` on the shared pool `kvs-demo-users` | The page's login. Same users, own client settings. Username/password flow, no secret, no redirect URLs | D12 |
| REST API `pir-api` (stage `prod`), Cognito authorizer on the shared pool | The only API the page calls | B3 |
| Lambda copies: `pir-list-cameras`, `pir-get-hls-url`, `pir-publish-cmd`, `pir-record-clip`, `pir-list-clips`, `pir-play-clip`, `pir-set-clip-tier`, `pir-delete-clip`, `pir-set-camera-mode`, `pir-set-camera-audio`, `pir-set-camera-outage-buffer` | Deployed from this repository's `cloud/lambda/*.py`, behind the same routes as today (`/cameras`, `/cameras/mode`, `/cameras/audio`, `/cameras/outage-buffer`, `/cmd`, `/hls`, `/clips`, `/clips/record`, `/clips/play`, `/clips/tier`, `/clips/delete`) | option (b) |
| Lambda `pir-control` | `GET /pir` (switch + status), `GET /pir/clips` (local clip index with thumbnails, paged like `list-clips`: `page`, `pages`, `total`, `deleted`), `POST /pir/mode`, `POST /pir/upload` | A2, extras 1 and 2 |
| DynamoDB table `pir-local` (on-demand, TTL on `ttl`) | PK `cameraId`, SK `status` or `clip#<startTs>`: live status and the local clip index | D11 |
| Inline policy `PirLocal` on the device role `KVSAdapterRole` | The Pi reads and writes `pir-local` (`GetItem`, `Query`, `PutItem`, `UpdateItem`) | extras 1 and 2 |
| Roles `pir-<function>-role`, policies `cloud/iam/pir-*.json` | One role per function, mirroring the existing per-function policies. `pir-control` may update only `pirRecording` on `cameras` (`dynamodb:Attributes` condition) and only `uploadRequestedAt`/`requestedBy` on `pir-local` | |

- **Copies are this repository's code.** Since 2026-10-03 `list_clips.py` carries the
  successor's paging, ported line for line (`limit`, `page` as a number or `last`; `page`,
  `pages`, `total` in the reply), so `pir-list-clips` and the shared `list-clips` give
  identical answers to the same request, paged or not.
- **Configuration of the copies** (runtime, memory, timeout, environment) is taken from the
  live originals with `aws lambda get-function-configuration` when they are created.
- **The deploy path is guarded:** a script `cloud/deploy-pir.sh` zips and updates functions,
  and **refuses any name without the `pir-` prefix**. That makes #48 structurally impossible
  from this side.
- **Logging:** `pir-control` logs `cognito:username` with every write. Extra 3 (restricting
  PIR control to a Cognito group) is postponed; the log prepares it.
- **Cost:** no idle cost. Lambda and API Gateway are billed per request, the CloudFront free
  tier covers this traffic, and `pir-local` is on-demand.

**What stays shared** (§4.1): the data (`cameras`, `clips`, the evidence bucket, KVS
streams), the Cognito user pool, the IoT Thing `adapter-01` with its device role and role
alias, and the ONVIF event path (`ClipToS3Rule` → `clip-to-s3`) used by cam‑02's detection
clips.

### 3.10 Live PIR status (extra 1)

- **The watcher writes the status item** (`cameraId`, SK `status`) in `pir-local`:
  - Pico online, motion, session state (idle / recording / post-roll);
  - `pirRecording` as configured and as effective, and whether the ring is recording;
  - last event, last clip, today's sessions, ignored and stale counts;
  - clock-reference state, and `updatedAt`.
- **It reports the actual state, not just the setting**, e.g. "enabled, but ring not
  recording: USB stick missing", "pirTopic missing", "Pico offline".
- **When:** on change, coalesced to at most one write per 5 s, plus a heartbeat every 5 min.
- **How:** off the event path, on its own thread, with short timeouts (connect 3 s, read 5 s,
  one attempt). The supervisor once blocked for 45 minutes in a call with boto3's default
  timeouts (`_fetch_registry()` docstring). Offline, writes fail; the thread keeps only the
  latest state, and the first good pass after recovery writes it.
- **As built (Phase 10, `adapter/pir_cloud.py`):** the fields are `pirRecording`,
  `effective` (`off`, `armed`, or `enabled, but …`: broker unreachable, Pico offline, USB
  stick not usable, ring not recording), `ringRecording`, `picoOnline`, `motion`,
  `brokerConnected`, `session`, `sessionId`, `lastEvent`, `lastClipAt`, `sessionsToday`
  (session folders started today), `counts`, `clockValid`, `picoReboots`, `ntpSynced`,
  `clockJumps`, `pirTopic`, `updatedAt`. Only values that change when something happens: a
  field that moved every second would defeat the change detection. `lastEvent`, `lastClipAt`
  and `counts` are kept in memory and start empty when the watcher restarts; `sessionsToday`
  is counted from the stick. The local heartbeat file
  gains a `cloud` block (`ok`, `lastOkAt`, `lastError`), so the admin side can see whether the
  cloud is being kept up to date.
- **The page** polls `GET /pir` every 10 s while visible (`document.visibilityState`). Once
  `updatedAt` is older than 10 min, it shows "unknown (last update hh:mm)": a stale value means
  unknown, as with `aws_state.py`. That is roughly $0.30 a month at 8 h of open page a day.
- **The admin app** reads the local heartbeat file `pir-state.json` instead (same fields,
  faster, works without AWS).

### 3.11 The local clip index and upload requests from both GUIs (extra 2, D9, D13)

- **Index rows** (`cameraId`, SK `clip#<startTs>`) are written by the watcher after a clip is
  assembled: `sessionId`, `startTs`, `durationSec`, `sizeBytes`, `labels`, `videoCodec`,
  thumbnails (base64), `kept`, and later `deletedAt` and `ttl`.
- **Each field has one owner,** and rows are only ever updated field by field, never
  overwritten whole:

  | Owner | Fields |
  |---|---|
  | watcher | clip details, thumbnails, `kept` (mirrors the admin app's `keep` marker), `deletedAt`, `ttl` |
  | `pir-control` | `uploadRequestedAt`, `requestedBy` |
  | uploader | `uploadedKey`, `uploadedAt`, `uploadError` |

- **A cloud request** is `POST /pir/upload`. It sets `uploadRequestedAt`, on condition that
  the row exists and has no `deletedAt`.
- **The uploader polls** every 30 s (its existing scan interval) for rows with
  `uploadRequestedAt` and no `uploadedKey` or `uploadError`, alongside the local markers. It
  reads them from the sparse index **`upload-requests`** (PK `cameraId`, SK
  `uploadRequestedAt`; only rows with a request are in it, projecting `sessionId`,
  `requestedBy`, `uploadedKey`, `uploadError`), never from the table: a query of the table
  reads every clip row, thumbnails included, which at a few thousand rows is ~30 MB per poll,
  dollars a day. Polling rather than MQTT, for three reasons:
  - no replay logic after the Pi has been offline: the request is simply still there;
  - no change to `agent.py`;
  - no dependency on the single `adapter-01` MQTT connection, which only one of the two
    projects' Pis can hold at a time.
- **Latency:** request → upload start ≤ 30 s; a 3-minute clip of ~23 MB then takes roughly
  10–40 s on a typical uplink.
- **Both GUIs show the same state:** local only, requested, in AWS (with the clip in the
  clip list), failed (with the error), deleted. The page reads it through `GET /pir/clips`.
  The admin app reads only local files: when the uploader picks up a cloud request it records
  it in `cloud-request.json` (requester, times), and `uploaded.json` says `via: cloud` and who
  asked. So the LAN page needs no AWS call and works offline.
- **Every upload's result reaches the row,** whichever GUI asked: the uploader mirrors
  `uploaded.json` onto the row and then marks it `indexRowUpdated`. A clip uploaded from the
  admin app shows as "in AWS" on the cloud page too.
- **When the AWS copy is deleted** (from either project's page, or by hand), the uploader
  notices within 5 minutes (decided 2026-10-03). It lists the day folders that hold its uploads
  (`clips/<camera>/<date>/`), and for a clip whose key is missing it resets the row (`REMOVE`
  the result and the request fields, on condition that `uploadedKey` still names that key) and
  removes `upload-requested`, `cloud-request.json` and `uploaded.json`, in that order. Both GUIs
  then show "on the Pi only". The request markers go too, because either left behind would
  make the next pass upload the clip again: a deleted copy ends the request cycle. It is the
  only time the uploader touches another writer's field.
  - It lists rather than HEADs: without `ListBucket` on the bucket, S3 answers a HEAD on a
    missing key with 403, not 404. `PirLocal` grants `s3:ListBucket` for the prefix `clips/*`
    only.
  - Cost: one list request per day folder per 5 minutes, a few cents a month.
- **A picked-up cloud request is pending** like a LAN one: retention skips the clip, and the
  admin app won't delete it until the upload is done.
- **A clip that has gone** by the time a request runs gets `uploadError: gone`; a clip whose
  assembly failed gets `uploadError: clip failed`.
- **Size:** thumbnails make a row ~10–30 KB, far under DynamoDB's 400 KB limit. 14 days of
  busy-afternoon clips are a few thousand rows: cents a month in storage and reads.
- **Row lifetime:** DynamoDB's TTL removes a row one day after its `deletedAt`.

---

## 4. Successor compatibility

The successor (`VideoSafeZone`) is developed for ONVIF cameras only. This plan must not break
it, and it shouldn't be able to break this project's USB camera either. Checked against live
AWS on 2026-10-03.

### 4.1 What is shared, and who owns it after this plan

| Resource | Owner after this plan | Used by this project |
|---|---|---|
| Unprefixed Lambdas, API `kvs-demo-api`, bucket `vms-demo-client-…`, distribution `E1B12167KKII6B` | **the successor** | Not by the page. Only `clip-to-s3`, through `ClipToS3Rule`, for cam‑02's ONVIF detection clips |
| `pir-` resources (§3.9) | **this repository** | The whole cloud GUI, PIR status and requests |
| `cameras` and `clips` tables, evidence bucket, KVS streams | shared data | Both, under the data contract (§4.3) |
| Cognito pool `kvs-demo-users` | shared | Each project has its own app client |
| IoT Thing `adapter-01`, device role `KVSAdapterRole`, role alias | shared | Both projects' Pis |

The live state on 2026-10-03:

- 11 of the 12 shared Lambdas are byte-identical to this repository's code;
- `list-clips` differs: the successor added paging and still serves callers that don't page;
- the live root page is the successor's (50,599 bytes, against this repository's 45,664).

### 4.2 Every change in this plan, checked against the successor

| Change | Shared resource touched | Effect on the successor | Verdict |
|---|---|---|---|
| `pirTopic`, `pirRecording` on cam‑01's row (A2) | `cameras` table | `list-cameras` returns a fixed set of fields, so they are invisible. The event watcher ignores cam‑01, because `recordingMode` stays `manual`. The outage supervisor reads only `outageBufferSec` | **Safe.** A2 is what makes it safe; a `recordingMode` value `pir` would have reached the successor's code |
| Uploaded PIR clips | `clips` table, evidence bucket | Same row format as the outage clip already in the table and listed by the successor's page. `play-clip` accepts any `clips/` key; the page shows a missing codec as nothing, and PIR rows carry `videoCodec` anyway | **Safe.** The label `pir` shows in the successor's list, as intended |
| `videoCodec` added to outage and PIR rows | `clips` table | Brings this repository's rows in line with every other row | **Safe** |
| Ring recording, watcher, supervisor, uploader, admin app | the VMS Pi only | None; the successor's Pi doesn't run this code | **Safe** |
| `pir-` bucket, distribution, app client, API, Lambdas, roles, table | new resources only | None. The names can't collide with an ONVIF-only project, and the deploy script refuses unprefixed names | **Safe** |
| `PirLocal` inline policy | device role `KVSAdapterRole` | The successor's Pi gains access to `pir-local`, which it never uses | **Safe**, slightly wider privileges (§4.4) |
| PIR status and upload requests | HTTPS with the device certificate | Don't touch the shared `adapter-01` MQTT connection | **Safe** |
| Unprefixed Lambdas, `kvs-demo-api`, root page, IoT policy | none | Not modified; this repository stops deploying them | **Safe** |

### 4.3 The data contract

Both projects write the same tables, so both have to keep these rules. Rules 1–3 and 5a are
under this repository's control; rules 4 and 5b need to be recorded in the successor's
CLAUDE.md as well.

1. **Existing rows are updated field by field, never overwritten whole.** This repository
   complies: its only full-row write creates a new ONVIF camera
   (`adapter/onvif-admin/app.py:427`).
2. **Add, never redefine.** New fields and new rows are fine. Changing the meaning of a field,
   adding values to an existing enum, or removing a field is not. In particular,
   `recordingMode` keeps only values both projects understand, which is why PIR is
   `pirRecording`.
3. **Every clip row carries** `cameraId`, `startTs`, `s3Key` (under `clips/<cameraId>/`),
   `labels`, `durationSec` and `videoCodec`, whoever writes it.
4. **If the successor wants cam‑01 gone from its page, it filters in its page,** not in a
   shared Lambda or in the data.
5. **Neither project deploys the other's resources:** (a) this repository deploys only
   `pir-` resources; (b) the successor never deploys `pir-` resources.

### 4.4 Known remaining couplings (accepted for now)

- **Shared device role.** Any permission added for the VMS Pi also reaches the successor's
  Pi; today that is `PirLocal`. The only fix is a separate IoT Thing, certificate and role
  for the VMS Pi.
- **One MQTT session for two Pis.** `adapter-01` can be connected from one Pi at a time. Start
  and Stop on cam‑01 in the successor's page reach whichever Pi holds it, so a successor user
  can start this project's KVS producer, which costs money. This was true before this plan.
- **The ONVIF event path stays shared.** cam‑02's detection clips go through the successor's
  `clip-to-s3`. The successor keeps it working for its own ONVIF cameras, so the risk is low.
  If needed, the follow-up is an own topic, rule and `pir-clip-to-s3`; that needs a device
  grant on the new topic.
- **cam‑01 stays visible in the successor's page,** because the registry is shared, and its
  PIR clips show there with the label `pir`.
- **The contract needs both sides.** Rule 4 and rule 5b are outside this repository's
  control.

---

## 5. Step-by-step plan

Each phase ends with a check that proves it works.

- Phases 0–8 deploy nothing to AWS.
- Phases 9–11 create only `pir-` resources.
- **No phase modifies a shared resource.**

### Phase 0: Network and broker (Pi 4B)

**Status on 2026-10-03**, checked on the Pi:

| Step | State | Evidence |
|---|---|---|
| 1. Fixed IPv4 for both boards | **Done** (2026-10-03, set on the FRITZ!Box) | Both boards have a fixed IPv4 address on the FRITZ!Box. The Pi still uses DHCP (`ipv4.method auto`) and always receives `192.168.178.53`, the address the Pico is built with. The Pico was offline at the time of the check, so its address wasn't seen from the Pi; the broker doesn't need it |
| 1b. FRITZ!Box as LAN time server | Not set up; optional | The Pi syncs from the default NTP servers (`NTPSynchronized=yes`) |
| 2. Network | **Done** (Wi‑Fi variant) | `eth0` has no cable; the NetworkManager profile has `802-11-wireless.powersave = disable`; `iw dev wlan0 get power_save` → `off` |
| 3. Mosquitto | **Done** (2026-10-03) | 2.0.21 installed. `conf.d/vms.conf`, `passwd` (three users) and `acl` in place; `passwd` and `acl` owned by `mosquitto:mosquitto`, mode 600. Listening on `0.0.0.0:1883` and `[::]:1883`; the log shows no warnings |
| 4. Check | **Done** (2026-10-03) | An anonymous client is refused on `localhost` and on `192.168.178.53`; `pico` with a wrong password is refused. `pico` publishing via `192.168.178.53` reached a `vms` subscriber; a `gui` publish to `pir/cmd/retrigger` did not; a `vms` publish to it did ([B.6](#b6-test-it)). Not yet tried from a second machine; the Pico's first connection in Phase 1 will be that test |
| 5. `paho-mqtt` in `venv-adapter` | **Done** (2026-10-03) | `paho-mqtt` 2.1.0 under Python 3.13.5. An anonymous test connection to the local broker was answered "Not authorized", so the library reaches the broker |

Steps:

1. On the FRITZ!Box:
   - give both boards **DHCP reservations. This is required, not optional:** the Pico has
     the broker's IP compiled in (Phase 1, step 3);
   - keep both off the guest network;
   - optionally enable the FRITZ!Box as the LAN time server.
2. Pi 4B network: **Ethernet** (preferred), or Wi‑Fi with power save off
   (`nmcli connection modify "<profile>" 802-11-wireless.powersave 2`). Done.
3. **Install and configure Mosquitto.** The full manual is
   [Appendix B](#appendix-b-mosquitto-on-the-pi-4b-setup-manual). In short:
   - `sudo apt install mosquitto mosquitto-clients`;
   - three broker users, `pico`, `vms` and `gui`, in `/etc/mosquitto/passwd`;
   - their topic permissions in `/etc/mosquitto/acl`. `vms` is used by both the watcher and
     the admin app, which sends the retrigger command; `gui` is read-only, for the optional
     kiosk and scripts;
   - `/etc/mosquitto/conf.d/vms.conf` with `listener 1883`, `allow_anonymous false` and the
     two file paths;
   - persistence is already on in Debian's stock `mosquitto.conf`.
4. **Check** ([B.6](#b6-test-it)):
   - anonymous clients and wrong passwords are refused;
   - `pico` publishing via the LAN address reaches a `vms` subscriber;
   - a `gui` publish to the command topic never reaches it;
   - `iw dev wlan0 get power_save` reports `off`.
5. `venv-adapter/bin/pip install paho-mqtt` (needed from Phase 2 on).
   `venv-adapter/bin/python3 -c 'import paho.mqtt'` proves it.
   - **paho-mqtt 2.x changed its API:** `mqtt.Client()` takes the callback API version as its
     first argument. New code uses `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)`, whose
     `on_connect(client, userdata, flags, reason_code, properties)` gets a reason code rather
     than an integer. Most examples online are written for 1.x.

**What Phase 0 installed on this Pi** (2026-10-03):

| Software | Version | Installed with | Where | Check that it works | Remove |
|---|---|---|---|---|---|
| `mosquitto` (broker, system service) | 2.0.21-1 | `sudo apt install mosquitto mosquitto-clients` | Debian package; config in `/etc/mosquitto/` ([Appendix B](#appendix-b-mosquitto-on-the-pi-4b-setup-manual)) | `systemctl is-active mosquitto`; `ss -ltn \| grep 1883` shows `0.0.0.0:1883` | `sudo apt purge mosquitto` (also deletes `/etc/mosquitto/`, so back up first, B.11) |
| `mosquitto-clients` (`mosquitto_pub`, `mosquitto_sub`) | 2.0.21-1 | same command | Debian package | the B.6 tests | `sudo apt remove mosquitto-clients` |
| `paho-mqtt` (Python MQTT client) | 2.1.0 | `venv-adapter/bin/pip install paho-mqtt` | `venv-adapter/lib/python3.13/site-packages`; system Python untouched | `venv-adapter/bin/python3 -c 'import paho.mqtt; print(paho.mqtt.__version__)'` | `venv-adapter/bin/pip uninstall paho-mqtt` |

- **The fresh-Pi checklist now covers both:** `LAUNCH.md` **A10** (optional, only for the PIR
  trigger) installs and proves them, and Part A's "what's missing" check reports the broker.
  The repository has no requirements file (CLAUDE.md: "no build system, package manifest"),
  so A10 is what a rebuilt Pi or a recreated venv follows.
- `paho-mqtt` stays out of A4's package list, which is "every third-party module `adapter/`
  imports". Until Phase 6 adds the watcher, no VMS code imports it.

### Phase 1: Pico 2 W firmware (`blink_freertos`)

**Status: done** (reported 2026-10-03). The Pi side confirms the contract is met (Phase 2,
first findings); the hold-time potentiometer is still to be set.

Owned by `blink_freertos`; summarised here. **The full version, with the interface contract,
is `PIR-MQTT-VMS-Pico.md` §3 and §4.3.** New since the original plan: the `pir/state` fields,
its warm-up form and heartbeat, and the 64-bit `boot_ms`.

1. **Hooks with timestamps:** `pir_on_motion_start(uint32_t time_ms)` and
   `pir_on_motion_stop(uint32_t time_ms, uint32_t duration_ms)`. Pass the ISR timestamp
   through from `pir_apply()`, and update README-PIR.md §5.
2. **New `lan_mqtt_task.c/h`**, the sole owner of the second lwIP MQTT client.
   - **Connection:**
     - wait for Wi‑Fi only (`wifi_wait_connected()`), **not** for SNTP;
     - plain TCP (`tls_config = NULL`), user and password, keep-alive 30 s;
     - LWT: `status` = `offline`, retained, QoS 1.
   - **On connect, in this order:**
     - publish `status` = `online` (retained);
     - publish `pir/state` (retained) from `pir_get_status()`, or in its warm-up form while
       that returns false, with `boot_ms` = current uptime. This comes before any queued
       event, so the Pi gets a fresh clock reference (§3.5);
     - subscribe to `pir/cmd/#`. `retrigger` calls `pir_task_set_retrigger()`, which is a
       single atomic GPIO write and safe from the lwIP callback;
     - then drain the event queue.
   - **Event path:**
     - the strong hook override only does `xQueueSend(…, 0)` into a ~16-entry queue; if the
       queue is full, it counts a drop and never blocks the `pir` task;
     - loop: `xQueuePeek()` → publish → `xQueueReceive()` only after `ERR_OK`; on `ERR_MEM`,
       back off ~50 ms and retry;
     - after each event, republish the retained `pir/state`.
   - **Heartbeat:** republish `pir/state` every 30 s, with `boot_ms` = uptime at publish.
   - **Plumbing:**
     - every lwIP call between `cyw43_arch_lwip_begin()` / `cyw43_arch_lwip_end()`;
     - reconnect with exponential backoff;
     - `snprintf` length guard on every payload.
3. **Configuration:** gitignored `lan_mqtt_config.h` plus `lan_mqtt_config.h.example`
   (broker IP, user, password), the same pattern as `wifi_credentials.h`.
4. **`lwipopts.h`:** `MEM_SIZE` → 16000, `MQTT_OUTPUT_RINGBUF_SIZE` → 512,
   `MEMP_NUM_SYS_TIMEOUT` → 18.
5. **`main.c`:** create the task at `tskIDLE_PRIORITY + 2`; add the source to `CMakeLists.txt`.
6. **Check:**
   - a wave produces `start` and `stop` with matching `seq` in `mosquitto_sub` within
     milliseconds;
   - `pir/state` arrives every 30 s with a growing `boot_ms`;
   - powering off the Pico yields `offline` after ~45 s (1.5 × keep-alive);
   - with the internet unplugged, local events still flow;
   - AWS telemetry still arrives every 10 s (D6: the direct path stays).

### Phase 2: Observation (measure before building, as VMS's ONVIF Phase 0 did)

**Status on 2026-10-03: the tool is built, tested and configured, and the Pico publishes. The
measurements are postponed** (your decision, 14:30), with the hold time still untuned (first
findings, below). Phase 3 went ahead in the meantime. Resume with step 0.

| Item | State |
|---|---|
| Logger and analysis, `adapter/observe_pir.py` | **Done.** Tested against a throwaway broker with synthetic Pico traffic, and against a synthetic 20-hour log with known answers (below) |
| MQTT keys in `/etc/adapter/adapter.env` and `config/adapter.env.example` | **Done** (`MQTT_HOST=localhost`, `MQTT_PORT=1883`, `MQTT_USER=vms`, `MQTT_PASSWORD_FILE`). The live file was backed up first to `adapter.env.bak-2026-10-03` |
| The `vms` password file `/etc/adapter/mqtt-vms.password` | **Done** (2026-10-03, [B.7](#b7-credentials-for-vms-programs-needed-from-phase-2)): `root:vladimir`, mode 640, plaintext with no trailing newline. The B.7 proof passed: a 30 s run against the real broker logged `connected` and no `connect_failed` (0 messages, since nothing publishes yet) |
| Afternoon run, night run, single-trigger run | **On hold** until the hold time is set (first findings, below) |

**First findings, 2026-10-03 14:20**, Phase 1 firmware running. From a 70 s capture with
`observe_pir.py` and the broker log:

- **The contract is met.**
  - The Pico logs in as `pico2w-01` from `192.168.178.63`, with keep-alive 30 s.
  - `status` is retained `online`; `pir/state` carries `motion`, `changed_ms`, `count`,
    `dropped` and `boot_ms`, and arrives every 30.000 s.
  - A start and its stop share a `seq`; stops carry `duration_ms`; everything is QoS 1.
  - Delivery is immediate: the start and the state published 12 ms later give the same
    `receive − boot_ms` to within 1 ms.
- **The hold time is far too short.** Motions lasted 0.44 s and 0.76 s, so T_hold < 0.5 s:
  the module's potentiometer is at minimum (README-PIR.md §6, item 4). One person walking
  through would produce a burst of tiny start/stop pairs, so the measurement waits until the
  potentiometer is set (Appendix A.5 step 1).
- **The Pico's `ts` is 0.87 s behind the Pi's clock,** although `boot_ms` shows no delay.
  lwIP's SNTP sets the Pico's clock in whole seconds (`time_task_sntp_set_system_time(uint32_t sec)`).
  This confirms §3.5's decision to time events by `boot_ms`, never by `ts`.
- **One reconnect at 14:08: a deliberate power cycle** (confirmed). It doubled as Phase 1's
  offline check, and passed:
  - power off at ~14:07:45;
  - the broker dropped the Pico at 14:08:30, 45 s later (1.5 × keep-alive), and published
    the retained `offline`;
  - power on at ~14:08:54 (from `boot_ms`);
  - back on the broker at 14:08:57, **3 s after power-on**, Wi‑Fi and login included.

**The tool, `adapter/observe_pir.py`.** It is built on the same lessons as
`adapter/observe_events.py`, the harness behind the ONVIF figures:

- one JSONL line per message, with the Pi's receive time, flushed at once, so an
  interrupted run keeps everything before the interruption;
- a harness heartbeat every 5 min, and the Pico's own 30 s `pir/state` heartbeat, so a quiet
  night is a measurement and an absent Pico is visible;
- payloads that aren't the expected JSON are kept, as text or hex;
- connects, disconnects and failed logins are recorded, so a gap is explainable;
- **a clean MQTT session**, unlike the watcher. A persistent one would make the broker queue
  days of events between runs and replay them into the next log with the wrong receive time;
- connection details from `adapter.env`, read only when observing, with command-line
  overrides; `--analyse` needs no configuration at all.

**Steps:**

0. **Set the hold time first** (added after the first findings). Every later figure depends
   on it. Watch the events live:

   ```bash
   venv-adapter/bin/python3 adapter/observe_pir.py --tail      # Ctrl+C to stop; writes no file
   #   14:31:07  motion start #31
   #   14:31:11  motion stop  #31  lasted 4.12 s
   ```

   Give **one brief wave, then stay still or out of view**; the stop's duration is T_hold.
   Turn the module's potentiometer and repeat until a brief wave gives **about 3–5 s**:
   - long enough that one person crossing the hall is one start/stop pair, not a burst;
   - short enough that the clip doesn't run on long after they've gone (POST_SEC adds
     ~4–5 s on top, Appendix A).

   Then check the mode switch: in single-trigger mode (`retrigger` = `0`) continuous waving
   gives repeated pairs of about T_hold each; back in retriggerable mode (`1`), one long pair.
   If both behave the same, S1 isn't taking effect (README-PIR.md §6, item 4), and step 2
   can't be run.
1. **Afternoon, then night.** Log `home/pico2w-01/#` to
   `measurements/pir-<date>-<label>.jsonl`. Run it bounded: a transient unit survives logout
   but not a reboot, and **never runs as an enabled unit**:

   ```bash
   cd ~/MyProjects/VMS
   systemd-run --user --unit=pir-observe --collect \
     "$PWD/venv-adapter/bin/python3" "$PWD/adapter/observe_pir.py" \
     --out "$PWD/measurements/pir-$(date +%F)-afternoon.jsonl" --hours 5
   journalctl --user -u pir-observe -f          # its console output
   systemctl --user stop pir-observe            # end early; the log closes cleanly
   ```

   Only one copy at a time; each run appends to its `--out` file.
2. **Single-trigger mode** for a shorter period, to see how often "ignore" would actually
   fire. Switch the module during a run:

   ```bash
   mosquitto_pub -h localhost -u vms -P '<vms-password>' -t home/pico2w-01/pir/cmd/retrigger -m 0
   #   ... later, back to the firmware default:
   mosquitto_pub -h localhost -u vms -P '<vms-password>' -t home/pico2w-01/pir/cmd/retrigger -m 1
   ```

   The logger records the command, and the analysis splits its figures by mode.
3. **Analyse:**

   ```bash
   venv-adapter/bin/python3 adapter/observe_pir.py --analyse measurements/pir-<date>-<label>.jsonl
   ```

   It reports:

   | Section | What it answers |
   |---|---|
   | coverage, broker, clock | Is the log trustworthy: harness heartbeats complete, broker disconnects, NTP sync |
   | Pico boots, lateness, drift | Reboots (from `pir/state` only, §3.5); how late messages arrive; how many exceed `STALE_SEC`; the Pico crystal's drift in ms/h |
   | Pico heartbeat, status | Silences over 75 s, `online`/`offline` changes, warm-up states, queue drops |
   | events | Unique events after QoS 1 duplicates; unpaired starts; **missing `seq` numbers, i.e. lost events** |
   | starts by hour; per mode | Motion duration (**T_hold ≤ the shortest**) and the **stop → next start gaps** with counts under 5/10/15/30/60 s (Appendix A.5 steps 1 and 3) |
   | quiet window (`--quiet`, default 22:00–07:00) | Starts in the night, grouped into clusters with times, for you to judge as **false positives** |
   | duty cycle | A first estimate for POST_SEC 0/5/10/30 s with a simplified session model. Phase 4's replay of the real `ClipSession` supersedes it |

4. **For POST_SEC specifically,** also measure the PIR hold time with a single brief wave,
   and how far the camera sees beyond the PIR's field (Appendix A.5 steps 1–2). Those two
   need a person and the camera's live view, not just the log.

**How the analysis was checked.** I generated a 20-hour synthetic log with known answers and
ran `--analyse` on it. Every figure came out right:

- 74 afternoon motions, of which 4 duplicates were removed;
- one hour in single-trigger mode, with every motion exactly T_hold (5.0 s);
- a Pico reboot at 02:00, seen as 2 boots, an 80 s heartbeat silence and an offline/online
  pair;
- a 3-start false-positive cluster at 03:21;
- events delivered 20 s and 90 s late, with the 90 s one flagged as stale;
- one lost event, found as 1 missing `seq`;
- a 30 s broker disconnect;
- the Pico clock's +10 ms/h gain, measured as −9 ms/h.

The first version miscounted 4 boots: it took a late event's older `boot_ms` for a reboot.
That exposed the same flaw in §3.5's rule, corrected there.

### Phase 3: Registry and the local switch (no recording yet)

1. Add `pirTopic` to the cam‑01 registry row (field-level update, §4.3 rule 1).
2. Admin app: a PIR on/off switch, `POST /api/cameras/<id>/pir`:
   - writes `pirRecording` with a field-level update;
   - refuses a camera without `pirTopic`;
   - leaves `recordingMode` and the existing mode endpoint unchanged.
3. **Check:**
   - the switch sets and clears `pirRecording` on cam‑01 and refuses cam‑02;
   - `recordingMode` stays `manual`; `event_watcher.py` logs no watch for cam‑01;
   - `GET /cameras` on the shared API returns exactly the same fields as before (the
     successor's view is unchanged).

**Status: done on 2026-10-03.** The look at the page in a browser, pending here, was done in
Phase 8 (headless Firefox through Marionette).

What was built:

- **`pirTopic` on cam‑01**, written with one field-level update. The condition stops a
  mistyped key from creating a new row:

  ```bash
  aws dynamodb update-item --table-name cameras --key '{"cameraId":{"S":"cam-01"}}' \
    --update-expression "SET pirTopic = :t" --condition-expression "attribute_exists(cameraId)" \
    --expression-attribute-values '{":t":{"S":"home/pico2w-01/pir/event"}}'
  ```

- **`POST /api/cameras/<id>/pir`** in `adapter/onvif-admin/app.py`, modelled on the audio and
  outage-buffer endpoints:
  - body `{"pirRecording": true|false}`; anything else gets 400, an unknown camera 404;
  - switching on needs `pirTopic`; switching off is always allowed;
  - **a request that changes nothing writes nothing**, so "off" on a camera without a PIR
    sensor doesn't plant a `pirRecording` field in a row the successor also reads;
  - `recordingMode` is never touched.
- **A "PIR trigger" checkbox** in the admin page's Recording column, shown **only for cameras
  with `pirTopic`**, not greyed out on every camera. Its status text says plainly that
  nothing records on it yet. A refused change puts the box back, as the audio box does.
- `GET /api/cameras` needed no change: it already returns every field of each row.

Results of the checks:

| Check | Result |
|---|---|
| cam‑01 on, then off | 200 both times; the row reads `pirRecording: True`, then `False`; `recordingMode: manual` throughout; the page's data shows the same |
| cam‑02 on | **400**, "no PIR sensor configured for this camera (pirTopic)" |
| cam‑02 off | 200, and **no write**: cam‑02's row still has no `pirRecording` field |
| `{"pirRecording": "yes"}` / `cam-09` | 400 / 404 |
| The successor's view | The shared `list-cameras` Lambda, invoked before and after: **identical output**, no `pir` field visible |
| The ONVIF event watcher | Kept logging "no cameras in a detection mode; idling" while cam‑01's switch was on |
| **The page in a browser** | **Done in Phase 8**, with headless Firefox through Marionette. At the time: headless Chromium and Firefox on the Pi gave only blank or too-early screenshots, and there's no JavaScript engine for an offline check. Open `http://192.168.178.53:8080`: the "PIR trigger" box should appear only on cam‑01's row, and ticking it should show "on — saved; local recording not built yet" |

cam‑01's switch was left **off**.

### Phase 4: `ClipSession` and the replay tool

1. `adapter/pir_session.py`: `ClipSession` (§3.3) and event ageing (§3.5) as pure logic, with
   no MQTT, no files and time passed in, the way `ClipGate` is written.
2. `adapter/bin/replay-pir.py`: runs a Phase 2 `.jsonl` (the format `adapter/observe_pir.py`
   writes) through it and prints clips, ignored
   events, stale events and duty cycle for given `POST_SEC`, `MAX_SEC` and `STALE_SEC`.
3. **Check:**
   - replaying the Phase 2 logs gives plausible clip counts;
   - a synthetic backlog (receive times shifted later) is dropped beyond `STALE_SEC`, and
     otherwise placed at the event time.

**Status: done on 2026-10-03.** Phase 2's real logs don't exist yet, so the replay check used
the synthetic 20-hour log and the 70 s real capture from Phase 2's first findings.

**`adapter/pir_session.py`** is pure logic: no MQTT, no files, no clock reads. Every method
is told the time it acts at. It has three parts:

| Part | Job | Notes |
|---|---|---|
| `PicoClock` | §3.5: event time = `min(receive − boot_ms)` over 10 min + `boot_ms` | Reboots from `pir/state` only. Valid once a message arrives `SETTLE_SEC` (5 s) after a connect, or when restored for the same boot. `to_dict()` / `from_dict()` are what the watcher persists |
| `ClipSession` | §3.3: idle → recording → post-roll | `tick(now)` before every input closes an expired session, so a start after the post-roll opens a new clip. Closes on stop + POST_SEC, on the cap, on `offline`, or at the end of a replay |
| `PirTrigger` | What the watcher calls per message | Dedupe by `(reboot count, seq, event)`; ageing; a stale start is dropped, a stale stop still closes its session. Retained messages must not reach it |

Running the file directly checks **11 scenarios**, each a rule a regression would break
silently:
- one clip per start/stop pair;
- events ignored while recording or in post-roll;
- a start after the post-roll opens a new clip;
- the cap;
- a late event placed at its event time;
- a stale start dropped;
- a late event not counted as a reboot;
- nothing trusted right after a connect;
- `offline` closing a clip;
- duplicates counted once;
- a persisted reference surviving a restart, but not a Pico reboot.

```bash
venv-adapter/bin/python3 adapter/pir_session.py       # pir_session: all 11 scenarios pass
```

**`adapter/bin/replay-pir.py`** runs an `observe_pir.py` log through it:

```bash
venv-adapter/bin/python3 adapter/bin/replay-pir.py measurements/pir-<date>-<label>.jsonl \
  [--post 0,5,10,30] [--max 180] [--stale 60] [--clips]
```

For each POST_SEC it prints clips, recorded minutes, the duty cycle, ignored and stale
starts, unageable events and duplicates; `--clips` lists every clip. It assumes the ring was
recording throughout. **It deliberately doesn't apply the settle rule:**
- `observe_pir.py` logs through a clean MQTT session, so its logs can't hold broker backlog;
- the first replay of the real capture applied the rule and dropped both motions, because
  they came within 5 s of the logger connecting;
- the rule stays in `PicoClock` for the watcher's persistent session (Phase 6).

Results:

| Check | Result |
|---|---|
| Synthetic 20 h log, POST_SEC 0/5/10/30 | 84 / 77 / 69 / 61 clips, duty 3.1–4.8 %. Agrees with `observe_pir.py`'s rough estimate, except **one clip fewer: the real logic drops the 90 s-late start as stale**. The 8 "stray stops" at POST_SEC 5 are the stops of the 7 ignored starts plus the stale one |
| A late event | The event at 02:11:00, received at 02:11:20, gives a clip starting **02:10:48** (event time − 12 s), not 02:11:08 |
| A stale event | The event received 90 s late at 02:16 is dropped; no clip |
| **The real capture** | POST_SEC 0: two clips (17.8 s and 12.4 s). **POST_SEC 5: one clip.** The second motion began 3.1 s after the first stopped, inside the post-roll, and was ignored, so that activity would be lost (Appendix A.4). With T_hold at 0.4 s, every pause in someone's movement shows up as a stop. That is why Phase 2's step 0 (tuning the hold time to ~3–5 s) comes first |

### Phase 5: Ring recording in PIR mode (supervisor)

1. `outage_buffer.py`:
   - `_fetch_registry()` also returns `pirRecording`;
   - `want` gains cameras with `pirRecording` on (still subject to `buffer_ready()` and
     `disk_ok()`), independent of `ProducerLatch`;
   - the arming log line names the reason: outage or pir.
2. Session capture: read `pir/*/state.json`, link overlapping completed ring segments, write
   `sweep.json` (§3.6).
3. The ring stays on the stick for bring-up (D7).
4. **Check:**
   - with `pirRecording` on and `kvs-cam01` stopped, `live/cam01/` holds 4–5 segments and
     keeps rolling, and the MediaMTX path's `bytesReceived` advances;
   - a hand-written test journal makes the supervisor link the right segments and advance
     `sweep.json`;
   - switching PIR off disarms within 60 s;
   - **outage regression:** `OUTAGE.md` §7's verification still passes with PIR on,
     including an outage capture while a PIR session holds links to the same segments.

**Status: done on 2026-10-03,** including the live outage regression (below).

What changed in `adapter/outage_buffer.py`:

- **Registry:** each row reads as `{"outageBufferSec", "pirRecording"}`. `_normalise()`
  also loads the old cache format (`{"cam-01": 0}`), which the restart did.
- **Arming:** a camera is armed for `outage` (limit > 0 and producer active), for `pir`
  (switch on, producer or not), or `outage+pir`. The log names the reason (`ARMED (pir)`),
  and a change of reason while armed is logged too.
- **Outage capture only for outage-armed cameras.** A PIR-only camera has limit 0, which
  `Capture` reads as "no limit", so it would have been captured for the whole outage.
- **Retention:** a PIR-only camera keeps pruning during an outage. Without that, its ring
  would grow for as long as AWS stayed unreachable.
- **PIR session capture:** `sweep_pir_sessions()`, run every tick before the outage sweep
  (§3.6).

Results:

| Check | Result |
|---|---|
| Offline: an outage capture and PIR sessions sharing segments, using the supervisor's real functions on a temporary directory | **All 6 cases pass.** The capture renamed 5 segments out of `live/` and the PIR copies survived (link count 2). A session opened during the outage found its pre-roll in `outage/`. A never-closed journal was capped. An unreadable journal was skipped and logged once |
| Arming on the real system | Switch on at 15:37; the registry poll saw it at 15:38:05; **`cam-01: recording ARMED (pir)` at 15:38:09**, with `kvs-cam01` inactive |
| The ring | **4 segments** (three from the last 120 s plus the open one), each ~3.76 MB per 30 s = 1.0 Mbit/s; **rolling**: the oldest was replaced 40 s later; `bytesReceived` advancing |
| A hand-written journal | Open session: 2 segments linked within a tick. Closed with `to` 15 s ahead: 4 segments and **`complete` at 15:44:17**, once the segment containing `to` finished. Coverage started 25 s before `from` and ended 26 s after `to` (whole segments; Phase 6 trims) |
| Link, don't move | The first linked segment showed **link count 1** once the ring had pruned its own copy: the session's copy survived the ring's retention |
| The footage | The 4 segments join into **120.0 s of H.264, 1280×720**, and a frame from the middle decodes to the live scene |
| Switch off | **Disarmed 17 s later**; MediaMTX `record: false`; the ring stopped growing |
| **Live outage regression** (`OUTAGE.md` §7), 16:11–16:22 | **Passed** (details below) |

**The live outage regression, step by step.** cam‑01 ran with `outageBufferSec` 300,
`pirRecording` on and `kvs-cam01` uploading, with a PIR test session open across the outage:

| Step | Time | Result |
|---|---|---|
| Armed | 16:11:32 | `cam-01: recording ARMED (outage+pir)`; KVS acknowledging fragments |
| Safety timer, then block | 16:12:01 | A one-off root timer was set to remove the rules after 12 min whatever happened. Then the rules dropped AWS's ranges for IPv4 (`3/8 18/8 35/8 52/8 54/8`) and IPv6 (`2a05::/16`). **Verified:** the IoT, credentials and S3 endpoints timed out on both families; `api.anthropic.com` still answered |
| Outage detected | 16:12:15 | 14 s after the block (`mqtt interrupted`); capture `20261003T141215Z` |
| During the outage | to 16:16 | 8 segments in the capture, and **every one also linked in the PIR session (link count 2)**. The session journal was held open past its cap and was capped at 16:15:53, by design |
| Unblock | 16:16:26 | After 265 s; rules removed, timer cancelled, AWS reachable on both families |
| Recovery | 16:16:52 | `RECOVERED after 277s` |
| Backfill | 16:18:05–18 | After the uploader's 60 s settle: 10 segments merged into **one 36 MB, 304 s clip**, uploaded to `clips/cam-01/2026/10/03/161133-outage.mp4`, registered (`outage-buffer`), local copy deleted |
| PIR copies after that deletion | | Link count **1**: the session's footage survived the outage capture's removal |
| The clip | | **304.0 s of H.264, 1280×720**; a frame from inside the blocked period decodes to live footage |
| KVS itself | | **152 s missing** (16:11:57 → 16:14:29); the producer's buffer replayed the rest. The outage clip covers 16:11:33 → 16:16:37, so it **fills the whole gap** |
| Restore | 16:20–16:22 | `kvs-cam01` stopped; `outageBufferSec` 0; `pirRecording` off; disarmed at 16:22:18; test session deleted; no firewall rules or timers left |
| The test clip | 16:3x | **Deleted at your request** (it showed you on camera, in both projects' clip lists): its `clips` row (conditioned on its `s3Key`) and the S3 object. The bucket has no versioning, so no copy remains |

Two observations from the run:

- The new outage row has **no `videoCodec`**, unlike every other row. That's the gap Phase 7
  closes (§4.3, rule 3).
- **A Wi‑Fi drop, unrelated to the test, at 16:21.** The FRITZ!Box ran a burst of group
  rekeyings and disconnected the Pi (`reason=2`) 5 min after the block was lifted. The agent
  (16:20:54–16:21:32) and one supervisor registry read (16:21:13) both lost AWS for ~40 s and
  recovered by themselves. It delayed the disarm by one registry poll, and it's a reminder
  that Phase 0 preferred Ethernet.

Found on the way:

- **The cam‑01 publisher had been hung since boot** (`FoundAndFixed.md` #51, open). `ready`
  since 08:03:23, `bytesReceived` frozen at 83 KB, no error, `active`. Restarting
  `kvs-camera-publish` brought the video back. With a hung publisher the ring is armed and
  records nothing; the watcher's ring check (§3.3) will catch it, but only reports. A
  run-time watchdog for the publisher is still to do.
- **A disarmed camera's last ring files stay** (≤ 5, ~19 MB) until it is armed again, when
  normal pruning removes them. That's existing outage-buffer behaviour, bounded, and left
  as it is.
- `pirRecording` was left **off**: with the ring still on the stick, PIR mode mustn't run
  unattended (D7, Phase 12).

### Phase 6: `kvs-pir-watcher.service` (new VMS user unit), local part

1. `adapter/pir_watcher.py`:
   - waits for `NTPSynchronized=yes` before acting on any event (§2.3);
   - every 60 s, reads the cameras with `pirRecording` on from the registry cache (§3.8),
     the same cadence as `event_watcher.py`;
   - subscribes to each `pirTopic`, plus `status` and `pir/state`, on localhost with a
     persistent session;
   - runs one `PirTrigger` per camera from `adapter/pir_session.py` (Phase 4), the exact
     code the replay tested. It calls `connected()` on every (re)connect, because its
     persistent session can deliver backlog, and persists `PicoClock.to_dict()` so a restart
     keeps the clock reference;
   - adds the ring check, the journal, and merge + trim + thumbnail once `sweep.json` has
     caught up;
   - applies local retention (D8, §3.6);
   - supports `--dry-run`: it logs sessions and writes no journals.
2. **A separate process** from `event_watcher.py`, following VMS's rule that a stalled broker
   must not stop ONVIF detection. It is also separate from the supervisor, whose tick loop
   must stay free of slow work; merges take seconds.
3. **Heartbeat state file** `$XDG_RUNTIME_DIR/vms/pir-state.json`, rewritten every 10 s (the
   `aws_state.py` pattern: a stale file means *unknown*). It holds the same fields as the
   cloud status item (§3.10).
4. Add the unit to LAUNCH.md A8's unit table.
5. **Check:**
   - `--dry-run` while waving logs exactly one session plus the ignored events;
   - for real, `pir/<sessionId>/clip.mp4` and its thumbnail appear ≤ ~35 s after the clip's
     end, and `ffprobe` shows a duration ≈ the window (within 2 s at each end);
   - **decode a frame and look at it, and play the clip in a browser**, not only in
     `ffprobe` (CLAUDE.md, "Verifying changes");
   - the footage starts before the hand enters the frame;
   - with `kvs-cam01` stopped, the clip still appears;
   - killing the watcher mid-session and restarting it yields one clip with the right window;
   - with the internet unplugged, local clips keep coming.

**Status: done on 2026-10-03,** including real motion through the real Pico. Browser playback
was checked with Phase 8's clip list.

**`adapter/pir_watcher.py`**, unit `kvs-pir-watcher` (user, enabled):

- **Inputs:**
  - the registry from the supervisor's cache, which now also carries `pirTopic`. The watcher
    makes **no AWS call** (checked: no `boto3` or credentials code in it);
  - MQTT on localhost as `vms`, client id `vms-pir-watcher`, persistent session. For each
    camera with `pirTopic` it subscribes to `<base>/pir/event`, `<base>/pir/state` and
    `<base>/status`; sessions run only while `pirRecording` is on.
- **Logic:** one `PirTrigger` per camera, the Phase 4 code. It calls `connected()` on every
  (re)connect, persists each clock reference to `~/.local/state/vms/pir-watcher-clocks.json`,
  and passes §3.3's ring check (`ring_ok`: the newest ring file is < 60 s old). That check
  also catches a hung publisher (#51).
- **Journal** `pir/<sessionId>/state.json`, written atomically:
  - `recording` when a session opens;
  - `post-roll` with its `end` when the stop arrives, so a restart in the post-roll keeps it;
  - `closed` with `to` and the reason;
  - then `merged` (or `failed`, with the error).
  - Switching PIR off, removing the camera, or a wall-clock jump closes an open session with
    that reason.
- **Assembly**, on its own thread: once `sweep.json` says `complete` (or 10 min after `to`,
  with whatever was captured), it merges with `merge()` plus `inpoint`/`outpoint`.
  - A track-layout change keeps the longest run.
  - It writes `clip.mp4` and a 160×90 `thumb-1.jpg` (D13: a frame 2 s after the motion began),
    removes the linked segments, and records `durationSec`, `window`, `videoCodec` and
    `sizeBytes`.
- **Heartbeat** `$XDG_RUNTIME_DIR/vms/pir-state.json` every 10 s: NTP, broker, clock jumps, per
  Pico (online, last state, motion, count, warm-up, drops) and per camera (switch, ring
  recording, session state, last event and clip, clock validity, counts).
- **Retention (D8)** every 10 min:
  - merged or failed clips older than 14 days are deleted unless they carry `keep` or a
    pending `upload-requested`;
  - while free space is below the supervisor's floor + 2 GB, the oldest unkept clips go
    first and pending ones last; kept clips never go;
  - open, closed or assembling sessions are never touched.
- **NTP:** it waits for `NTPSynchronized=yes` for **at most 5 min**, then carries on (see the
  consistency record), and treats a later step of the wall clock as a *clock jump*.

Results. The tests used a **simulated Pico on a throwaway broker** (`127.0.0.1:18830`, with the
watcher pointed at it via `MQTT_*` environment overrides) against the **real supervisor, ring
and cam‑01 footage**. That controls event timing exactly; the real Pico had just gone offline
anyway (below).

| Check | Result |
|---|---|
| `--dry-run` | One session opened on the start, closed 5 s after the stop (clip 16:33:41 + 20.4 s = 12 + 3.4 + 5); **no file written** to the stick |
| A real clip | An 8 s motion: session closed 16:34:44; **`clip.mp4` ready 25 s after the clip's end**. **26.8 s for a 25.4 s window** (+1.4 s, keyframe rounding); H.264 **High** 1280×720, 3.4 MB; thumbnail 160×90; linked segments removed |
| Decode and look | A frame from 13 s into the clip and the thumbnail both show the live scene |
| `kvs-cam01` stopped | Stopped throughout: clips come from the ring alone |
| **Kill mid-session** | `kill -9` with a session open; the stop published **while the watcher was down**; on restart: `resumed open session`, the broker delivered the held stop, and the **clip ends 5.4 s after the real stop** (POST_SEC 5 + the simulator's 0.4 s start-up), not at the restart. One clip, 32.5 s for 31.5 s |
| Supervisor restart | Finished sessions left alone: no segment folder recreated in merged sessions |
| Heartbeat file | Written every 10 s with every field above |
| Retention, by age | On fake sessions: only the old, unkept, non-pending one deleted |
| Retention, near the floor | Oldest unkept first until free space was back above floor + margin; pending and kept untouched |
| The real service | Installed from A8's template, enabled, connected to the real broker as `vms`; saw the real Pico's retained state; PIR switched off again afterwards: supervisor disarmed 16:40:16, watcher `PIR trigger OFF` 16:40:29 |
| **Internet unplugged** | **Covered, not separately run:** the watcher makes no AWS call, and Phase 5's live outage regression showed the supervisor linking a PIR session during a 265 s outage |
| **Real motion, real Pico** (16:49–16:54, after its wiring was fixed) | **Passed.** 13 sessions opened and closed by real stops; all 13 merged (17.7–19.4 s clips for 17.4–18.2 s windows). A frame from the pre-roll shows the scene still, and one just after the 12 s mark shows the arm moving in front of the camera: **the footage starts before the motion** |
| Browser playback | **Done in Phase 8:** a clip played in Firefox from the admin page's clip list |

Found on the way:

- **The real Pico had no network for 35 min** (16:09:48 → 16:44:45, its AWS telemetry
  stopped too). It **never rebooted** (`boot_ms` continuous), its sensor kept counting
  (`count` 145 → 270), and its queue overflowed (`dropped` 225, the new field doing its job).
  Its green LED was off, and fixing a loose wire brought it back; one more drop came 48 s
  after it reconnected. Recorded in `PIR-MQTT-VMS-Pico.md`.
- **The Pico publishes slower than its sensor fires.** In a burst of motions about 1 s apart,
  sessions opened 0.0, 5.3, 6.2, 8.2, 12.8 and 17.4 s after their motions: roughly one
  motion delivered every ~2 s.
  - **Not the watcher:** fed the same burst in-process, it spent 0.7 ms per message (median;
    3.4 ms at worst).
  - **Not the AWS path:** the Pico's telemetry kept its 10 s rhythm throughout, so no TLS
    reconnect was holding lwIP's lock (§2.1).
  - The clips were still placed at the motions' real times (§3.5), which is late-event
    handling working on real data. But a long enough burst would cross `STALE_SEC` and lose
    events.
  - A firmware finding, recorded in `PIR-MQTT-VMS-Pico.md`. Tuning the hold time (Phase 2,
    step 0) will hide most of it, since one person becomes one pair instead of a burst.
- 15 clips stay on the stick (36 MB): the two synthetic-event ones from 14:34 and 14:35 UTC
  and 13 from real motion at 14:49–14:53 UTC, under `/mnt/vms-buffer/pir/cam-01-<UTC>/clip.mp4`.
  They're local only; retention removes them after 14 days.
- `pirRecording` was left **off** (D7).

### Phase 7: Uploads requested on the LAN

1. `outage_uploader.py`:
   - `upload_and_register()` takes a key suffix and writes **`videoCodec`** from `ffprobe`,
     for PIR and outage uploads alike;
   - a new scan of `pir/*/upload-requested`;
   - writes `uploaded.json`; keeps the local clip.
2. **Check:**
   - a requested clip appears in the cloud clip list labelled `pir`, with a codec, and plays
     from its presigned URL; in the successor's page too;
   - repeating the request registers nothing twice;
   - with the internet down, the request stays pending and goes through on recovery;
   - a clip without a request is never uploaded;
   - an outage clip uploaded afterwards carries `videoCodec`.

**Status: done on 2026-10-03.** All five checks passed, two of them in a short live outage.

What changed in `adapter/outage_uploader.py`:

- **`video_codec(path)`** reads the clip's codec as `h264`/`h265`, the names `clip_to_s3.py`
  and `record_clip.py` write. **`upload_and_register()`** puts it into every row it writes,
  backfill included. It takes a `suffix` for the key name and returns the S3 key, or
  `None` on failure.
- **`pending_pir_uploads()` / `process_pir_request()`**, run every 30 s pass after any
  outage backfill (that footage is what KVS lost, so it goes first):
  - a session with `upload-requested`, no `uploaded.json` and status `merged` is uploaded as
    `HHMMSS-pir-s<seq>.mp4`, with `labels: ["pir", "pir:seq N"]`, `durationSec` from the
    journal and `startTs` = the window's start;
  - then `uploaded.json` is written (`s3Key`, `startTs`, `uploadedAt`, `sizeBytes`,
    `videoCodec`), and **the local clip stays** (D10);
  - a request for a clip that isn't merged yet is logged once and waits;
  - a failed upload leaves the request in place for the next pass.
- The request marker is created by hand for now; the admin page's button comes in Phase 8.

Results:

| Check | Result |
|---|---|
| **A requested clip in the cloud** | `cam-01-20261003T144938Z` requested 17:01:35; uploaded 17:02:06 as `clips/cam-01/2026/10/03/164926-pir-s275.mp4`. The row has `labels: ["pir", "pir:seq 275"]` and **`videoCodec: "h264"`**. The shared `list-clips` (which **both projects' pages** call) returns it at the top of cam‑01's list. `play-clip`'s presigned link delivered it: HTTP 200, `video/mp4`, **byte-identical to the local clip**, H.264 High |
| **Nothing registered twice** | A request left in place: no activity over a full pass. A simulated crash right after uploading (`uploaded.json` removed): the identical file went to the same key, the table **refused a second row** ("a clip already exists … not overwriting"), and `uploaded.json` was rewritten with the same key. Still one `pir` row |
| Only requested clips go up | 1 request, 1 upload, out of 15 clips |
| **Requested during an outage** | AWS blocked 17:06:07–17:08:40 (IPv4 and IPv6, safety timer set first). A request made at 17:06:12 **waited**: the uploader's passes failed and no `uploaded.json` appeared. It **went through after recovery** at 17:11:26 as `165000-pir-s282.mp4`, local copy kept |
| **An outage clip carries `videoCodec`** | The run's backfill, a 332 s head and a 64 s tail (the outage outran its 300 s limit, see below), **both have `videoCodec: "h264"`** |

Found on the way:

- **Recovery took almost 4 minutes, not seconds.** DNS failed from 17:09:50 and the Wi‑Fi
  dropped at 17:11:18. That's the same pattern as at 16:21:18: a burst of group rekeyings,
  then `reason=2`. Today's Wi‑Fi disconnects so far: 09:51:14, 16:21:18, 17:11:18. Recorded
  as `FoundAndFixed.md` #52 (open). It's environmental, not this code, but it costs every
  AWS client minutes of connectivity; Ethernet (Phase 0's preference) would avoid it.
- **The test uploads are in the shared clips table:** two PIR clips (`164926-pir-s275`,
  `165000-pir-s282`) and the two outage clips from 17:05 and 17:11. All show the room, and
  you in it. **Kept at your request** (2026-10-03) for now; they're visible in both projects'
  pages.

### Phase 8: Local GUI (admin app)

1. **"PIR sensor" panel**, fed by a new `GET /api/pir` that reads `pir-state.json`:
   - online/offline, motion, session state (idle / recording / post-roll), effective state;
   - ignored and stale counts, recent events;
   - the PIR on/off switch (Phase 3) and a retrigger-mode toggle, which publishes
     `pir/cmd/retrigger` as user `vms`.
2. **"PIR clips" list**, fed by `GET /api/pir-clips`:
   - each clip served by Flask to a `<video>` tag (LAN only), with its thumbnail;
   - Keep, Delete and Upload to AWS buttons;
   - the upload state; from Phase 10 on, this includes requests made in the cloud.
3. **Optional QML kiosk** (PySide6 + paho-mqtt, Wayland) for a display on the Pi: the PIR
   state plus the MediaMTX live view (D5).
4. **Check:**
   - a wave changes the panel within the heartbeat interval;
   - the new clip shows in the list, plays, and uploads on request.

**Status: done on 2026-10-03,** without the optional QML kiosk (D5: admin panel first).

**The admin page** (`adapter/onvif-admin/`), new sections 5 and 6:

- **5. PIR sensor**, refreshed every 5 s from `GET /api/pir` (the watcher's heartbeat). That
  endpoint marks the data **stale** after 30 s, three missed heartbeats, so a dead watcher
  reads as *unknown* (the `aws_state.py` rule). It shows:
  - **per Pico:** online/offline, heartbeat age, motion or warm-up, motions since boot, events
    dropped on the Pico (in red), the last retrigger command (the Pico reports no mode), and
    the **12 latest events** with their duration and **how late they arrived**;
  - **per camera:** the PIR switch, the ring buffer, the session state and ID, the last motion
    and clip, the counts, and **Retriggerable / Single-trigger** buttons
    (`POST /api/cameras/<id>/pir/retrigger`, published as `vms`; the topic comes from the
    heartbeat, falling back to the registry, so it works without AWS).
- **6. PIR clips**, from `GET /api/pir-clips`, newest first: thumbnail, recording time, length,
  how the session closed, and an AWS badge (*local only*, *upload requested*, *in AWS* with
  the key). Each row has:
  - **Play**, an inline `<video>` served from the stick with byte ranges (206), so it seeks;
  - **Upload to AWS** / **Cancel upload**, which create or remove the `upload-requested`
    marker that Phase 7's uploader consumes;
  - **keep**, the retention marker;
  - **Delete**, with confirmation. It's refused while the session is still recording or being
    assembled, and while an upload is pending. Any copy in AWS stays.
  - The list doesn't refresh while a clip is playing.
- **Session IDs are checked against a strict pattern**, so no request reaches outside
  `/mnt/vms-buffer/pir/`.
- **The watcher's heartbeat gained:** the recent events, kept per Pico even while PIR is off,
  with lateness from a display-only clock reference; and the last `pir/cmd/retrigger`, from
  whoever sent it (it now subscribes to that topic too).

Results:

| Check | Result |
|---|---|
| The endpoints, on live data | `/api/pir`: Pico online, 368 motions, the 225 dropped, 12 events. `/api/pir-clips`: all 15 clips, the two uploaded ones with their keys. Clip: **HTTP 206** for a range request, `video/mp4`. Thumbnail: 200, `image/jpeg` |
| Markers and refusals | keep on/off; upload of an already uploaded clip reports *in AWS*. On dummy sessions: upload or delete while assembling refused (409), delete with an upload pending refused, cancel withdrew it, then delete went through. Path escapes: 404 |
| Retrigger | Published as `vms`; the watcher recorded the command within seconds (sent "retriggerable", the Pico's current mode, so the sensor didn't change) |
| **The page in a real browser** | Headless Chromium renders nothing on this Pi (blank screenshot, 40-byte DOM), so the page was driven through **headless Firefox's Marionette interface** (JSON over TCP, stdlib Python): loaded, data waited for, text read, screenshot taken. Both sections render with live data: thumbnails, badges, the greyed Upload button for uploaded clips |
| **A wave changes the panel** | Live sensor events appeared in the panel within the refresh cycle (heartbeat 10 s + page 5 s); the screenshot shows events from seconds before it was taken |
| **A clip plays** | Clicking Play in Firefox: `readyState 4`, **5.5 s in after 6 s**, not paused, no error, 1280×720, duration 17.9 s. That also closes Phase 6's browser-playback check |
| **Uploads on request** | With the uploader stopped for the test (so no further footage went to AWS): **Upload to AWS** turned the badge to *upload requested* and created the marker; **Cancel upload** turned it back to *local only*. The upload itself was proven in Phase 7 |

### Phase 9: The `pir-` cloud resources (no page yet)

1. `pir-local` table (on-demand, TTL on `ttl`) and the `PirLocal` inline policy on
   `KVSAdapterRole`.
2. Roles `pir-<function>-role` with `cloud/iam/pir-*.json`, one per function. Add their rows,
   and `PirLocal`'s, to the mapping table in `cloud/iam/check-drift.sh`.
3. `cloud/deploy-pir.sh`: zip and update, refusing any function name without the `pir-`
   prefix.
4. The eleven Lambda copies, configured from the live originals (§3.9), plus `pir-control`.
5. REST API `pir-api`: Cognito authorizer on `kvs-demo-users`, the routes from §3.9, CORS,
   stage `prod`.
6. Cognito app client `pir-web` (username/password flow, no secret).
7. **Check:**
   - with a `pir-web` token, every copied route answers. For the same request it returns the
     same body as the shared API, except `/clips`, which has no paging fields;
   - `pir-control` switches `pirRecording` and refuses cameras without `pirTopic`, and its
     log shows the user;
   - `check-drift.sh` is clean, including the new rows;
   - **successor untouched:** `LastModified` of every unprefixed Lambda, the `kvs-demo-api`
     deployment, and the root `index.html` are unchanged.


**Status: done on 2026-10-03.**

**How it was built:** `cloud/pir_stack.py`, an idempotent provisioning tool (boto3, operator
credentials; `--check` only reports). It records the IDs in **`cloud/pir-stack.json`**:

| Resource | ID / name |
|---|---|
| API | **`pir-api`**, `wkq6c6zmb1`, edge-optimised like the shared one, stage `prod`: `https://wkq6c6zmb1.execute-api.eu-central-1.amazonaws.com/prod` |
| App client | **`pir-web`** `284r1pa1n9dgdumg1q65n57kcn` on the shared pool `kvs-demo-users`; username/password flow, no secret, the same token settings as `kvs-demo-web-client` |
| Functions | 11 copies `pir-<name>` (this repository's code; runtime, timeout, memory and environment read from the live originals) + **`pir-control`** |
| Roles | 12 `pir-<name>-role`, each with `AWSLambdaBasicExecutionRole` and **the same inline policy files as the original**. `pir-record-clip` gets its own role; the original shares `ClipToS3LambdaRole` with `clip-to-s3` |
| Table | **`pir-local`**, on-demand, PK `cameraId`, SK `sk`, TTL on `ttl` |
| Device policy | **`PirLocal`** on `KVSAdapterRole`: GetItem/Query/PutItem/UpdateItem on `pir-local`, the one additive change to a shared resource (§4.4) |

- **Routes:** the shared API's eleven, unchanged, plus `GET /pir`, `GET /pir/clips`,
  `POST /pir/mode`, `POST /pir/upload`. All 15 sit behind `CognitoAuthorizer` on the shared
  pool, with mock `OPTIONS` for CORS on every path.
- **One deliberate difference from the shared API:** gateway responses `DEFAULT_4XX` and
  `DEFAULT_5XX` carry CORS headers. An expired token's 401 then reaches the page as a 401
  rather than an opaque CORS failure, the quirk noted for the shared API.
- **`cloud/lambda/pir_control.py`:**
  - the switch: a field-level update of `pirRecording`, written only when it changes, refused
    without `pirTopic`;
  - status and clip-index reads from `pir-local`;
  - upload requests: `uploadRequestedAt`/`requestedBy` on an existing clip row, refused once
    `deletedAt` is set;
  - every write logs `cognito:username`.
- **`cloud/iam/pir-control-policy.json`** limits it with `dynamodb:Attributes`: on `cameras`
  only `cameraId`, `pirRecording`; on `pir-local` only the request fields; `ReturnValues`
  `NONE`/`UPDATED_NEW`.
- **`cloud/deploy-pir.sh`** deploys code to `pir-` names only. It checks every name before
  deploying any, and builds the zip in a temporary directory.

Results:

| Check | Result |
|---|---|
| Idempotency | A second run in `--check` mode: everything `ok` |
| `deploy-pir.sh` | `list-cameras` refused (exit 2); a mixed list refused before deploying anything; `pir-control` deployed |
| **Every copied route, with a `pir-web` token** | The same request to `pir-api` and to the shared API: **identical status and body on all 11** (`GET /cameras`, `/clips`, `/hls`; the 8 writes tested with invalid input only, so nothing was written, started or deleted). `/clips` is identical too: the successor's paging fields appear only when a caller asks for paging |
| No token | `pir-api` 401 **with** CORS; shared API 401 without |
| `pir-control` | `GET /pir` returns the switch and `pirTopic` (status `null` until Phase 10); `GET /pir/clips` empty; **switch on and off for cam‑01, verified in the registry**; cam‑02 refused (no field added); unknown clip 404; bad body 400; **CloudWatch logs the user** (`pir-phase9-test`) |
| **IAM limits, simulated** | `pir-control-role` may update `pirRecording` on `cameras`: allowed. `recordingMode`, or `pirRecording` plus `onvifPassword`: **denied**. The request fields on `pir-local`: allowed; the Pi's clip details: denied; `clips`: denied |
| `check-drift.sh` | 17 new rows (the `pir-` roles reuse the originals' files, plus `PirLocal` and `PirControlAccess`): **no drift** across all 31 |
| **Successor untouched** | Against a baseline taken before provisioning: **all 16 unprefixed functions** have the same `LastModified` and `CodeSha256`; the shared API is still on deployment `5fm7vd`; the root `index.html` has the same ETag. The only differences are the two planned additive items: `PirLocal` on the device role and the app client `pir-web` |

- The token came from a **temporary user** (`pir-phase9-test`, random password, never written
  down) created for the test and **deleted afterwards**; the pool is back to its one user.
- **The page doesn't use `pir-api` yet.** That's Phase 11. Until then, nothing calls the
  copies except these tests.

### Phase 10: Status and the clip index from the Pi

1. Watcher: writes the status item (§3.10) and the index rows with thumbnails (§3.11); sets
   `deletedAt` and `ttl` when retention deletes a clip.
2. Uploader: polls `pir-local` for cloud requests; writes the result fields.
3. Admin app: shows cloud requests in the PIR clips list.
4. **Check:**
   - the status item changes within ~5 s of a wave; with the internet down, `updatedAt`
     stops, and on recovery the item is current again;
   - a request set with `POST /pir/upload` starts an upload within 30 s; the row and the
     admin app both show the result;
   - a request for a deleted clip ends as `uploadError: gone`;
   - deleted clips' rows disappear about a day later (TTL).

**Status: done on 2026-10-03** (the TTL deletion is due on 2026-10-04, see below).

**How it was built:**

- **`adapter/pir_cloud.py`**: the watcher's cloud thread (`CloudSync`):
  - **status item:** written on change, at most once per 5 s per camera, plus every 5 min;
  - **clip index: reconciled, not event-driven.** Every 30 s it builds what each merged clip's
    row should say from the session folders, compares it with a digest of what was last
    written (`~/.local/state/vms/pir-cloud-sync.json`), and writes only the difference. A
    clip that left the stick gets `deletedAt` and `ttl` = now + 1 day, on condition that the
    row still exists. After a start it first adopts any live row it has no record of, so a
    lost state file can't leave a deleted clip's row behind;
  - UpdateItem with explicit fields only; short timeouts; device credentials refreshed every
    30 min; failures logged once per streak.
- **`adapter/pir_watcher.py`:** builds the status from what it already tracks and adds
  `effective`; starts the thread unless `--dry-run`; retention counts `cloud-request.json` as
  pending.
- **`adapter/outage_uploader.py`:** `pir_cloud_pass()` after the outage backfill and the LAN
  requests, with its own error handling: queries `upload-requests`, carries out each request
  (`cloud-request.json`, then the same upload as a LAN request), mirrors every
  `uploaded.json` onto its row, writes `uploadError` for gone or failed clips.
- **Admin app:** `cloudRequest` in `/api/pir-clips`. The clip list shows "uploading — asked
  for in the cloud by …" and, afterwards, "in AWS — asked for in the cloud by …". It refuses
  Cancel and Delete while a cloud request is pending.
- **`cloud/pir_stack.py`** creates the index `upload-requests`. **`PirLocal`** gains `Query`
  on the index ARN (an index is its own IAM resource). No Lambda changed.

Results:

| Check | Result |
|---|---|
| Index rows | All 15 merged clips indexed within 8 s of the watcher's start; thumbnails 2.7–3.8 KB base64; the two clips uploaded in Phase 7 got `uploadedKey` from the first uploader pass |
| Status item | Written on start: `effective: off`, Pico online, broker connected, `sessionsToday: 15` |
| **Change within ~5 s of a wave** | Waves at 20:08–20:11, measured by polling the item once a second: 10 changes caught. From the Pi receiving an event to the item in the cloud: **0.2–4.3 s, median 0.95 s**. Only one was above 2.5 s: a stop that came 2 s after its start and waited out the 5 s gap after the start's write. Events inside one gap are merged (the item keeps the latest), by design. Before that, the Pico delivered the events **1–74 s late**, growing within a burst (`PIR-MQTT-VMS-Pico.md` §4.5) |
| **Internet down** | AWS blocked 19:52:31–19:57:27 (IPv4 and IPv6, safety timer set first). `updatedAt` **stopped** at 19:51:47: the 5-minute heartbeat due at 19:56:47 didn't land, and the watcher's local health showed `ok: false`. Each service logged the outage once. **On recovery** the uploader resumed after 2 s, the watcher after 6 s; `updatedAt` 19:57:32. Motion events that arrived during the block were in the item, and a `keep` ticked during the block reached its row |
| **Cloud request** | `POST /pir/upload` at 19:48:59 by a temporary user: picked up at 19:49:19 (**19 s**), in S3 at 19:49:22 as `164932-pir-s281.mp4`. The row got `uploadedKey`; the `clips` row has `labels` `pir`, `pir:seq 281` and `videoCodec h264`; the admin page shows "in AWS — asked for in the cloud by pir-phase10-test" (rendered in headless Firefox) |
| **Gone** | A disposable copy of a clip, indexed, then deleted with the watcher stopped; `POST /pir/upload` for it: **`uploadError: gone`** 29 s later. The restarted watcher set `deletedAt` and `ttl` (2026-10-04 19:51); a repeated request got **404** |
| TTL | Enabled on `ttl`. The row above expires 2026-10-04 19:51; DynamoDB deletes expired items within a few days, usually much sooner. **Check pending** |
| `check-drift.sh` | No drift, with the extended `PirLocal` |

- The token came from a **temporary user** (`pir-phase10-test`), deleted afterwards.
- **The test upload was deleted afterwards** (2026-10-03 20:14, at the user's request): the S3
  object `clips/cam-01/2026/10/03/164932-pir-s281.mp4` and its `clips` row, the way
  `delete-clip` does it. Its `pir-local` row lost the request and result fields in one update,
  so the uploader saw no new request, and the local `uploaded.json`/`cloud-request.json` went.
  The local clip stays (D10). The four test clips from Phases 5 and 7 remain.

### Phase 11: This project's cloud page (C2)

1. Bucket `pir-client-596633517506`, CloudFront distribution with OAC, cache headers as for
   the existing client (`no-cache, must-revalidate`).
2. The page is a fork of `client/index.html`: `API` = `pir-api`,
   `COGNITO_CLIENT_ID` = `pir-web`, plus:
   - a PIR panel for cameras with `pirTopic`: the switch, the live status with its "unknown"
     state, and the local clip list with thumbnails and Upload to AWS buttons;
   - a "PIR" badge for clips labelled `pir` in the clip list.
3. **Check:**
   - a full regression of the page against live AWS: login, live view on both cameras
     (including H.265 on cam‑02), manual recording, the clip list with play/tier/delete, the
     audio and outage settings;
   - all PIR functions work from the page;
   - **successor untouched:** the root page at `dugyd3kkt36pw.cloudfront.net` is the same
     object as before, and the successor's page lists the PIR clip with the label `pir`.

**Status: done on 2026-10-03.**

**How it was built:**

- **Hosting** (`cloud/pir_stack.py`, recorded in `cloud/pir-stack.json`):

  | Resource | ID / setting |
  |---|---|
  | Bucket | **`pir-client-596633517506`**: Block Public Access on (all four), no website endpoint, bucket policy allowing `s3:GetObject` for this distribution only (`AWS:SourceArn`) |
  | Origin Access Control | `pir-client-oac` `E37HAUIB0AA9T6`, sigv4, always sign |
  | Distribution | **`E53AGX9O0GDTV`**, **`https://d10sy0s307vyid.cloudfront.net`**: settings mirrored from the shared `E1B12167KKII6B` (root object `index.html`, redirect to HTTPS, managed CachingOptimized, compression, PriceClass_100, HTTP/2, IPv6) |

- **Deploy:** `cloud/deploy-pir.sh client`. It refuses a bucket not named `pir-client-…`, and a
  page whose `API` and `COGNITO_CLIENT_ID` aren't `pir-api` and `pir-web` from
  `pir-stack.json`. It uploads with `Cache-Control: no-cache, must-revalidate`.
- **The page is `client/index.html` itself, changed in place.** It is this project's page now;
  the shared bucket's page is the successor's. Changes:
  - `API` = `pir-api`, `COGNITO_CLIENT_ID` = `pir-web`; title "VMS cameras";
  - which cameras get the PIR parts comes from `GET /pir` (`pirTopic`), so `GET /cameras`
    stays a byte-identical copy of the shared `list-cameras`;
  - **PIR motion sensor** section: the switch (`POST /pir/mode`), and the live status from the
    status item: Armed / Off / Not recording (with the reason) / Unknown (a report older than
    10 min) / Switching (the switch and the Pi's view differ). Polled every 10 s while the tab
    is visible;
  - **Motion clips on the Pi:** thumbnail, time, duration, codec, how the motion ended, and
    the state (On the Pi only, Upload requested, In AWS, Failed, Deleted on the Pi), with
    **Upload to AWS**, and **Play** once it is in AWS. "Older clips" pages through
    `GET /pir/clips`. While a request is pending, the list refreshes with the status; when it
    completes, Evidence clips refreshes too;
  - Evidence clips: a **PIR** badge on clips labelled `pir`.
- **Found in testing, fixed:** deleting an uploaded clip's AWS copy left its row "In AWS" with
  a dead Play button. The uploader now checks every 5 minutes (§3.11, "When the AWS copy is
  deleted"), and `PirLocal` gained `s3:ListBucket` on `clips/*`.

Results (headless Firefox through Marionette, signed in on the live page as a temporary user):

| Check | Result |
|---|---|
| Hosting | The served page is byte-identical to the file; HTTP redirects to HTTPS; **reading the bucket directly is refused (403)** |
| Deploy guards | A shared-bucket target and a page on the shared API are both refused (exit 2) |
| Login, panels | Signed in through `pir-web`; cam-01 and cam-02 panels from `GET /cameras`; **the PIR parts appear on cam-01 only** |
| **Live view** | cam-01 (H.264): picture after 15 s, 1280×720, advancing. **cam-02 (H.265): picture after 21 s, 640×360, advancing**, the camera's clock in the frame. This Firefox decodes H.265 (`hevcSupport()`: live and clips). Stop works on both; both producers inactive afterwards |
| Manual recording | 12 s recorded, listed as 16 s `manual-recording`, played, deleted |
| Evidence clips | Listed with badges; play (cam-01 H.264, cam-02 H.265); a tier change to Standard-IA confirmed in S3; delete with the confirm dialog, confirmed in S3 and `clips` |
| Audio, outage settings | Each changed, confirmed in the registry, and set back |
| **PIR status** | Showed "Sensor offline" while the Pico really was: its "offline" message reached the broker at the time shown, and the broker log shows it flapping (connected 20:25, 20:28, 20:30; timed out 20:30, 20:31) |
| **PIR switch** | On: registry at once, ring armed 4 s later, "Switching on" → **Armed** at 69 s. Off: "Not recording — PIR is on, but the Pico is offline" in between (the Pico had just timed out), **Off** at 114 s |
| **Upload from the page** | Upload requested at 0 s → **In AWS at 20 s** (the Pi took 2 s); Evidence clips refreshed by itself with the clip badged PIR; Play from both lists |
| **AWS copy deleted** | An upload from the LAN, its AWS copy deleted the way `delete-clip` does it: **back to "on the Pi only" after 3 min 51 s**; `upload-requested` removed; **no re-upload** on the next pass; the two kept uploads untouched |
| **Successor untouched** | Against a baseline taken before the regression: all 16 unprefixed functions, the shared API's stage (`5fm7vd`), the root `index.html` (ETag `beb1e49e…`), the shared distribution's config and the shared app client: **no difference**. **The successor's page lists the PIR clips with the label `pir`** and has no PIR panel |

- The temporary user `pir-phase11-test` was deleted; the pool is back to one user. The test
  uploads were deleted. PIR, audio and outage buffering are off, as before.
- **Found on the way, not this page:** cam-02's H.265 detection clips are 9 s files listed as
  45 s (`FoundAndFixed.md` #53, open, on the successor's `clip-to-s3` path).

### Phase 12: Ring in RAM, retention under load (D7, D8)

1. The ring for PIR cameras moves to `$XDG_RUNTIME_DIR/vms/ring/`. `recordPath` becomes
   per-camera in the supervisor, which copies instead of linking or renaming across
   filesystems.
2. **Check:**
   - stick writes over 24 h (sectors written, from `/sys/block/sda/stat`) ≈ session footage
     only (§2.4);
   - the ring uses ≈ 24 MB of RAM;
   - Phase 5's outage regression passes again.
3. A one-week soak with retention forced short (one day). It should show deletion working,
   pending requests exempt, and the disk floor never reached.
4. **This phase has to be done before PIR mode is left on unattended (D7).**

**Status: built and checked on 2026-10-03, except step 3: the one-week soak is postponed**
(decided 2026-10-03). Until it has run, PIR mode stays off when nobody is around.

**How it was built** (`adapter/outage_buffer.py`, `adapter/pir_watcher.py`, `adapter/mediamtx_api.py`):

- **Per-camera ring location:** armed for PIR → `$XDG_RUNTIME_DIR/vms/ring/` with
  `recordDeleteAfter: 10m` as a backstop; outage-only → the stick, unchanged. Below 64 MB free
  in the runtime tmpfs the PIR ring falls back to the stick. `set_recording()` and
  `recording_conf_matches()` take the cleaner setting, defaulting to off (§3.6).
- **Both places, everywhere:** `ring_segments()` lists a path's ring in RAM and on the stick,
  chronologically; outage capture, tail, pruning and the PIR sweep all use it.
- **Out of RAM by durable copy:** `.name.part`, fsync, rename, then delete the RAM file.
- **Sessions staged in RAM** (decided 2026-10-03, after the first measurement): RAM-ring
  segments are hard-linked into `$XDG_RUNTIME_DIR/vms/pir/<sessionId>/`; footage already on the
  stick is linked there. The watcher merges from both and removes the staging; the supervisor's
  `tend_pir_staging()` clears leftovers and moves a session unmerged for 10 min to the stick.
- **A disarmed camera's RAM ring is emptied** once its files are 2 min old, the last segment
  included; otherwise ~4 MB would sit in RAM until the next arming.
- **Watcher:** its ring check looks in both places; `PIR_RETENTION_DAYS` overrides the 14-day
  retention, for the soak only.

Results:

| Check | Result |
|---|---|
| Offline, real functions on the RAM tmpfs and the stick | **19 cases pass**: the ring across both places; RAM footage staged as links (same inode, nothing written to the stick); staging surviving an outage capture taking the ring file; later sessions linking captured footage on the stick; the safety net (merged, deleted, unmerged for 10 min); pruning; same-filesystem moves still renames |
| Retention, real `retention()` | Old clips deleted, an uploaded one too (D10); spared: `keep`, a pending LAN request, **a pending cloud request**, an open session, a recent clip |
| **Arming** | `cam-01: recording ARMED (pir) in RAM`; MediaMTX reports `recordPath /run/user/1000/vms/ring/…`, `recordDeleteAfter 10m0s`. Adding the outage reason kept the path ("stays armed, now for outage+pir"): no recorder rebuild |
| **RAM** | Ring **13–15 MB** (three completed segments and the open one); 3.6 MB during the outage (only the open one); **8 KB once disarmed** |
| **Stick writes, segments copied** (first design) | 20 min, 19 sessions (28 % duty cycle): **152 MB**, of which 148 MB accounted: 26 segment copies (104 MB) and the merged clips (44 MB): **3.3×** the kept footage |
| **Stick writes, sessions staged in RAM** | 20 min, 35 sessions (51 % duty cycle): **96.8 MB for 88.4 MB of clips and thumbnails: 1.09×**. The session folders on the stick hold only `clip.mp4`, the thumbnail and two JSON files; clips decode to live footage |
| **Outage regression** (Phase 5's, `OUTAGE.md` §7) | `outageBufferSec` 300, PIR on, `kvs-cam01` uploading. AWS blocked 22:08:58–22:13:41 (both families, safety timer first): outage at 22:09:08, recovered at 22:13:56 after **288 s**. The capture held the 2-min pre-roll from 22:07:55 onwards, every segment copied out of RAM, with no `.part` left. A session during the outage merged from captured pre-roll plus a staged segment. Backfill: 11 segments into **one 338 s, 40 MB clip**. **Gap-fill 99.72 %** (KVS alone 39.84 %); a frame from inside the block decodes to live footage |
| Found on the way | The block missed the IoT endpoint's new range (63.176.0.0/12), and the first attempt "recovered" after 7 s: `FoundAndFixed.md` #54, fixed in `adapter/bin/awsblock.sh` |

- The 24 h stick-write figure (step 2's first check) comes with the soak; the 20-minute
  windows above already separate the ring (nothing) from session footage (1.09×).
- Restored afterwards: `kvs-cam01` stopped, `outageBufferSec` 0, **PIR off**. The regression's
  outage clip was deleted, as after Phase 5's.
- **The soak, when it runs:** PIR on; `PIR_RETENTION_DAYS=1` through a removable drop-in for
  `kvs-pir-watcher`; mark today's test clips `keep` first if they should survive; then a week of
  watching deletions, the disk floor and stick writes per day.

### Phase 13: Hardening and documentation

**Status: open.** The documentation items marked *done* were finished along the way, in the
phase named.

1. Hardening:
   - hardware watchdog on the Pico, also covering "no broker connection for N minutes"
     (`PIR-MQTT-VMS-Pico.md` §4.5, finding 2): *open, firmware*;
   - an alert when `pir-state.json` or the cloud status item goes stale: *open*;
   - optional TLS for the broker, but **not on port 8883**: the Pi's nftables output chain
     drops every connection to 8883 (guide §7.3), which would block the Pi's own clients
     ([B.9](#b9-firewall)): *open*;
   - a one-night soak, comparing clips against the Phase 2 expectations: *open*. It needs
     Phase 2's logs; it is not Phase 12's one-week soak, which tests retention and the RAM
     ring;
   - the cam-01 publisher hanging silently (`FoundAndFixed.md` #51): *open*.
2. VMS documentation:
   - `Camera-Features.md` §9: a new "PIR trigger" phase: *open*;
   - `OUTAGE.md`: the supervisor also arms for PIR, and a PIR camera's ring lives in RAM:
     *done* (Phases 5 and 12). Guide §16.3c: *open*;
   - the guide: the `pir-` cloud resources and this project's page: *open*;
   - LAUNCH.md: the new unit in A8 *done* (Phase 6); starting and checking it in Parts B and
     C *done* (2026-10-03). Still open: `paho-mqtt` into A4's package list, since the watcher
     imports it. Mosquitto and `paho-mqtt` are in A10 (added 2026-10-03);
   - `config/adapter.env.example`: the MQTT keys: *done* (Phase 2);
   - CLAUDE.md: *done*: the Lambda and client deploy sections use `cloud/deploy-pir.sh` and
     the `pir-client` bucket, and the unprefixed resources are the successor's (Phases 9, 11);
     the user-units list has `kvs-pir-watcher` (Phase 6); the "client identical to the
     successor's" rationale (#48) is retired (Phase 11);
   - README: the feature list: *done* (2026-10-03);
   - `FoundAndFixed.md` entries for every defect found: *done* (#51–#55).
3. The successor's CLAUDE.md: the data contract, rules 4 and 5b (§4.3): *open*; it needs
   access to that repository.
4. `blink_freertos`: *open, yours*:
   - `PIR-MQTT-VMS-Pico.md` committed there in place of `PIR-MQTT-VMS.md`, and kept in step
     with this file;
   - MQTT_RASPI_4B_Pico2W.md, README-PIR.md, README.md.

### Dependencies

- Phases 0, 1 and 3–11 are done, and Phase 12 except its soak. Phase 2's measurements are
  postponed and resume with tuning the hold time. Once its logs exist, rerun `replay-pir.py` on them; that is when the replay
  check meets real data.
- Next on the local side: **Phase 12's soak** (one week, retention at one day), the last
  condition for leaving PIR mode on unattended; then Phase 13. The cloud side is complete.
  On the Pico side: the firmware's burst throughput (`PIR-MQTT-VMS-Pico.md` §4.5) and the
  hold-time tuning.
- Phase 5 ran with the ring on the stick; Phase 12 moved it to RAM (D7).
- Phase 10 needs Phase 9's table and policy; Phase 11 needs Phases 9 and 10.
- Phases 9–11 are independent of Phases 4–8 on the AWS side, but the PIR panel only shows real
  data once Phase 6 runs.

---

## 6. Decisions

### 6.1 PIR recording

| # | Decision | Options | Status |
|---|---|---|---|
| D1 | Clip model | A: fixed 45 s · B: follows the motion | **Resolved: B.** A existed only to avoid a `clip_to_s3` change, and local clips need none (§3.4) |
| D2 | What counts as "recording runs" | only an open PIR session · also a manual cloud recording | **Decided 2026-10-03: PIR sessions only** |
| D3 | Where PIR footage comes from | (a) keep `kvs-cam01` running, ingest cost 24/7 · (b) PIR starts the producer: pre-roll lost plus KVS spin-up · (c) a local ring buffer | **Decided 2026-10-03: (c)**, a ring buffer on the Pi as outage buffering uses; selected clips are copied to AWS |
| D4 | Presence longer than `MAX_SEC`, or a return during POST_ROLL that continues past the session's end ([A.4](#a4-coupling-with-ignore-while-recording-runs)) | close the session · open a continuation session while `pir/state` still reports motion | **Open.** Decide after Phase 2 data; A.4 is the strongest argument for continuation |
| D5 | Local GUI scope | admin-app panel only · plus QML kiosk | Recommendation: admin panel first. The cloud page's PIR panel is settled separately (extra 1) |
| D6 | AWS path of the Pico | keep the direct TLS telemetry path · a Mosquitto bridge on the Pi | **Decided 2026-10-03: keep direct** for now |
| D7 | Where the ring lives in PIR mode | on the stick (~69–87 full-drive writes a year) · in RAM (≈ 24 MB) | **Decided 2026-10-03: RAM before PIR mode is left on unattended**; the stick for bring-up. **Built in Phase 12**, with session footage staged in RAM as well (decided 2026-10-03) |
| D8 | Local clip retention | by age · by free space · user pin | **Decided 2026-10-03: 14 days, oldest unkept first as the disk floor nears, `keep` exempt.** Pending upload requests are exempt from the age rule (§3.6) |
| D9 | Where clips are selected for upload | the admin app · also the cloud page | **Decided 2026-10-03: both** (§3.7, §3.11) |
| D10 | Does an upload copy or move the clip | copy · move | **Decided 2026-10-03: copy** |
| D11 | Where live status and the clip index live | `cameras` row · own table | **Decided 2026-10-03: own table `pir-local`** |
| D12 | Login client for this project's page | the shared app client · own app client | **Decided 2026-10-03: own app client `pir-web`** |
| D13 | Thumbnails for choosing clips remotely | none · in the index row | **Decided 2026-10-03: in the index row** (1–3 frames, 160×90) |

### 6.2 Remote control and the successor

| Topic | Choice (2026-10-03) | Alternatives not taken |
|---|---|---|
| Where the PIR switch is stored | **A2:** `pirRecording` on the `cameras` row; `recordingMode` untouched | A1: a `recordingMode` value `pir`, which would reach the successor |
| How the cloud changes it | **B3:** `pir-control` behind this project's own API `pir-api` | B1: extend shared Lambdas (overwritten by the next successor deploy) · B2: new routes on the shared API (its stage deployment publishes the successor's pending edits) · B4: MQTT command to the Pi |
| Where this project's page lives | **C2:** own bucket and CloudFront distribution | C1: a path in the shared bucket · C3: one shared page behind a flag |
| The Lambdas the page needs | **Option (b):** own `pir-` copies | (a): rely on a contract that the successor keeps serving non-ONVIF cameras |
| Name prefix | **`pir-`** for every resource only this repository deploys | `vms-`, already used by shared buckets |
| `videoCodec` on uploaded clips | **Added** to PIR and outage rows | |
| Extra 1: live PIR status in the cloud | **Accepted** (§3.10) | |
| Extra 2: cloud requests to upload local clips | **Accepted** (§3.11) | |
| Extra 3: restricting PIR control to a Cognito group | **Postponed.** `pir-control` logs the user meanwhile | |

---

## 7. Consistency record: what earlier plans said and what changed

### Recorded in `blink_freertos` on 2026-10-02

| Earlier statement | Status now | Reason |
|---|---|---|
| Motion events to AWS IoT Core on `…/motion`, stored in DynamoDB, served by an API, web UI polling every 2 s (AWS plan) | **Superseded** for motion | Latency (seconds) and cloud-only access; the local path is ~5–30 ms |
| `motion` daemon + `pir-camera.service` drive the USB camera (local plan) | **Dropped** | VMS/MediaMTX owns the camera; the daemon would also clash with onvif-admin on port 8080 |
| Web GUI served by Mosquitto (`http_dir`, WebSockets 9001), SQLite recorder, retained `pir/history` (local plan) | **Dropped** | The VMS admin app is the local GUI; VMS's `clips` table is the recording history |
| Topic contract `status` / `pir/event` / `pir/state` / `pir/cmd` | **Kept** | `pir/history` and the camera topics were removed |
| Pico `lan_mqtt_task`, `lwipopts.h` changes, hook timestamps | **Kept** | Phase 1 |
| MQTT_RASPI_4B_Pico2W.md: coreMQTT over sockets with `LWIP_SOCKET=1` | **Not applicable** | The project already uses lwIP `apps/mqtt`; sockets are disabled |
| MQTT_RASPI_4B_Pico2W.md: ISR via `gpio_set_irq_enabled_with_callback()`, edge type from the event mask | **Not applicable** | `pir.c` uses a raw per-pin handler next to the CYW43 handler, and acts on the sampled level |
| MQTT_RASPI_4B_Pico2W.md: "turn off Wi‑Fi power save on the Pico" | **Already done** | `wifi_task.c:54` |
| MQTT_RASPI_4B_Pico2W.md: "SNTP optional" | **Refined** | SNTP exists, but the local path must not depend on it (§2.1) |
| "Port 9001 is free; MediaMTX uses 8554/8888/9997" | **Corrected** | MediaMTX also uses 8889, 1935, 8890; 1883 and 9001 are still free |
| "Check GetClip's limits before raising MAX_SEC" | Resolved then; **no longer applies** (below) | |

### First revision, 2026-10-03: local recording

| Earlier statement | Status now | Reason |
|---|---|---|
| File `PIR-MQTT-VMS.md`; "this repository" meant `blink_freertos` | **Renamed** `PIR-MQTT-VMS-PI4.md`; "this repository" now means VMS | The copy lives in VMS and is authoritative for the Pi side |
| D3 recommendation (a): keep `kvs-cam01` running in PIR mode | **Superseded** by (c), a local ring buffer | Decision. Option (a) contradicted the project's rule against unattended producers (guide §1.2) |
| A PIR clip is one publish to `adapter/<thing>/event`, cut from KVS by `clip_to_s3.py` | **Superseded** for PIR | Clips are cut locally from the ring (§3.6); AWS sees only uploaded clips |
| Producer check: no clip unless `kvs-cam01` is active | **Replaced** by a ring check (§3.3) | MediaMTX records cam‑01 without the producer |
| `PUBLISH_DELAY_SEC`-style publish wait; `INGEST_MARGIN_SEC ≈ 5 s` | **Replaced** by waiting for segment capture (≤ ~35 s) | KVS ingest is no longer involved |
| Variant A (fixed 45 s) vs B (`endTimestamp` in `clip_to_s3.py`); "variable-length clips" phase | **Dropped**; the window follows the motion with no Lambda change (D1) | Local clips involve no Lambda |
| MAX_SEC = 180 s sized against GetClip's 100 MB / 200 fragments | **Kept**, now as a product cap (§3.4) | GetClip isn't on the PIR path |
| Move `publish_event()` out of `event_watcher.py` into a shared `clip_trigger.py` | **Dropped** | PIR no longer publishes to the event topic, so nothing is shared |
| ACL: `gui` writes `…/pir/cmd/#`, `vms` read-only | **Changed:** `vms` writes the command topic, `gui` is read-only | The admin app, which sends the command, connects as `vms` |
| Phase 0 check: `mosquitto_sub` / `mosquitto_pub` without credentials | **Fixed:** `-u`/`-P` on both, plus negative checks | Mosquitto 2.x refuses anonymous clients once a password file is in use |
| Fixed IPv4 for the boards: listed like an option | **Required** | The broker IP is compiled into the Pico |
| Timestamps: Pi receive time by default; the Pico's `ts` if more than 2 s older | **Replaced** by ageing every event with `boot_ms`; drop beyond `STALE_SEC` = 60 s, otherwise place at the event time (§3.5) | Decision. It doesn't depend on SNTP on the Pico, and the threshold follows from what the ring still holds |
| `pir/state` = `{"motion","since_boot_ms","count"}`, published on connect and after events | **Changed:** `changed_ms` and `boot_ms` (uptime at publish), heartbeat every 30 s, published before the queue drains | §3.5 needs a fresh clock reference. Carried into `PIR-MQTT-VMS-Pico.md` |
| "`config.py` reads all keys at import time" | **Corrected:** six identity keys at import; `config.get()` is lazy | The rule (read MQTT keys lazily) stands |
| "Known limitation: a watcher restart mid-session loses that session's clip" | **Resolved** by journals on the stick plus the persistent session (§3.3) | |
| (new) Watcher acts before NTP sync | **Prevented:** it waits for `NTPSynchronized=yes` | The Pi has no RTC (observed 2026-10-03) |
| (new) Ungated recording on the USB stick | **Decided** as D7: RAM before unattended use | `OUTAGE.md` §3.5 gated it away for wear reasons |

### Second revision, 2026-10-03: remote control, the `pir-` resources, the successor

| Earlier statement (first revision or proposals) | Status now | Reason |
|---|---|---|
| `recordingMode` gains the value `pir` | **Replaced** by the switch `pirRecording` (A2) | A new enum value would reach the successor's code; a new field doesn't (§4.2) |
| PIR mode set LAN-side only; the cloud client keeps showing cam‑01 as "manual only" | **Superseded:** set from the admin app and from this project's own page through `pir-control` | Remote control requested; available only in this project |
| "The PIR feature needs no cloud deploy at all"; "nothing in the phases deploys to AWS" | **Replaced** by "no shared resource is modified; new resources are `pir-` only" (§3.9, §5) | Remote control needs cloud resources; isolation comes from the namespace, not from avoiding AWS |
| D9: select uploads in the admin app only | **Changed:** both GUIs (§3.11) | Decision |
| Proposal: live status in cam‑01's `cameras` row, needing no new grant | **Changed:** in the table `pir-local` (D11) | Status isn't camera configuration, and the clip index needs the table and grant anyway |
| Proposal: names with a `vms-` prefix | **Changed:** `pir-` | The shared buckets already use `vms-` |
| This project's page uses the shared Lambdas (option (a), a contract) | **Changed:** own `pir-` copies (option (b)) | An ONVIF-only successor may change the shared Lambdas; the page must not depend on them |
| Proposal: page at `/vms/` in the shared bucket (C1) | **Changed:** own bucket and distribution (C2) | Decision; immune to anything done to the shared bucket |
| Outage uploads omit `videoCodec` | **Changed:** PIR and outage rows carry it (§4.3 rule 3) | Every other row in the shared table has it |
| `boot_ms` as produced by the Pico's ISR (`to_ms_since_boot()`) | **Changed:** 64-bit in payloads, widened by the Pico's LAN task | Found re-checking `blink_freertos`: the 32-bit counter wraps after 49.7 days, which §3.5 would read as a reboot |
| `pir/state` published "from `pir_get_status()`" on connect | **Refined:** a warm-up form while the sensor warms up, plus an optional `dropped` counter | `pir_get_status()` returns false for the first 30 s after boot |
| "The `blink_freertos` copy must be aligned" | **Done:** `PIR-MQTT-VMS-Pico.md`, prepared 2026-10-03 | It covers the firmware and the MQTT interface |
| CLAUDE.md: camera controls stay out of the cloud client to keep `client/index.html` identical to the successor's (#48) | **Ends with Phase 11:** this project's page becomes its own fork on its own bucket | The two pages are deliberately different now; CLAUDE.md is updated in Phase 13 |

### Phase 0 carried out, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| Phase 0 step 3 listed the Mosquitto setup inline | **Moved** to Appendix B (the full manual) and `LAUNCH.md` A10 (the short form) | The real setup needed more than the outline: file ownership, the `mosquitto_passwd` warning, pasting heredocs whole |
| "Make the password file readable by the `mosquitto` user only" | **Made precise:** `passwd` and `acl` owned by `mosquitto:mosquitto`, mode 600. `mosquitto_passwd` warns that the owner isn't root, and that warning is ignored | The broker reads the files as `mosquitto` and warns otherwise; the tool runs as root (B.3) |
| Hardening: "optional TLS on 8883" | **Changed:** TLS, if added, must use another port | The nftables output chain drops connections to 8883 (guide §7.3), including the Pi's own clients (B.9) |
| "Mosquitto 1883 … free" | **Now used** by Mosquitto, on all interfaces, LAN only | Phase 0 step 3 |
| "Install `paho-mqtt` (or `aiomqtt`)" | **`paho-mqtt` 2.1.0** installed; new code uses the 2.x callback API | Phase 0 step 5 |
| DHCP reservations "required" | **Done** on the FRITZ!Box; the Pi always receives `192.168.178.53` | Phase 0 step 1 |
| "Phase 13 adds Mosquitto and `paho-mqtt` to LAUNCH.md Part A" | **Done early,** as the optional step A10 | A rebuilt Pi shouldn't depend on this plan to get them |
| Phase 0 check, from "another LAN host" | **Done from the Pi itself** via its LAN address; a second machine is still untried | The Pico's first connection in Phase 1 will be that test |

### Phase 2 tooling, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.5: the reference "is reset when `boot_ms` goes backwards" | **Corrected:** only `pir/state`'s `boot_ms` signals a reboot; events are assigned to the boot whose first state preceded them | Found by testing the observer on a synthetic log: a late event legitimately carries an older `boot_ms`, and the old rule turned 2 boots into 4 and invented a lost event. The watcher would have reset its reference on every late event |
| Phase 2 logger: "a `mosquitto_sub -v -F` line format or a 20-line script is enough" | **Replaced** by `adapter/observe_pir.py`, a harness with heartbeats and analysis | The numbers Phase 2 must produce (lateness, lost events, gaps, false-positive clusters, duty cycle) need the analysis, and the heartbeats are what make a quiet night a measurement (`observe_events.py`'s lesson) |
| The watcher uses a persistent MQTT session (§3.8) | **Kept for the watcher; the observer uses a clean one** | The watcher must act on events that arrived while it was down; the observer must not replay days of queued events into a new log |
| MQTT keys in `adapter.env`: "from Phase 6" | **Added now** to `/etc/adapter/adapter.env` and `config/adapter.env.example` | The Phase 2 logger is their first reader |
| B.7 password file command: `sudo sh -c 'printf … "<vms-password>" …'` | **Replaced** by `read -s` piped into `sudo sh -c 'umask 077; cat > …'` | The old form saved the password in shell history and briefly created the file world-readable |

### Phase 4, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.5's settle rule: "events are dropped as un-ageable until a message received ≥ 5 s after the (re)connect" | **Kept for the watcher, not applied when replaying `observe_pir.py` logs** | Those logs come from a clean session and hold no backlog. Applying the rule dropped both motions of the first real capture |
| §3.3: "stop(N) … POST_ROLL"; other events "IGNORED" | **Made precise in code:** a stop with no open session counts as a *stray stop*; a stop for another `seq`, or a second stop, as an *ignored stop*; `offline` closes at whichever comes first, the offline time or the session's own deadline | Every outcome is counted, so a replay explains where each event went |
| Appendix A.4: a return during POST_ROLL is ignored | **Demonstrated on real data:** with T_hold 0.4 s and POST_SEC 5, the second of two motions 3.1 s apart was ignored | It turns the hold-time tuning (Phase 2, step 0) into a prerequisite for any meaningful POST_SEC |
| `observe_pir.py`'s private log reader `_load()` | **Public as `load_log()`**, shared with `replay-pir.py` | One tolerant reader instead of two copies |

### Phase 5, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.6: `sweep.json` = `{"through": …}` | **Extended:** `through`, `coverageStart`, `segments`, `complete`, `cappedBySupervisor`; the journal format fixed too (§3.6) | Phase 6 needs a definite contract, and `complete` saves the watcher re-deriving it |
| §3.6: the supervisor links segments out of the ring | **Refined:** it also searches outage captures, runs before the outage sweep, and caps never-closed journals | Found designing the outage interplay: an outage moves segments out of `live/` within a tick |
| `want = {outage} ∪ {pir}` | **Plus two guards** the naive union would have broken: outage capture only for outage-armed cameras, and pruning continues for PIR-only cameras during an outage | `outageBufferSec = 0` reads as "no limit" in `Capture`; a PIR-only ring wasn't pruned while a capture ran |
| (new) cam‑01's publisher | **Hung silently since boot** (`FoundAndFixed.md` #51, open) | Found before the live test: `bytesReceived` frozen, `active`, no error |
| (assumed) two programs share `cameras-cache.json` | **Not so:** the supervisor's is `~/.local/state/vms/`, `sync_mediamtx_paths.py`'s is `~/.local/state/vms-adapter/` | Checked before changing the supervisor's cache format; no collision |

### Phase 6, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| Phase 6: the watcher "waits for `NTPSynchronized=yes` before acting on any event" | **Bounded:** it waits at most 5 min, then carries on with the saved clock, and treats a later clock step as a jump (open sessions close at the last good time, clock references reset) | An indefinite wait contradicts "with the internet unplugged, local clips keep coming": without internet (and without the optional FRITZ!Box time server) NTP never syncs. Offline, event times and segment names are both on the Pi's clock, so they stay consistent; only the step needs handling |
| §3.8: "the watcher reuses the supervisor's cached registry" | **Done, after extending the cache with `pirTopic`** | The cache held only `outageBufferSec` and `pirRecording` |
| §3.6: the supervisor stops linking once a session is complete | **Also across a restart:** it skips sessions whose journal says `merged`/`failed` or whose `sweep.json` says `complete` | After a restart it would otherwise recreate the segment folder of a session the watcher had already merged |
| §3.6 journal: `from`, `to`, `cameraId` | **Plus the watcher's own fields:** `seq`, `t0`, `status` (recording, post-roll, closed, merged, failed), `end` in post-roll, `reason`, and after merging `clip`, `thumbnails`, `durationSec`, `window`, `videoCodec`, `sizeBytes` | A restart resumes from them, and Phase 7/8 read them |
| §3.6: "merge with `outage_uploader.merge()`, extended with in/out points" | **Done:** `merge(…, inpoint=, outpoint=)`, existing calls unchanged | |
| (new) The real Pico | **Back after 35 min without network** (loose wiring, fixed); no reboot; one more drop 48 s after reconnecting | Seen when the real service connected |
| §3.5's late-event handling, so far only simulated | **Exercised on real data:** events arriving 5–17 s late were placed at their real times | The Pico's slow publishing in bursts (`PIR-MQTT-VMS-Pico.md`) |

### Phase 7, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.7: key `clips/cam-01/YYYY/MM/DD/HHMMSS-pir.mp4` | **Changed** to `HHMMSS-pir-s<seq>.mp4` | Two PIR sessions can start in the same second; the outage key's second resolution would let one upload overwrite another |
| `upload_and_register()` returns `True`/`False` | **Returns the S3 key or `None`**; callers that test truthiness are unchanged | `uploaded.json` records the key |
| Outage rows without `videoCodec` (§4.3, rule 3) | **Fixed:** every upload probes the clip; verified on a real backfill (head and tail) | The data contract asks every writer to set it |
| (new) Wi‑Fi drops | **Recorded as `FoundAndFixed.md` #52, open** | Seen twice during today's tests, delaying recovery by minutes |

### Phase 8, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| Phase 8: the panel shows "recent events" | **Per Pico, even while PIR is off**, with lateness from a display-only clock reference | A wave should show up whether or not recording is on; lateness makes the Pico's burst lag visible |
| The retrigger toggle shows the module's mode | **Shows the last command seen**, from any sender; the watcher subscribes to `pir/cmd/retrigger` | The Pico doesn't report its mode, and a reboot resets it to retriggerable |
| §3.6 one writer per file: the admin app writes `keep` and `upload-requested` | **Kept, plus deleting whole sessions**, refused while open, assembling or with an upload pending | So it can't remove a folder the watcher or the uploader is using |
| Verifying the page: "open it in a browser" | **Headless Firefox via Marionette** (stdlib Python) | Headless Chromium renders blank on this Pi; Marionette gives the rendered text, screenshots and media-element state |

### Phase 9, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.9: create the resources (the plan named no tool) | **`cloud/pir_stack.py`**, idempotent, IDs in `cloud/pir-stack.json` | Repeatable and reviewable, instead of a page of one-off CLI calls; README's "rebuilding on another account" needs it |
| §3.9: roles "mirroring the existing per-function policies" | **The same files**, mapped twice in `check-drift.sh` | A copy's permissions must equal the original's; one file can't drift from itself |
| §3.9: `pir-api` mirrors the shared API | **Plus CORS on gateway error responses** (`DEFAULT_4XX/5XX`) | The shared API's expired-token 401 shows up as a CORS failure in a browser |
| §3.9: `pir-control` "may update only `pirRecording`" | **Enforced by IAM** (`dynamodb:Attributes`, `ReturnValues`) and **proven by simulation**, since `check-drift.sh` ignores conditions | A code bug can't widen what the role allows |
| Testing with "a `pir-web` token" | **A temporary pool user,** deleted afterwards | No one's real password needed to pass through the session |

### Phase 10, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.11: the uploader polls `pir-local` for rows with a request | **Polls the sparse index `upload-requests`**; `PirLocal` gains `Query` on its ARN | A query of the table reads every row, thumbnails included: ~30 MB per poll at a few thousand rows |
| §3.11: the admin app reads the index row with the device credentials | **Reads the uploader's local record** `cloud-request.json`; `uploaded.json` gains `via` and `requestedBy` | The LAN page stays free of AWS calls and keeps working offline |
| §3.10: offline, writes are dropped | **The index is reconciled** against a record of what was written; the status keeps its latest value | A Wi‑Fi drop (#52) or restart loses nothing; the next good pass catches up |
| §3.11: the uploader writes result fields for cloud requests | **For every upload**, LAN ones included (`indexRowUpdated`) | Both GUIs show the same state |
| §3.11 index row fields | **Plus `seq`, `window`, `reason`** | Additive; the page can show why a clip ended |
| §3.6: the uploader writes `uploaded.json` | **Plus `cloud-request.json`** | One writer per file |

### Phase 11, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| Phase 11: "a fork of `client/index.html`" | **`client/index.html` changed in place**; the shared bucket's page is the successor's | This repository deploys only to its own bucket now, so a second copy of the page would only drift |
| The PIR panel "for cameras with `pirTopic`" | **Found through `GET /pir`**, not `GET /cameras` | `pir-list-cameras` stays an identical copy of the shared function |
| §3.9: the bucket and distribution | **Private from the start** (Block Public Access, OAC-only policy), deployed through `deploy-pir.sh client` with two guards | The shared bucket is still public pending its cutover; #48 made from this side impossible for the page too |
| D10: deleting in a cloud clip list doesn't touch the local copy | **Kept, and the clip becomes uploadable again** within 5 minutes; the uploader clears both request markers then | Found in testing: the row stayed "In AWS" with a dead Play button. Decided 2026-10-03, check every 5 min |
| §3.11: `PirLocal` = DynamoDB on `pir-local` | **Plus `s3:ListBucket` on `clips/*`** | To tell a deleted key (404) from a forbidden one; HEAD can't without it |
| Phase 11 check: H.265 live "including H.265 on cam‑02" | **Verified in headless Firefox on the Pi**, which decodes H.265 | No Windows browser needed for this check |

### Phase 12, 2026-10-03

| Earlier statement | Status now | Reason |
|---|---|---|
| Phase 12: the supervisor "copies instead of linking" out of the RAM ring | **Sessions are staged in RAM as links**; only outage captures copy | Copying measured 3.3× the kept footage in stick writes; staging 1.09×. Decided 2026-10-03, accepting that a session not yet merged is lost on a reboot |
| `recordDeleteAfter: 0s` permanently | **Kept for the stick ring; the RAM ring gets 10 min** as a backstop | A dead supervisor would otherwise fill the 374 MB runtime tmpfs in ~40 min; 10 min can't race a capture |
| "The ring for PIR cameras moves to RAM" | **Armed for PIR (`pir` or `outage+pir`) → RAM; outage-only stays on the stick**; RAM short → the stick | The producer gate already bounds outage-only writes |
| §2.4: the ring needs ≈ 24 MB | **13–15 MB measured** at 1.07 Mbit/s | The estimate assumed audio on (1.26 Mbit/s) and a full extra segment |
| Phase 12 step 3: the one-week soak | **Postponed** (decided 2026-10-03) | PIR mode stays off unattended until it runs |
| Outage tests block AWS's usual ranges | **Plus 63.176.0.0/12** | The IoT endpoint moved there; the probe saw AWS as reachable (#54) |

### Paging and the admin player, 2026-10-03 (after Phase 12)

| Earlier statement | Status now | Reason |
|---|---|---|
| §3.9: `pir-list-clips` without the successor's paging; "this page doesn't use it" | **Ported line for line**; both pages' Evidence clips page by 10 | Asked for: paging like the successor's, step 10, with the clip count shown |
| `GET /pir/clips`: 50 per request with a `next` cursor; "Older clips" button | **Page numbers** (`page`, `pages`, `total`, `deleted`), the same pager | One pager for every list, with a jump to any page |
| Admin app: the PIR clip list showed up to 200 clips | **10 per page**, the same pager, server-side (`/api/pir-clips?page=&limit=`) | Same look and step on the LAN page |
| The successor's pager: page numbers as raised buttons, hidden on a single page | **Page numbers as links**; the count shows even on one page | Asked for |
| Admin player: closed only by pressing Play again | **"Stop & close player"** under the video, one player at a time | Asked for, as on the cloud page |
| Admin status poll checked the camera in DynamoDB | **Against the supervisor's local registry cache** | A DNS failure made each poll hang ~36 s and froze the page (`FoundAndFixed.md` #55) |

---

## 8. Verified facts and sources

**This repository (VMS)**, checked 2026-10-03

- `adapter/event_watcher.py`: `ClipGate`, `PUBLISH_DELAY_SEC = 38`, `COOLDOWN_SEC = 60`,
  `REGISTRY_POLL_SEC = 60`; acts only on modes in `MODE_TOPICS` with `onvifHost`.
- `cloud/lambda/clip_to_s3.py`: window ts − 12 s … ts + 33 s (untouched by this plan).
- `cloud/iot/clip-to-s3-rule.json`: `SELECT * FROM 'adapter/+/event'`.
- `cloud/lambda/set_camera_mode.py`: `VALID_MODES`, non-manual gated on `onvifHost`;
  `adapter/onvif-admin/app.py:516–542`: the same gating in the admin app.
- `cloud/lambda/list_cameras.py`: returns a fixed set of fields per camera, including
  `supportsDetection`; `client/index.html:405–415`: the mode select is disabled without it.
- `cloud/lambda/publish_cmd.py`: accepts only `start`, `stop`, `ir`.
- `adapter/onvif-admin/app.py`: existing camera rows are written with `update_item` only; the
  one `put_item` (line 427) creates a new ONVIF camera.
- `adapter/outage_buffer.py`:
  - `PREROLL_SEC = 120`, `SEGMENT_DURATION = "30s"`, `TICK_SEC = 5`;
  - arming = `outageBufferSec > 0` ∧ producer active ∧ `buffer_ready()` ∧ `disk_ok()`;
  - `completed_segments()`; `segment_start()` reads local time;
  - `Capture.sweep()` renames out of `live/`;
  - disk floor = max(4 GB, 15 %).
- `adapter/outage_uploader.py`: `merge()` with mandatory AAC; `upload_and_register()`
  writes key `clips/<cam>/…-outage.mp4` with a conditional `put_item` and no `videoCodec`.
- `cloud/iam/outage-backfill-policy.json`: `s3:PutObject`/`GetObject` on `clips/*`,
  `dynamodb:PutItem` on `clips`. `cloud/iam/check-drift.sh`: no drift; it maps each file to
  an explicitly named role policy.
- `cloud/iam/kvs-producer-policy.json`: `iot:Publish` on `topic/adapter/adapter-01/event`.
- `adapter/config.py`: six keys read at import, `ConfigError` if missing; `get()` is lazy.
- `adapter/camera_control.py`: `get_stream_status()`, `unit_name()`,
  `mediamtx_path_name()`.
- `adapter/bin/publish-cam01.sh` and `/etc/adapter/cameras/cam01.env`: `CAM_FPS_OUT=15`,
  `VIDEO_BITRATE=1000000`, `GOP=30`.
- `mediamtx/mediamtx.yml`: ports.
- `OUTAGE.md` §2, §3.3–§3.5, §4.2–§4.7; `FoundAndFixed.md` #48; `COSTS-1.4.md` §7.2;
  `Camera-Features.md` §9.

**Live AWS**, 2026-10-03

- Lambdas: 11 of 12 shared functions byte-identical to this repository; `list-clips`
  differs (successor's paging; still returns `labels` and `videoCodec`).
- Client bucket: the root `index.html` is 50,599 bytes, deployed 2026-09-28 17:11 UTC (the
  successor's); this repository's is 45,664 bytes. CloudFront `E1B12167KKII6B` has only the
  default root object; the bucket policy lets it read `bucket/*`.
- API `kvs-demo-api` (`svfvg7xh9g`): preflight `Access-Control-Allow-Origin: *`; gateway
  error responses carry no CORS headers. Authorizer `CognitoAuthorizer` on pool
  `eu-central-1_jiAd6UD3S` (`kvs-demo-users`).
- App client `1r0n2cor1pgqvahfogs6cfno7s`: `ALLOW_USER_PASSWORD_AUTH`,
  `ALLOW_REFRESH_TOKEN_AUTH`, no secret, no callback URLs.
- `play-clip` accepts any key starting with `clips/`. The live page's `clipCodecHtml()`
  renders a missing `videoCodec` as nothing.
- `clips` table: an `outage-buffer` row from 2026-09-19 with the outage schema; every row
  has `videoCodec`. S3 lifecycle `tier-down` on `clips/`.
- `cameras` and `clips` tables: `PAY_PER_REQUEST`. Only `cam-01` and `cam-02` rows; one IoT
  Thing for cameras, `adapter-01`.
- Device role `KVSAdapterRole`: inline `KVSProducer` (609 chars) and `OutageBackfill`
  (472 chars); limit 10,240. IoT policy: publish/subscribe/receive on
  `adapter/${iot:Connection.Thing.ThingName}/*`.
- No resource named `pir-…` exists (Lambda, table, bucket, API).

**The Pi itself**, 2026-10-03

- Debian 13 (trixie); `wlan0` 192.168.178.53 by DHCP (864000 s lease); `eth0` no carrier.
- NetworkManager `802-11-wireless.powersave = disable`; `iw` power save off.
- `mosquitto` and `mosquitto-clients` 2.0.21-1 installed and configured the same day
  (Phase 0, Appendix B): listening on `0.0.0.0:1883` and `[::]:1883`; 8883 and 9001 free.
- `paho-mqtt` 2.1.0 installed in `venv-adapter` the same day; `aiomqtt` not installed.
- `/etc/adapter/adapter.env` carries `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER` and
  `MQTT_PASSWORD_FILE` since 2026-10-03; both readers (`config.py`, `adapter-config.sh`)
  still load. `/etc/adapter/mqtt-vms.password` exists (`root:vladimir`, 640, plaintext), and
  `observe_pir.py` logs in with it as `vms`.
- `adapter/observe_pir.py`: capture tested against a throwaway broker on `127.0.0.1:18830`
  (retained replays flagged, duplicates kept, non-JSON kept as text, binary as hex, the
  command logged, SIGTERM closes cleanly); analysis tested against a synthetic 20-hour log
  with known answers (Phase 2).
- The `pir-` cloud resources (Phase 9): all 11 copied routes answer identically to the shared
  API; `pir-control` switches the registry and logs the user; its IAM limits proven by
  simulation; `check-drift.sh` clean on 31 rows; the successor's functions, API deployment and
  page unchanged against a pre-provisioning baseline.
- The RAM ring (Phase 12): `/run/user/1000` is a 374 MB tmpfs; MediaMTX (a user unit, same
  uid) records into `$XDG_RUNTIME_DIR/vms/ring/` and reports `recordDeleteAfter` back as `10m0s`.
  cam‑01's 30 s segments are 3.75–4.0 MB (1.0–1.07 Mbit/s, audio off). The IoT data endpoint
  resolves to 63.179.34.254 (and `2a05:d014:…`); `kinesisvideo` and `dynamodb` have no AAAA
  record.
- This project's page (Phase 11): `https://d10sy0s307vyid.cloudfront.net` (distribution
  `E53AGX9O0GDTV`, OAC `E37HAUIB0AA9T6`, bucket `pir-client-596633517506`), `Deployed` within
  minutes of creation. The full page regression passed against live AWS, including
  cam-02's H.265 live view. Headless Firefox on this Pi reports and delivers H.265 decoding
  (MSE and `<video>`). WebDriver scripts see the page's functions and DOM but not its
  top-level `let`/`const` variables.
- Status and clip index (Phase 10): index rows for every merged clip; a cloud request picked up
  19 s after it was made; `uploadError: gone` for a deleted clip; `deletedAt` and `ttl` set;
  with AWS blocked, `updatedAt` stopped and caught up within 6 s of recovery. Creating the
  index `upload-requests` took about 8 minutes, although the table held 16 rows. A query
  of the whole table (15 rows) consumed 6.5 read units, about 3.5 KB per row. The first
  status write came 8 s after the watcher started (credential vend included).
- The admin page's sections 5–6 (Phase 8): all endpoints on live data; rendered and clicked in
  headless Firefox via Marionette; a clip played (`readyState 4`, advancing); Upload/Cancel
  round trip with the uploader paused.
- `adapter/outage_uploader.py` (Phase 7): a requested PIR clip uploaded, listed by the shared
  `list-clips` with `videoCodec`, fetched byte-identical via `play-clip`; no duplicate after a
  simulated crash; a request made during a live outage uploaded after recovery; outage
  backfill rows carry `videoCodec`.
- `adapter/pir_watcher.py` (Phase 6): dry run, a real clip from real footage (26.8 s for a
  25.4 s window, ready 25 s after its end), kill-and-restart mid-session (clip end follows
  the real stop), retention by age and by disk floor; unit `kvs-pir-watcher` enabled.
- `adapter/outage_buffer.py` with PIR arming (Phase 5): offline simulation of outage + PIR
  sharing segments passed; live arming, the ring, a hand-written session (120.0 s of
  decodable H.264) and disarming verified on the Pi; the live outage regression passed
  (16:11–16:22: the 152 s KVS gap fully covered by the backfilled 304 s clip, PIR copies
  surviving the capture and its deletion).
- `adapter/pir_session.py`: its 11 scenario checks pass. `adapter/bin/replay-pir.py`:
  replayed the synthetic log and the real 70 s capture (Phase 4).
- `nftables` active: no input rules; the output chain drops `tcp dport 8883` (guide §7.3).
- No `/dev/rtc*`. Boot 08:02:19; `systemd-timesyncd` initial sync 08:03:07, with the clock
  reading 22:11 of the previous evening before it.
- `/mnt/vms-buffer`: 57 GB ext4, mounted. RAM 3.7 GB, 2.1 GB available. `Linger=yes`.

**`blink_freertos`** (GitHub `vizdr/RasPi_Pico2W_FreeRTOS_AWS`), re-checked 2026-10-03
against `main` as pushed 2026-10-02 09:04 UTC; the full list is in `PIR-MQTT-VMS-Pico.md` §7

- `pir.c/h`, `pir_task.c/h`: driver/service split; hook signatures; `seq` = `motion_count`;
  `pir.c:37` timestamps edges with the 32-bit `to_ms_since_boot()`; `pir_get_status()`
  returns false during the 30 s warm-up.
- `wifi_task.c:54`: `CYW43_NONE_PM`.
- `lwipopts.h`: `MEM_SIZE 4000`, `LWIP_SOCKET 0`, `MEMP_NUM_SYS_TIMEOUT 16`.
- `aws_iot_task.c`: lwIP MQTT client; `time_wait_synced()` before TLS.

**Pico SDK 2.3.0 / lwIP**

- `mqtt_opts.h`: `MQTT_OUTPUT_RINGBUF_SIZE 256`, `MQTT_REQ_MAX_IN_FLIGHT 4`,
  `MQTT_VAR_HEADER_BUFFER_LEN 128`.
- `mqtt.c`: `mqtt_client_new()` uses `mem_calloc`; the cyclic timer uses `sys_timeout`.
- `opt.h`: `MEMP_NUM_TCP_PCB 5`.
- `cyw43.h`: `CYW43_DEFAULT_PM` = `CYW43_PERFORMANCE_PM` (PM2).

**External**

- AWS Kinesis Video Streams API reference, *GetClip*: "the first 100 MB or the first
  200 fragments". No longer on the PIR path; it still bounds VMS's own clips.
- Debian trixie archive: `mosquitto` 2.0.21, `pyside6` 6.8.2, `python3-paho-mqtt`,
  `python3-asyncio-mqtt`, `motion` 4.7.0, `ffmpeg` 7.1; no Qt MQTT package.

---

## Appendix A: Sizing POST_SEC

### A.1 Definition

POST_SEC is the extra footage kept **after the PIR's `stop` event** (§3.4):

```
clip     = [ t0 − 12 s ,  t1 + POST_SEC ]      t0 = start, t1 = stop (event times, §3.5)
assembly =   t1 + POST_SEC + segment capture   (≤ ~35 s: one 30 s segment plus a tick)
```

Don't confuse POST_SEC with the assembly wait. That wait only delays building the clip until
the ring segments covering its end are complete; it adds nothing to the clip.

### A.2 Why it can be much shorter than VMS's 33 s

VMS cuts ONVIF detection clips at ts + 33 s because those detectors latch for only about 5 s
and give **no real end-of-activity signal**. The 33 s is a margin for "the event probably
continued".

The PIR in retriggerable mode (the firmware default) gives a real end. Its output stays high
until one **hold time** (T_hold, set by the module's potentiometer) has passed **after the
last movement**. When `stop` arrives, the clip already contains T_hold seconds of "no motion
detected":

```
last movement ──── T_hold ────► stop (t1) ── POST_SEC ──► clip end
              (already in the clip)          (extra)
```

POST_SEC only has to cover what T_hold doesn't.

### A.3 What POST_SEC must cover

| Component | Why | Typical size |
|---|---|---|
| **Keyframe rounding** (T_keyframe) | The trim is a stream copy, so it cuts on keyframes. cam‑01 has a keyframe every 2 s (`GOP=30` at `CAM_FPS_OUT=15`), so the end can be rounded off by up to one GOP | ≥ 2 s |
| **The camera sees more than the PIR** (T_camera_extra) | If the camera's field of view reaches further than the PIR's, a person can still be visible after the PIR has lost them | 0 s if the PIR's field covers the camera's view; otherwise measure |
| **Short-gap tolerance** (T_return_gap) | A person who returns within POST_SEC is still inside the same clip (see A.4) | Depends on how returns should be handled |

```
POST_SEC = max( 2 × T_keyframe ,  T_camera_extra ,  T_return_gap )
```

**Starting value without measurements: POST_SEC ≈ 4–5 s**, i.e. two GOPs, assuming the
PIR's field covers the camera's view.

### A.4 Coupling with "ignore while recording runs"

During POST_ROLL the session still counts as "recording", so a new `start` is **ignored**
(§3.3). That has a consequence:

- A person who returns **within POST_SEC** is in the clip only until t1 + POST_SEC.
- In retriggerable mode, if they keep moving past the session's end, the PIR output stays
  high and **no new `start` arrives**. That activity is never recorded.

So POST_SEC is a trade-off:

- **Longer:** brief returns are covered by the same clip, but every session costs more local
  storage, more stick writes, and more upload if the clip is selected.
- **Shorter:** cheaper, but the "returned and stayed" case falls into the gap.

The robust fix does not depend on POST_SEC. At session close, check the retained `pir/state`;
if it still reports `motion: true`, open a continuation session. That is decision **D4**
(§6), and this case is the strongest argument for it.

### A.5 Turning Phase 2 data into a number

From the observation log (`measurements/pir-<date>.jsonl`, Phase 2):

1. **T_hold:** do a single brief wave and record `duration_ms` of that start/stop pair. With
   almost no motion, the duration ≈ T_hold. This is the tail every clip already has.
2. **T_camera_extra:** walk out of the PIR's field on a path the camera can still see, and
   compare the recorded video with the `stop` time.
3. **Gap distribution (T_return_gap):** collect the times between each `stop` and the next
   `start`. If many gaps lie just above a candidate POST_SEC, people often return briefly.
   Then either raise POST_SEC to cover that percentile, or rely on D4's continuation session.
4. **Replay:** run the log through `ClipSession` with the Phase 4 replay tool for several
   POST_SEC values, and compare clips per hour, duty cycle and the number of ignored starts.
   This is the same method VMS used to size `COOLDOWN_SEC` (`adapter/bin/replay-gate.py`).
5. **Verify on real clips (Phase 6):** `ffprobe` the clip duration and check visually that
   the person has left the frame before the clip ends.

---

## Appendix B: Mosquitto on the Pi 4B, setup manual

Set up and verified on this Pi on 2026-10-03 (Raspberry Pi OS trixie, `mosquitto` 2.0.21-1).
Everything runs on the Pi. The files created here are system configuration and are **not in
git** (B.11). `LAUNCH.md` A10 is the short form of this appendix, without the explanations.

### B.1 What the package installs

- **Packages:** `mosquitto` (the broker, a system service) and `mosquitto-clients`
  (`mosquitto_pub`, `mosquitto_sub`).
- **The service** `mosquitto.service`:
  - starts `/usr/sbin/mosquitto -c /etc/mosquitto/mosquitto.conf` as root; the broker then
    runs as the `mosquitto` user;
  - `systemctl reload mosquitto` sends `SIGHUP`, which re-reads the password and ACL files
    without a restart.
- **The package's own config**, `/etc/mosquitto/mosquitto.conf`. Leave it unedited:

  ```
  # Place your local configuration in /etc/mosquitto/conf.d/
  persistence true
  persistence_location /var/lib/mosquitto/
  log_dest file /var/log/mosquitto/mosquitto.log
  include_dir /etc/mosquitto/conf.d
  ```

  - `include_dir` loads every file ending in `.conf` from `/etc/mosquitto/conf.d/`. **This
    project's settings go there, in a file of its own, `vms.conf` (B.5).** A package upgrade
    then never has to merge local changes into the package's file.
  - Persistence is already on, which the watcher's persistent session (§3.8) relies on.
- **Without any `listener` line, Mosquitto 2.x runs in "local only mode"**: it listens on
  `127.0.0.1` and `::1` only. That is the state straight after installing. The log says so:

  ```
  Starting in local only mode. Connections will only be possible from clients running on this machine.
  Create a configuration file which defines a listener to allow remote access.
  ```

### B.2 Install

```bash
sudo apt install mosquitto mosquitto-clients
systemctl is-active mosquitto      # active, but still local only (B.1)
```

### B.3 Users and passwords

| User | Used by | May |
|---|---|---|
| `pico` | the Pico 2 W firmware | publish under `home/pico2w-01/`; read its command topics |
| `vms` | `kvs-pir-watcher` and the admin app | read everything under `home/`; send PIR commands |
| `gui` | the optional kiosk, scripts | read only |

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd pico   # -c creates the file: first user only
sudo mosquitto_passwd /etc/mosquitto/passwd vms
sudo mosquitto_passwd /etc/mosquitto/passwd gui
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd
sudo chmod 600 /etc/mosquitto/passwd
```

- **Use `-c` only once.** It creates the file, and so wipes any users already in it.
- **Type passwords at the prompt.** The `-b <user> <password>` form puts them in your shell
  history.
- **The file holds salted hashes, not passwords.** Keep the plaintext passwords somewhere
  safe: the Pico's `lan_mqtt_config.h` needs `pico`'s, and VMS needs `vms`'s (B.7).
- **Ownership matters.** This version warns at startup if the file isn't owned by
  `mosquitto:mosquitto` or is world-readable: "Future versions will refuse to load this
  file." `mosquitto:mosquitto` with mode 600 avoids both warnings.
- **`mosquitto_passwd` warns the other way round,** and that is expected. Run through
  `sudo`, it reads the file as root, and complains that the owner and group are not root:

  ```
  Warning: File /etc/mosquitto/passwd owner is not root. Future versions will refuse to load this file.To fix this, use `chown root /etc/mosquitto/passwd`.
  ```

  **Ignore it, and don't run the suggested `chown root`.** The broker is the one that has to
  read the file, as the `mosquitto` user. After the warning the tool prompts for the password
  as usual.
- **After every `mosquitto_passwd` run,** confirm the ownership survived, restore it if not,
  and reload:

  ```bash
  ls -l /etc/mosquitto/passwd        # -rw------- mosquitto mosquitto
  sudo chown mosquitto:mosquitto /etc/mosquitto/passwd && sudo chmod 600 /etc/mosquitto/passwd
  sudo systemctl reload mosquitto
  ```

### B.4 Topic permissions (ACL)

```bash
sudo tee /etc/mosquitto/acl > /dev/null <<'EOF'
user pico
topic write home/pico2w-01/#
topic read  home/pico2w-01/pir/cmd/#

user vms
topic read  home/#
topic write home/pico2w-01/pir/cmd/#

user gui
topic read  home/#
EOF
sudo chown mosquitto:mosquitto /etc/mosquitto/acl
sudo chmod 600 /etc/mosquitto/acl
```

- **Paste the whole block,** from `sudo tee` through `EOF`. The lines between the markers are
  file content. Typed straight at the prompt, they only produce
  `bash: user: command not found`, which is harmless (it happened during the setup on
  2026-10-03).
- **Format:** a `user` line, followed by the `topic read|write|readwrite <pattern>` lines that
  apply to that user. `#` matches any number of topic levels.
- **Anything not granted is denied.** A denied publish is dropped silently: MQTT 3.1.1 can't
  refuse a publish, so the publisher still sees success. A topic a user may not read is simply
  never delivered to them.

### B.5 The project's config file

```bash
sudo tee /etc/mosquitto/conf.d/vms.conf > /dev/null <<'EOF'
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/acl
EOF
sudo systemctl restart mosquitto
```

| Line | Meaning |
|---|---|
| `listener 1883` | Accept connections on port 1883 on all of the Pi's interfaces. This line is what ends local-only mode |
| `allow_anonymous false` | Every client must log in. Mosquitto 2.x already does this once a listener is defined; the line makes it explicit |
| `password_file` | Users and password hashes (B.3) |
| `acl_file` | Topic permissions (B.4) |

- `vms.conf` can stay owned by root, mode 644: the broker reads it as root at startup.
- **Port 1883 is reachable from the LAN only.** Nothing is forwarded on the FRITZ!Box, so the
  project's rule of no inbound ports on the router still holds.

### B.6 Test it

**Healthy state:**

```bash
systemctl is-active mosquitto                 # active
ss -ltn | grep 1883                           # 0.0.0.0:1883 and [::]:1883
sudo tail -n 20 /var/log/mosquitto/mosquitto.log
```

The log after the restart on 2026-10-03. Its timestamps are Unix seconds; read them with
`date -d @1791022839`:

```
1791022839: mosquitto version 2.0.21 starting
1791022839: Config loaded from /etc/mosquitto/mosquitto.conf.
1791022839: Opening ipv4 listen socket on port 1883.
1791022839: Opening ipv6 listen socket on port 1883.
1791022839: mosquitto version 2.0.21 running
```

There is no "local only mode" line any more, and no `Warning: File …` lines.

**Refusals** (verified 2026-10-03):

```bash
mosquitto_pub -h localhost      -t home/pico2w-01/status -m x                    # anonymous
mosquitto_pub -h 192.168.178.53 -t home/pico2w-01/status -m x                    # anonymous, LAN address
mosquitto_pub -h 192.168.178.53 -u pico -P wrong -t home/pico2w-01/status -m x   # wrong password
```

Each prints `Connection error: Connection Refused: not authorised.` and exits with code 5. The
broker logs each one as a pair of lines:

```
1791022933: New connection from ::1:60532 on port 1883.
1791022933: Client auto-C4C88596-E527-9FDC-E4CF-96D4A2450C02 disconnected, not authorised.
```

- `::1` is `localhost` resolved to IPv6; the LAN tests show `192.168.178.53`.
- `auto-…` is the random client ID `mosquitto_pub` picks when none is given. The Pico
  connects with a fixed one (`pico2w-01`), which makes its connections easy to find in the log.

**The authenticated path and the ACL**, in two terminals:

```bash
# terminal 1: listen as vms
mosquitto_sub -h localhost -u vms -P '<vms-password>' -v -t 'home/#'

# terminal 2
mosquitto_pub -h 192.168.178.53 -u pico -P '<pico-password>' -t home/pico2w-01/status -m test
#   terminal 1 prints: home/pico2w-01/status test
mosquitto_pub -h localhost -u gui -P '<gui-password>' -t home/pico2w-01/pir/cmd/retrigger -m 1
#   terminal 1 prints NOTHING (gui may not send commands; the publish itself still "succeeds")
mosquitto_pub -h localhost -u vms -P '<vms-password>' -t home/pico2w-01/pir/cmd/retrigger -m 1
#   terminal 1 prints: home/pico2w-01/pir/cmd/retrigger 1
```

Run the `pico` publish from **another LAN host** as well. On the Pi itself, the LAN address is
routed internally, so only a second machine proves the network path.

The successful run on 2026-10-03: terminal 1 printed exactly two lines,

```
home/pico2w-01/status test
home/pico2w-01/pir/cmd/retrigger 1
```

and the broker logged each login with its user:

```
New client connected from ::1:58340 as 73E5… (p2, c1, k60, u'vms').
New client connected from 192.168.178.53:42718 as ABB0… (p2, c1, k60, u'pico').
New client connected from ::1:39486 as 64FE… (p2, c1, k60, u'gui').
New client connected from ::1:39128 as 1FEE… (p2, c1, k60, u'vms').
```

**A denied publish leaves no trace in the log** at the default log level. The `gui` client
logged in and disconnected normally; the only evidence of the ACL at work is the line
missing from terminal 1. That is why the test watches the subscriber.

### B.7 Credentials for VMS programs (needed from Phase 2)

The broker's `passwd` holds hashes, but VMS's own clients need the plaintext password. The
first one is the Phase 2 logger, `adapter/observe_pir.py`; the watcher follows in Phase 6.
The password goes in a file that only root and the VMS user can read:

```bash
read -rsp 'vms password: ' P; echo
printf '%s' "$P" | sudo sh -c 'umask 077; cat > /etc/adapter/mqtt-vms.password'; unset P
sudo chown root:vladimir /etc/adapter/mqtt-vms.password
sudo chmod 640 /etc/adapter/mqtt-vms.password
```

Why this shape:
- `read -s` doesn't echo the password, and it never appears on a command line, so it isn't
  saved in your shell history;
- `printf` is a shell builtin, so the password doesn't show up in the process list either;
- `umask 077` means the file never exists world-readable, even for a moment.

**Proof:** a 30-second run of the logger against the real broker connects rather than being
refused:

```bash
venv-adapter/bin/python3 adapter/observe_pir.py --out /tmp/pir-check.jsonl --hours 0.01
grep -c '"kind": "connected"' /tmp/pir-check.jsonl     # 1 -- and no "connect_failed" lines
rm /tmp/pir-check.jsonl
```

The keys in `/etc/adapter/adapter.env`, **added on this Pi on 2026-10-03** (backup:
`adapter.env.bak-2026-10-03`) and in its template `config/adapter.env.example`:

```
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USER=vms
MQTT_PASSWORD_FILE=/etc/adapter/mqtt-vms.password
```

They are read with `config.get()` at use, never at import (§2.3), so a Pi without them keeps
working.

### B.8 Changing things later

| Task | How |
|---|---|
| Add a user, or change a password | `sudo mosquitto_passwd /etc/mosquitto/passwd <user>` (**no `-c`**). Type the password at the prompt; nothing is shown while typing. Then check the ownership and reload (B.3) |
| Remove a user | `sudo mosquitto_passwd -D /etc/mosquitto/passwd <user>`, then check the ownership and reload (B.3) |
| Change the ACL | edit `/etc/mosquitto/acl`, then reload |
| Change `vms.conf` | `sudo systemctl restart mosquitto` (listener changes need a restart) |
| Clear a stale retained message | `mosquitto_pub -h localhost -u pico -P '<pico-password>' -t <topic> -r -n` (an empty retained message deletes it) |
| Watch all project traffic | `mosquitto_sub -h localhost -u vms -P '<vms-password>' -v -t 'home/#'` |

### B.9 Firewall

`nftables` is active on this Pi, with `/etc/nftables.conf` from guide §7.3:

- **Input chain: no rules,** so incoming connections are accepted, and LAN clients reach port
  1883 with nothing to add.
- **Output chain: `tcp dport 8883 drop`.** This is the guide's permanent proof that the adapter
  needs nothing but outbound 443.
  - It also drops connections **from the Pi to its own port 8883**.
  - So if the broker ever gets a TLS listener (Phase 13), it must not use 8883; take, for
    example, 8884. Otherwise the watcher and the admin app couldn't connect to it.
- The file is what loads at boot; `sudo nft list ruleset` shows what is live.

### B.10 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ss` shows only `127.0.0.1:1883`; the log says "Starting in local only mode" | No listener was loaded: `vms.conf` is missing, in the wrong directory, or doesn't end in `.conf` | B.5, then restart |
| `Connection Refused: not authorised` | Anonymous client, wrong user or password, or the user is missing from `passwd` | Check the credentials; set the password again (B.8) |
| Users vanished after adding one | `-c` was used again | Create all users again (B.3) |
| `Warning: File … owner is not mosquitto` or `… world readable` in the **broker's log** | Ownership or mode of `passwd` or `acl` | `chown`/`chmod` as in B.3 and B.4, then reload |
| `Warning: File … owner is not root` printed by **`mosquitto_passwd`** | The tool runs as root through `sudo`; the file correctly belongs to `mosquitto` | Ignore it and answer the `Password:` prompt. Don't `chown root` (B.3) |
| `systemctl restart` fails | A typo or a wrong path in `vms.conf` | `sudo journalctl -u mosquitto -n 30` |
| The publish "succeeded", but the subscriber gets nothing | The ACL denies the publisher that topic (silently), or the topic is misspelled | Compare the topic with `/etc/mosquitto/acl` |
| The Pico can't connect, but the LAN tests work | The Pi's address changed (no DHCP reservation yet), or the Pico's credentials are wrong | Phase 0 step 1; `lan_mqtt_config.h` |
| `bash: user: command not found` | File content pasted at the prompt | Use the `sudo tee … <<'EOF'` blocks |

### B.11 What to keep

None of these files are in git:

- `/etc/mosquitto/conf.d/vms.conf` and `/etc/mosquitto/acl`, which contain no secrets;
- `/etc/mosquitto/passwd`, or just the three plaintext passwords, from which it can be
  recreated;
- `/etc/adapter/mqtt-vms.password`.

The setup steps are in `LAUNCH.md` A10. Phase 13 can add templates for `vms.conf` and `acl`
under `config/mosquitto/` (they contain no secrets), following the pattern of
`config/adapter.env.example`, so A10 installs them instead of pasting heredocs.
