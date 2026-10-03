# PIR Motion Sensor → MQTT → VMS: Analysis and Plan (Pico 2 W side)

**Status:** Phase 1 (the firmware) is done, reported 2026-10-03. The Pi side confirms it: the
Pico's traffic meets the interface contract in §3 (VMS Phase 2, first findings). One setting is
still open: the PIR module's hold-time potentiometer is at minimum (T_hold < 0.5 s) and needs
turning up to about 3–5 s. Real events now produce clips on the Pi (VMS Phase 6), and that
exposed two firmware findings, in §4.5 · **Written:** 2026-10-02 ·
**Revised:** 2026-10-03, with the Pi-side decisions that affect the Pico, and the firmware
facts re-checked against this repository (`main`, pushed 2026-10-02 09:04 UTC)

**Where this file comes from.** The plan was first written here as `PIR-MQTT-VMS.md`. It was
then copied to the VMS repository and developed there as `PIR-MQTT-VMS-PI4.md`, which is
**authoritative for the Raspberry Pi 4B side**: the broker, recording, the GUIs and AWS.
**This file replaces `PIR-MQTT-VMS.md` here and is authoritative for the firmware (Phase 1)
and for the MQTT interface the Pico publishes (§3).** The Pi side is summarised only as far
as the Pico depends on it. References of the form *VMS §3.5* point into
`PIR-MQTT-VMS-PI4.md`.

**Repositories involved:**

- **this repository** (`blink_freertos`; GitHub `vizdr/RasPi_Pico2W_FreeRTOS_AWS`): the
  Pico 2 W firmware with the Makeblock Me PIR Motion Sensor v1.1
- **VMS** ([vizdr/VMS](https://github.com/vizdr/VMS)): the video management system on the
  Raspberry Pi 4B (USB camera `cam-01`, ONVIF camera `cam-02`, AWS KVS)

**Goal:** motion start/stop events from the PIR sensor reach the Raspberry Pi 4B with minimal
delay over a local MQTT broker. There they trigger **local recording of the USB camera**
`cam-01`. Clips the user selects are copied to AWS later.

The Pico's part:

- publish every motion edge reliably, stamped with its own clock;
- publish its state and its online status;
- accept the retrigger-mode command;
- keep its existing AWS telemetry path unchanged.

---

## Contents

1. [The system from the Pico's point of view](#1-the-system-from-the-picos-point-of-view)
2. [Firmware: current state and constraints](#2-firmware-current-state-and-constraints)
3. [Interface contract: what the Pi relies on](#3-interface-contract-what-the-pi-relies-on)
4. [Plan](#4-plan)
5. [Decisions that affect the Pico](#5-decisions-that-affect-the-pico)
6. [Consistency record](#6-consistency-record)
7. [Verified facts and sources](#7-verified-facts-and-sources)

---

## 1. The system from the Pico's point of view

```
Pico 2 W
 pir.c ISR ─► pir_task ─► hooks ─► lan_mqtt_task (new) ──MQTT 1883, user "pico"──► Mosquitto on the Pi 4B
                                     pir/event, pir/state (heartbeat), status (LWT)       │
                                     ◄── pir/cmd/retrigger ───────────────────────────────┤
                                                                                         ▼
                                              kvs-pir-watcher (VMS): ages events by boot_ms,
                                              one recording session at a time, records cam-01
                                              locally from a ring buffer, shows the PIR state
                                              in the local admin app and in VMS's cloud page

 aws_iot_task ──MQTT/TLS──► AWS IoT Core (telemetry every 10 s, unchanged: D6)
```

What the Pi does with the Pico's messages (VMS §3.3–§3.5):

- **Only `pir/event` starts a recording.** The retained `pir/state` never does, so the Pico
  may republish it as often as it likes.
- **Each event is placed on the Pi's clock using the Pico's `boot_ms`.** An event that arrives
  more than **60 s** after it happened is dropped for recording, because the Pi's ring buffer
  no longer holds its footage; it is still counted. A delayed event inside that limit is
  placed where it really happened, so queueing, back-off or a TLS stall on the Pico can't
  shift a clip.
- **Duplicates are expected:** QoS 1 may deliver twice, and the Pi deduplicates by
  `(seq, event)`.
- **`status = offline` closes an open recording session** at that moment.
- **While a session runs, further starts and stops are ignored.** In single-trigger mode
  that means the repeated start/stop pairs; in retriggerable mode, mostly duplicates.

---

## 2. Firmware: current state and constraints

Re-checked against this repository on 2026-10-03 (§7).

| Item | Current state (file) | Consequence |
|---|---|---|
| PIR driver | `pir.c/h`: raw GPIO IRQ on the shared `IO_IRQ_BANK0` (`gpio_add_raw_irq_handler`, coexists with CYW43), level-based edge handling, S1 mode pin on GPIO 13 | Done. Nothing to change |
| Edge timestamp | `pir.c:37`: `to_ms_since_boot(get_absolute_time())`, a **32-bit** millisecond counter that wraps after **49.7 days** | The Pi treats `boot_ms` going backwards as a Pico reboot. Payloads therefore carry a **64-bit** `boot_ms` (§3.3), widened in `lan_mqtt_task`, not in the ISR |
| PIR service | `pir_task.c/h`: ISR→task queue (`PIR_QUEUE_LEN 8`), 30 s warm-up, 1 s level resync (`PIR_RESYNC_MS`), `pir_status_t {motion, motion_count, last_change_ms}`, weak hooks | Hooks need a **timestamp parameter**. Today they are `pir_on_motion_start(void)` and `pir_on_motion_stop(duration_ms)`; `pir_apply()` already has `time_ms` |
| Warm-up | `pir_get_status()` returns **false** while the sensor warms up (30 s after boot) | The on-connect `pir/state` publish needs a warm-up variant (§3.2) |
| Resync path | If an edge is missed, the 1 s poll applies the new level with the poll time | Such an event is up to ~1 s late. Harmless: the Pi's clip cut is keyframe-rounded to 2 s anyway |
| Weak hooks | Defined only in `pir_task.c`; no strong override exists yet | **One owner** of the PIR events: the new LAN MQTT task. Any further consumer (e.g. AWS) is fed from the broker, not from a second override |
| MQTT client | lwIP `apps/mqtt` raw API, used by `aws_iot_task.c` (TLS to AWS, keep-alive 60 s) | Reuse it for a **second client** to Mosquitto. No coreMQTT and no sockets (`LWIP_SOCKET 0`) |
| MQTT client limits | `MQTT_OUTPUT_RINGBUF_SIZE` 256 B, `MQTT_REQ_MAX_IN_FLIGHT` 4 (`mqtt_opts.h`) | Bursts can return `ERR_MEM`. Events must be **retried, not dropped** |
| lwIP heap | `MEM_SIZE 4000` (`lwipopts.h:36`). `mqtt_client_new()` takes ~0.5 KB from it (`mem_calloc`: 256 B ring buffer, 128 B rx buffer, request list) | A second client plus a larger ring buffer needs `MEM_SIZE` ≈ 16000 |
| TCP connections | `MEMP_NUM_TCP_PCB` default 5 | Enough for AWS plus the local broker |
| Timers | Each MQTT client runs a cyclic `sys_timeout`; `MEMP_NUM_SYS_TIMEOUT 16` (`lwipopts.h:30`, raised once already after a "MEMP_SYS_TIMEOUT is empty" panic) | Raise to 18 for headroom |
| Wi‑Fi power save | Already off: `cyw43_wifi_pm(&cyw43_state, CYW43_NONE_PM)` (`wifi_task.c:54`) | Nothing to do on the Pico |
| Time | SNTP from `pool.ntp.org` (`time_task.c`); `aws_iot_task` blocks in `time_wait_synced()` (`aws_iot_task.c:103`) | The local path **must not wait for SNTP**, or it stops working without internet. It waits for `wifi_wait_connected()` only. UTC `ts` is added only when `time_is_synced()` |
| Shared lwIP core lock | An AWS TLS **reconnect** runs the mbedTLS handshake inside lwIP | Can delay local publishes during that handshake (estimate: ~1 s). Rare, only on reconnect. Harmless for recording, because the Pi places events by `boot_ms` (§3.3) |
| JSON payloads | `aws_iot_task.c`: `snprintf` into fixed buffers; the length goes straight to `mqtt_publish()` | Add the `len < 0 || len >= sizeof(buf)` guard to every payload |
| Task priorities | `main.c`: Wi‑Fi, Time, AwsIot, sensors at `tskIDLE_PRIORITY + 1`; `pir_task_start(tskIDLE_PRIORITY + 2)` | `lan_mqtt_task` at `+ 2` (§4, Phase 1) |

---

## 3. Interface contract: what the Pi relies on

### 3.1 Broker and credentials

- **Broker:** Mosquitto 2.0.21 on the Pi 4B at **`192.168.178.53`**, port **1883**, plain TCP.
  Installed, configured and tested on 2026-10-03 (VMS Phase 0).
  - The address is fixed on the FRITZ!Box (done 2026-10-03). That is required, because the
    address is compiled into the Pico (`lan_mqtt_config.h`).
  - The broker's setup manual is VMS `PIR-MQTT-VMS-PI4.md` Appendix B.
- **Authentication is mandatory.** The broker runs with `allow_anonymous false`; the Pico
  connects as user **`pico`** with a password.
- **ACL for `pico`:**

  ```
  user pico
  topic write home/pico2w-01/#
  topic read  home/pico2w-01/pir/cmd/#
  ```

  A publish outside that pattern is dropped by the broker silently. MQTT 3.1.1 has no way to
  refuse a publish, so the Pico still sees success.
- **Other users:** `vms` (the Pi's watcher and admin app; it sends the retrigger command) and
  `gui` (read-only). Neither concerns the firmware.

### 3.2 Topics

| Topic | QoS / retained | Direction | Payload |
|---|---|---|---|
| `home/pico2w-01/status` | 1 / **retained**, LWT = `offline` | Pico → | `online` / `offline` |
| `home/pico2w-01/pir/event` | 1 / no | Pico → | `{"seq":42,"event":"start","boot_ms":123456,"ts":1759312345123}`. `stop` adds `"duration_ms"`. `ts` only when SNTP has synced |
| `home/pico2w-01/pir/state` | 1 / **retained** | Pico → | `{"motion":true,"changed_ms":…,"count":42,"dropped":0,"boot_ms":…}`. During warm-up: `{"warming_up":true,"boot_ms":…}` |
| `home/pico2w-01/pir/cmd/retrigger` | 1 / no | → Pico | `1` / `0` → `pir_task_set_retrigger()` |

Field meanings:

| Field | Source | Meaning |
|---|---|---|
| `seq` | `motion_count` | A start and its stop share it. It exposes gaps |
| `event` | hook | `start` or `stop` |
| `boot_ms` in `pir/event` | the ISR timestamp, widened to 64 bit | When the edge happened, on the Pico's clock |
| `boot_ms` in `pir/state` | `time_us_64() / 1000` at publish | When the state was published, on the Pico's clock |
| `changed_ms` | `last_change_ms`, widened | When the last start or stop happened |
| `count` | `motion_count` | Starts since boot |
| `dropped` | the LAN queue's drop counter | Events lost because the queue was full (optional; the Pi shows it if present) |
| `duration_ms` | hook | Length of the motion that just ended |
| `ts` | system clock | UTC milliseconds, for logs; the Pi doesn't use it for timing |

**When `pir/state` is published:**

1. on connect, **before** any queued event (it is the Pi's first clock reference, §3.3);
2. after each event;
3. **every 30 s** as a heartbeat, even when nothing moves.

### 3.3 Timestamps: `boot_ms`

The Pi computes `offset = min(receive_time − boot_ms)` over recent messages, places each
event at `offset + boot_ms`, and drops events older than 60 s (VMS §3.5). That works without
SNTP on the Pico, and it puts late events where they belong. For it to work, the Pico must
keep five rules:

1. **`boot_ms` is milliseconds since boot, 64-bit, and never wraps.** In the `pir/state`
   stream it goes backwards only at a reboot, which is how the Pi detects one. The Pi never
   uses events for that: a queued event's `boot_ms` is older than the latest state's by
   design (rule 2).
   - Keep the hooks and the ISR on the cheap 32-bit value.
   - Widen it in `lan_mqtt_task` when building the payload:
     `ev64 = now64 − (uint32_t)(now32 − ev32)`, with `now64 = time_us_64() / 1000` and
     `now32 = (uint32_t)now64`, taken together.
   - This is exact for any event younger than 49.7 days.
2. **An event's `boot_ms` is the edge time from the ISR**, never the publish time. A queued or
   retried event keeps its original value.
3. **`pir/state`'s `boot_ms` is the publish time.** That is what makes it a clock reference.
4. **On connect, publish `pir/state` before draining the event queue.** After a Pico reboot,
   that guarantees the Pi's first reference is fresh rather than a queued event. It also lets
   the Pi assign every event to the right boot: an event belongs to the boot whose first
   `pir/state` arrived before it.
5. **Keep the 30 s heartbeat.** It keeps the reference current through long quiet periods and
   tracks crystal drift.

### 3.4 Delivery

- **QoS 1 everywhere.**
- **Retry, never drop.** An event stays in the queue until `mqtt_publish()` returned
  `ERR_OK` (§4, Phase 1).
- **Publish in queue order.** The Pi pairs starts and stops by `seq`.
- **Duplicates are fine**: the Pi deduplicates by `(seq, event)`.
- **Old events still go out.** An event the Pi will drop as too old (> 60 s) is still worth
  sending, because the Pi counts it and it shows up in the statistics. Don't discard on the
  Pico.

### 3.5 LWT and keep-alive

- **Keep-alive 30 s.** The broker publishes the retained LWT `offline` after ~45 s
  (1.5 × keep-alive) without traffic.
- The Pi closes an open recording session when it sees `offline`, and shows the Pico as
  offline locally and in VMS's cloud page.

### 3.6 Commands

- **`pir/cmd/retrigger`** (`1` = retriggerable, `0` = single trigger) comes from the Pi's
  local admin app. It maps to `pir_task_set_retrigger()`, a single `gpio_put()`
  (`pir.c:82`), and is safe from the lwIP callback.
- It is the only command. VMS's cloud page can switch PIR recording on and off and request
  clip uploads, but all of that happens on the Pi. **Nothing from the cloud reaches the
  Pico.**

### 3.7 What the Pico doesn't need to know

All of these live in VMS (`PIR-MQTT-VMS-PI4.md`) and need no firmware support:

- the ring buffer, clip assembly and retention;
- uploads of selected clips;
- VMS's own cloud resources (the `pir-` namespace) and its cloud page;
- the `pirRecording` switch;
- the successor project.

---

## 4. Plan

### 4.1 All phases, and where the Pico is involved

Phase numbers are shared with the VMS copy.

| Phase | Owner | What | Pico involvement |
|---|---|---|---|
| 0 | VMS | Network and broker, **done 2026-10-03** | A fixed address for the Pico; the `pico` broker user (§4.2) |
| **1** | **this repository** | **Firmware, done 2026-10-03** | **All of it (§4.3)** |
| 2 | VMS | Observation: log the Pico's topics for an afternoon and a night | The Pico runs normally; part of the run is in single-trigger mode, via `pir/cmd/retrigger = 0` |
| 3–8 | VMS | Registry switch, session logic, ring recording, watcher, uploads, local GUI | None. A Pico running Phase 1 is the input |
| 9–11 | VMS | VMS-only cloud resources, status in the cloud, VMS's cloud page | None |
| 12 | VMS | Ring buffer in RAM, retention under load | None |
| 13 | both | Hardening and documentation | Hardware watchdog on the Pico; this repository's docs (§4.4) |

### 4.2 Phase 0: what the Pico needs from it

1. **A DHCP reservation for the Pico** (and for the Pi) on the FRITZ!Box; both off the guest
   network.
2. **The broker user `pico`** with its password, and the ACL from §3.1.
3. **Test the credentials before flashing,** from any LAN host:

   ```bash
   mosquitto_pub -h 192.168.178.53 -u pico -P '<pico-password>' -t home/pico2w-01/status -m test
   ```

   Watch it arrive on the Pi with `mosquitto_sub -h localhost -u vms -P '<vms-password>' -v -t 'home/#'`.

**Status: all done on 2026-10-03.**

- Both boards have fixed addresses on the FRITZ!Box; the Pi's is `192.168.178.53`.
- Mosquitto is installed with the three users and the ACL above.
- The Pi's own Wi‑Fi power save is off.
- Verified:
  - anonymous clients and wrong passwords are refused;
  - `pico` publishing via `192.168.178.53` reaches a `vms` subscriber;
  - `gui` can't publish to the command topic.
- **Still untried:** a connection from a second machine. The Pico's first connection will be
  that test.

### 4.3 Phase 1: firmware (this repository)

1. **Hooks with timestamps:**

   ```c
   void pir_on_motion_start(uint32_t time_ms);
   void pir_on_motion_stop(uint32_t time_ms, uint32_t duration_ms);
   ```

   - Pass `time_ms` through from `pir_apply()`, which already has it.
   - Keep the weak defaults.
   - Update the example in README-PIR.md §5.

2. **New `lan_mqtt_task.c/h`**, the sole owner of the second lwIP MQTT client and of the
   strong hook overrides.
   - **Connection:**
     - wait for Wi‑Fi only (`wifi_wait_connected()`), **not** for SNTP;
     - plain TCP (`tls_config = NULL`), user `pico` and password, keep-alive 30 s;
     - LWT: `status` = `offline`, retained, QoS 1;
     - a fixed client id, e.g. `pico2w-01`.
   - **On connect, in this order:**
     1. publish `status` = `online` (retained);
     2. publish `pir/state` (retained): from `pir_get_status()` if it returns true, otherwise
        the warm-up form. `boot_ms` = now;
     3. subscribe to `pir/cmd/#`. `retrigger` calls `pir_task_set_retrigger()`;
     4. then drain the event queue.
   - **Event path:**
     - the strong hooks only do `xQueueSend(…, 0)` of `{event, seq, time_ms, duration_ms}`
       into a ~16-entry queue. If it is full, they count a drop and never block the `pir`
       task. This queue is separate from `pir_task`'s own ISR queue (`PIR_QUEUE_LEN 8`);
     - `seq` = `motion_count`, read with `pir_get_status()` in the hook (a start and its stop
       share it);
     - loop: `xQueuePeek()` → widen `time_ms` to 64-bit (§3.3) → build the payload →
       `mqtt_publish()` → `xQueueReceive()` only after `ERR_OK`. On `ERR_MEM`, back off
       ~50 ms and retry;
     - after each event, republish the retained `pir/state`.
   - **Heartbeat:** republish `pir/state` every 30 s.
   - **Plumbing:**
     - every lwIP call between `cyw43_arch_lwip_begin()` / `cyw43_arch_lwip_end()`;
     - reconnect with exponential backoff;
     - `snprintf` length guard on every payload;
     - while disconnected, events stay queued, and the queue's drop counter goes into
       `dropped`.

3. **Configuration:** gitignored `lan_mqtt_config.h` plus a committed
   `lan_mqtt_config.h.example` (broker IP, port, user, password), the same pattern as
   `wifi_credentials.h` / `wifi_credentials.h.example`. Add the real file to `.gitignore`.

4. **`lwipopts.h`:** `MEM_SIZE` → 16000, `MQTT_OUTPUT_RINGBUF_SIZE` → 512,
   `MEMP_NUM_SYS_TIMEOUT` → 18.

5. **`main.c` and `CMakeLists.txt`:**
   - create the task at `tskIDLE_PRIORITY + 2`, the same level as `pir_task`. A stack of
     1024 words is a starting point; confirm it with `uxTaskGetStackHighWaterMark()`;
   - add `lan_mqtt_task.c` to `add_executable(blink_freertos …)`.

6. **Check:**
   - a wave produces `start` and `stop` with matching `seq` in `mosquitto_sub` within
     milliseconds, with `boot_ms` increasing and equal to the edge time printed on the serial
     console;
   - right after boot, `pir/state` arrives in its warm-up form, and the normal form after the
     30 s warm-up;
   - `pir/state` arrives every 30 s with a growing `boot_ms`;
   - **widening near the wrap:** a unit test of the widening helper with `now32` just after
     the 32-bit wrap and `ev32` just before it gives the right 64-bit value;
   - with the broker stopped for a minute, waves are queued, and after reconnect they arrive
     in order with their original `boot_ms`, after `status` and `pir/state`;
   - powering off the Pico yields `offline` after ~45 s;
   - with the internet unplugged (LAN up), local events still flow;
   - AWS telemetry still arrives every 10 s;
   - a publish with wrong credentials is refused at connect.

### 4.4 Phase 13: the Pico's part

- **Hardening:** hardware watchdog on the Pico.
- **This repository's docs:**
  - README-PIR.md: the new hook signatures, and the LAN MQTT task in the design section;
  - README.md;
  - MQTT_RASPI_4B_Pico2W.md, if it is kept (it isn't in the pushed repository), aligned with
    §6.

### 4.5 Findings from the Pi side, 2026-10-03 (to act on here)

**1. Publishing is slower than the sensor in a burst.**
- The burst: motions about 1 s apart (T_hold is still 0.4 s), each producing a start, a stop
  and two `pir/state` republishes.
- The Pi received them **increasingly late:** 0.0, 5.3, 6.2, 8.2, 12.8 and 17.4 s after
  their edge times. That's roughly one motion delivered every ~2 s.
- **Not the Pi:** its watcher handled the same burst at 0.7 ms per message.
- **Not the AWS path:** the Pico's AWS telemetry kept its 10 s rhythm throughout, so no TLS
  handshake was holding lwIP's lock (§2).
- **Seen again at 20:08–20:11** (waves for the Pi's Phase 10 check, about 30 s apart): lateness
  1.0, 7.7, 10.1, 13.4, 34.3 and 37.2 s, growing within the series. One stop (seq 494) arrived
  **74.1 s** late, past the Pi's 60 s staleness limit: with PIR recording on, the Pi would have
  dropped it.
- So it is `lan_mqtt_task`'s publish loop. Things to check:
  - a fixed delay per loop iteration, where only a back-off on `ERR_MEM` should exist;
  - waiting for each QoS 1 acknowledgement before the next publish (lwIP allows 4 in flight);
  - republishing `pir/state` after **every** event: once per drained batch is enough, and
    halves the traffic;
  - Nagle on a stream of small packets.
- **Why it matters:** the Pi places late events correctly (§3.3), but an event more than 60 s
  late is dropped, and a 16-entry queue fills in a long burst. Tuning the hold time to 3–5 s
  cuts the event rate sharply, but the queue should drain faster than the sensor fills it
  regardless.

**2. 35 minutes without network, and no reboot.**
- From 16:09:48 to 16:44:45, neither the broker nor AWS heard from the Pico.
- It **never rebooted** (`boot_ms` continuous, about 2 h 39 min of uptime), its sensor kept
  counting (`count` 145 → 270), and its queue overflowed: **`dropped` 225**. That proves the
  counter works.
- The green LED was off; fixing a loose wire brought it back.
- One more drop came 48 s after it reconnected (keep-alive timeout), then it stayed stable.
- If a wiring or power fault can stall the network part without resetting the processor, the
  hardware watchdog (Phase 13) should also cover "no broker connection for N minutes", not
  only a hung task.

---

## 5. Decisions that affect the Pico

| # | Decision | Status | Effect on the firmware |
|---|---|---|---|
| D6 | AWS path of the Pico: keep the direct TLS telemetry path, or route through a Mosquitto bridge on the Pi | **Decided 2026-10-03: keep direct** | `aws_iot_task` stays as it is. The ~1 s stall of local publishes during an AWS TLS reconnect stays too; it doesn't affect recording (§3.3) |
| D4 | What happens when presence outlasts the 180 s clip cap, or continues after a session ends | **Open**, decided after Phase 2 | If the Pi opens continuation sessions, it reads the **retained `pir/state.motion`**. That is one more reason to keep `pir/state` accurate and retained |
| D2 | Only PIR sessions count as "recording runs" | Decided | None |
| others | D1, D3, D5, D7–D13, and the remote-control choices (A2, B3, C2, option (b)) | Decided or open in VMS | None; all on the Pi or in AWS (VMS §6) |

---

## 6. Consistency record

### The original plan (2026-10-02)

| Earlier statement | Status now | Reason |
|---|---|---|
| Motion events to AWS IoT Core on `…/motion`, stored in DynamoDB, served by an API, web UI polling every 2 s (AWS plan) | **Superseded** for motion | Latency (seconds) and cloud-only access; the local path is ~5–30 ms |
| `motion` daemon + `pir-camera.service` drive the USB camera (local plan) | **Dropped** | VMS/MediaMTX owns the camera |
| Web GUI served by Mosquitto (`http_dir`, WebSockets 9001), SQLite recorder, retained `pir/history` (local plan) | **Dropped** | The VMS admin app is the local GUI; VMS's `clips` table is the recording history |
| Topic contract `status` / `pir/event` / `pir/state` / `pir/cmd` | **Kept**, with the changes below | |
| Pico `lan_mqtt_task`, `lwipopts.h` changes, hook timestamps | **Kept** | Phase 1 |
| MQTT_RASPI_4B_Pico2W.md: coreMQTT over sockets with `LWIP_SOCKET=1` | **Not applicable** | The project already uses lwIP `apps/mqtt`; sockets are disabled |
| MQTT_RASPI_4B_Pico2W.md: ISR via `gpio_set_irq_enabled_with_callback()`, edge type from the event mask | **Not applicable** | `pir.c` uses a raw per-pin handler next to the CYW43 handler, and acts on the sampled level |
| MQTT_RASPI_4B_Pico2W.md: "turn off Wi‑Fi power save on the Pico" | **Already done** | `wifi_task.c:54` |
| MQTT_RASPI_4B_Pico2W.md: "SNTP optional" | **Refined** | SNTP exists, but the local path must not depend on it |

### This revision (2026-10-03)

| Earlier statement | Status now | Reason |
|---|---|---|
| File `PIR-MQTT-VMS.md`, covering both boards | **Replaced** by this file for the Pico, and by VMS `PIR-MQTT-VMS-PI4.md` for the Pi | Each repository holds the part it owns |
| `pir/state` = `{"motion","since_boot_ms","count"}`, published on connect and after each event | **Changed:** `changed_ms` (= `last_change_ms`), `boot_ms` (publish time), optional `dropped`, a warm-up form, a 30 s heartbeat, and published before the queue drains on connect | The Pi uses `pir/state` as its clock reference (§3.3) |
| Timestamps: the Pi uses its receive time, or the Pico's `ts` if more than 2 s older | **Replaced:** the Pi places events by `boot_ms` and drops events older than 60 s; `ts` is informational | It works without SNTP on the Pico, and late events land where they happened |
| `boot_ms` from `to_ms_since_boot()` | **Changed:** 64-bit in payloads, widened in `lan_mqtt_task` | Found while re-checking `pir.c:37`: the 32-bit counter wraps after 49.7 days, which the Pi would read as a reboot |
| On connect: publish `pir/state` "from `pir_get_status()`" | **Refined:** a warm-up form while `pir_get_status()` returns false | Found while re-checking `pir_task.h`: it returns false for the 30 s warm-up |
| "a ~16-entry queue" | **Clarified:** a new queue in `lan_mqtt_task`, separate from `pir_task`'s ISR queue (`PIR_QUEUE_LEN 8`) | Re-checked `pir_task.c:41` |
| Broker credentials | **Mandatory:** user `pico`, anonymous refused | Mosquitto 2.x with `allow_anonymous false` |
| Retrigger command sent by the `gui` user | **Changed:** sent by the Pi's admin app as user `vms`; `gui` is read-only | No effect on the firmware: it still subscribes to `pir/cmd/#` |
| Fixed IPv4 for the boards, listed like an option | **Required** | The broker address is compiled into the Pico |
| D6 open | **Decided:** keep the direct AWS path | VMS decision, 2026-10-03 |
| "`boot_ms` goes backwards only at a reboot, which is how the Pi detects one" | **Refined:** only in the `pir/state` stream; events are never used to detect a reboot | Found testing VMS's Phase 2 observer: a late event's older `boot_ms` was taken for a reboot. No change to the firmware; rules 2–4 already give the Pi what it needs |
| (new) Burst throughput | **Too slow:** about one motion delivered every ~2 s in a 1 s burst; lag grew to 17 s | Seen on the Pi in VMS Phase 6 (§4.5); to fix in `lan_mqtt_task` |
| (new) Network loss without reboot | **35 min, loose wiring;** `dropped` reached 225 | §4.5; the watchdog should also cover a lost broker connection |
| The Pi records clips from KVS | **Changed:** the Pi records locally from a ring buffer; selected clips are uploaded; there is remote control from VMS's own cloud page | No effect on the firmware |

---

## 7. Verified facts and sources

**This repository**, re-checked 2026-10-03 against `main` (pushed 2026-10-02 09:04 UTC)

- `CMakeLists.txt:36`: `project(blink_freertos …)`; `add_executable(blink_freertos …)` lists
  `pir.c` and `pir_task.c`.
- `pir.h/pir.c`:
  - `pir.c:37`: the edge callback gets `to_ms_since_boot(get_absolute_time())`;
  - `pir.c:72`: `gpio_add_raw_irq_handler()`;
  - `pir.c:82–85`: `pir_set_retrigger()` is a single `gpio_put()` on the S1 pin.
- `pir_task.h`:
  - `pir_status_t {motion, motion_count, last_change_ms}`;
  - `pir_get_status()` returns false during warm-up;
  - `pir_task_set_retrigger()`;
  - weak hooks `pir_on_motion_start(void)`, `pir_on_motion_stop(uint32_t duration_ms)`.
- `pir_task.c`:
  - `PIR_GPIO 14`, `PIR_MODE_GPIO 13`, `PIR_RESYNC_MS 1000`, `PIR_QUEUE_LEN 8`;
  - weak hook definitions at lines 57–66;
  - `pir_apply(bool level, uint32_t time_ms)` at line 105 increments `motion_count` on start
    and calls the hooks;
  - no other file overrides the hooks.
- `wifi_task.c:54`: `CYW43_NONE_PM`. `wifi_task.h`: `wifi_wait_connected()`,
  `wifi_is_connected()`.
- `time_task.h`: `time_wait_synced()`, `time_is_synced()`.
- `lwipopts.h`: `MEMP_NUM_SYS_TIMEOUT 16` (line 30), `LWIP_SOCKET 0` (32), `MEM_SIZE 4000`
  (36), `LWIP_ALTCP_TLS_MBEDTLS 1`.
- `aws_iot_task.c`:
  - `AWS_IOT_PUBLISH_INTERVAL_MS 10000` (line 34);
  - `time_wait_synced()` before TLS (103);
  - `mqtt_client_new()` (120);
  - `keep_alive = 60` (129);
  - `snprintf` + `mqtt_publish()` (193).
- `main.c`: tasks at `tskIDLE_PRIORITY + 1` (lines 55–61); `pir_task_start(tskIDLE_PRIORITY + 2)`
  (63).
- `README-PIR.md` §5 "Using the events": the hook example to update.
- `MQTT_RASPI_4B_Pico2W.md` and the original `PIR-MQTT-VMS.md` are not in the pushed
  repository.

**Pico SDK 2.3.0 / lwIP** (from the original plan)

- `mqtt_opts.h`: `MQTT_OUTPUT_RINGBUF_SIZE 256`, `MQTT_REQ_MAX_IN_FLIGHT 4`,
  `MQTT_VAR_HEADER_BUFFER_LEN 128`.
- `mqtt.c`: `mqtt_client_new()` uses `mem_calloc`; the cyclic timer uses `sys_timeout`.
- `opt.h`: `MEMP_NUM_TCP_PCB 5`.
- `cyw43.h`: `CYW43_DEFAULT_PM` = `CYW43_PERFORMANCE_PM` (PM2).

**The Pi side**, from VMS `PIR-MQTT-VMS-PI4.md`, 2026-10-03

- The Pi is at `192.168.178.53`, fixed on the FRITZ!Box.
- Mosquitto 2.0.21 is installed and configured: listening on `0.0.0.0:1883`, anonymous refused,
  users `pico`, `vms`, `gui` with the ACL in §3.1. Tests passed (VMS Appendix B.6).
- The Pi's own Wi‑Fi power save is off. Its firewall has no input rules, so the Pico reaches
  port 1883 with nothing to open.
- How the Pi uses the Pico's messages: VMS §3.2 (topics), §3.3 (sessions), §3.5 (event
  ageing, `STALE_SEC = 60`).
