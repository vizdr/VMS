# Camera feature inventory — `cam-02` (ONVIF IPC)

What the real ONVIF camera on this LAN can actually do, enumerated over ONVIF rather than
read off a spec sheet. Compiled 2026-09-06 by probing the live device
(`adapter/onvif_discovery.py` + ad-hoc ONVIF service calls).

**For anything about bitrate or cost, `COSTS-1.4.md` is authoritative** — it holds the
full measurement set (§1.2), the day/night analysis (§3) and the sensitivity ranking (§7).
The figures repeated here are a summary for convenience and defer to it on conflict.
`COSTS-1.3.md` is superseded: several of its point estimates were single samples taken
under unrecorded lighting, and v1.4 re-bases them.

**Every row is marked verified or advertised.** This project has been bitten repeatedly by
the difference — a service can appear in `GetServices` and still fault on every call (see
"What does not work" below), and the whole point of an inventory is to be trustworthy
about which is which.

| Status | Meaning |
|---|---|
| **verified** | observed working against the live camera |
| *advertised* | the device claims it; not exercised, or not confirmed to do anything |

---

## 1. Device identity

| Field | Value |
|---|---|
| Manufacturer | `A_ONVIF_CAMERA` (OEM rebrand; WSDL leaks a Hikvision lineage — see §16.2.1 of the guide) |
| Model | `YMA42P_C2_WM701_AF` |
| Firmware | `V3.3.2.1 build 2024-12-26` |
| Serial | `EF00000006108447` |
| ONVIF version | 17.06 |
| Address | `192.168.178.67` (static), ONVIF at `http://192.168.178.67/onvif/device_service` |

## 2. Services advertised

`device`, `media` (ver10 **and** ver20), `imaging` (ver20), `events`, `analytics` (ver20),
`ptz` (ver20), `search`, `replay`, `recording`, `deviceIO`, plus a vendor `plus` service.

Capability flags worth noting: Analytics `RuleSupport=true` / `AnalyticsModuleSupport=true`;
Events `WSPullPointSupport=true` (this is the one that matters for detection-driven
recording); Media streaming supports RTP multicast, RTP/TCP and RTP/RTSP/TCP.

## 3. Video and audio profiles

| Profile | Video | Audio | In use here |
|---|---|---|---|
| `MainStream` | H.264 **2560×1440** @ 15 fps, 3000 kbps configured | G.711 64 kbps | no |
| `SubStream` | H.264 **640×360** @ 15 fps, 500 kbps configured | G.711 64 kbps | **yes** (`cam-02` ingests this) |

Both profiles carry a `VideoAnalyticsConfiguration` (`VideoAnalyticsName`). The KVS
pipeline (`stream-cam02.sh`) depayloads video only — the G.711 audio track is present on
the wire and currently discarded.

**Measured bitrate.** `COSTS-1.4.md` §1.2 is the authoritative measurement set and §3 the
analysis; this is a summary only. Video payload only, 60 s stream-copy, all at 15 fps:

| Stream | Configured ceiling | Daylight | Dark | 24/7 est. (12/12) |
|---|---|---|---|---|
| `SubStream` (in use) | 500 kbps | 0.056 | 0.049 | **0.052 Mbps** |
| `MainStream` | 3000 kbps | 0.615 / 0.785 | 1.472 / 1.969 | **1.211 Mbps** |

**Illumination is the dominant variable, not motion** (`COSTS-1.4.md` §3.1, §3.3). The
main stream moves ~2.5× between daylight and dark on an empty scene; the mechanism is
sensor noise, which is high-entropy and temporally uncorrelated, so it defeats motion
estimation and forces P-frames to carry real residual. Ranked by measured effect:
**illumination (140–320 %) > motion (~34 %) > codec (40–50 %)**.

Two cautions on figures quoted elsewhere:

- The ~34 % motion figure comes from a cat entering frame (1.472 → 1.969), but the frame
  confirming the cat was captured about two minutes *after* that run began, so the
  attribution is uncertain — §1.3 of the cost doc treats 1.472 as an upper bound on
  night-empty and a lower bound on night-with-cat. It should not be cited as a clean
  motion measurement.
- `COSTS-1.3.md`'s single 0.937 Mbps sample for this stream sits mid-range, not at the
  bottom; v1.4 re-bases it to **0.615–1.969 Mbps**.

**The sub-stream's real advantage is stability, not just cheapness** (§3.2). It barely
responds to lighting (+14 %) because downscaling to 640×360 spatially averages sensor
noise away before the encoder sees it. A tier whose cost is flat across the diurnal cycle
can be committed to at fleet scale; one that triples every night cannot.

**`cam-01`, for contrast, has the *widest* relative swing measured here** — 0.241 Mbps in
daylight against 1.005 dark, a 4.2× ratio, while pointed at a static photograph with no
motion in the scene at all. `video_bitrate=1000000` is a rate-control *ceiling*, not a
constant bitrate: the encoder runs VBR beneath it and only saturates the cap when noise
demands it. (An earlier reading of the 1.005 night sample as evidence that rate capping
buys predictable cloud cost is withdrawn — see `COSTS-1.4.md` §3.4. Stability comes from
resolution, not from capping.)

## 4. Detection and analytics

The camera does its own detection on-device and burns a red bounding box into the video as
an OSD overlay. More usefully, it will **push the detections as ONVIF events** over a
WS-PullPoint subscription — no cloud inference, no Pi CPU cost.

| Topic | Payload | Status |
|---|---|---|
| `tns1:UserAlarm/IVA/HumanShapeDetect` | `State` (bool) | **verified — observed firing live**; this is the red box |
| `tns1:RuleEngine/CellMotionDetector/Motion` | `IsMotion` (bool), plus `Rule` in the source | **verified — fires, latches** (§9 Phase 0) |
| `tns1:VideoSource/MotionAlarm` | `State` (bool) | **verified — fires, latches** (§9 Phase 0) |
| `tns1:Device/Trigger/DigitalInput` | `LogicalState` (bool) | *advertised* |
| `tns1:Device/Trigger/Relay` | `LogicalState` | *advertised* |
| `tnshik:AlarmIn` (vendor) | `State` (bool) | *advertised* |

**All three detection topics are now confirmed firing** by the Phase 0 captures (§9) —
including `VideoSource/MotionAlarm`, which an earlier 60-second probe had missed and which
was consequently carried as merely *advertised*. All three latch rather than pulse, with a
median 5.0 s hold. Their firing rate, mutual discrimination and idle false-positive rate
are measured in §9; the three `Device/Trigger` and vendor `AlarmIn` topics remain
unexercised, as nothing is wired to the camera's physical inputs.

**Readable over ONVIF, but not writable.** Analytics configuration `MOTION-DECTECT1` holds one module,
`MyCellMD`, of type `tt:CellMotionEngine`:

| Parameter | Type | Current |
|---|---|---|
| `Sensitivity` | `xsd:integer` | 80 |
| `Layout` | `tt:CellLayout` | cell grid (detection zones) |

The full tuning set is richer than the module alone — the rule `MyMDRule`
(`tt:CellMotionDetector`) also carries `MinCount=5`, `AlarmOnDelay=100`,
`AlarmOffDelay=100` and `ActiveCells` (a base64 cell mask, 32 cells on this camera).

**None of it is writable, despite being present and typed.** An earlier revision of this
document claimed "both are writable via `SetVideoAnalyticsConfiguration`" — that was
inferred from the parameters being exposed, never tested, and it is **wrong**.
`SetVideoAnalyticsConfiguration` is a **silent no-op** here: it returns success and
changes nothing. Verified by sending `Sensitivity=55` (payload confirmed to carry 55) and
reading back `80` immediately after, twice. The advertised ONVIF 2.0 analytics service
(`ver20/analytics/wsdl`) exposes **no callable operations**, and the vendor ISAPI path
404s (§8). These values can therefore only be changed in the camera's own web UI.

A note on the mask: `ActiveCells` decodes to 32 bits (`d0ff00f0`), but the camera returns
an **empty `Layout` element**, so it never reports the grid's Columns/Rows. The geometry
is unknown, which is why the admin GUI shows the bit pattern rather than drawing a grid
that might not correspond to reality.

There is also a serialisation trap: the empty `Layout` ElementItem cannot be re-serialised
by zeep (`ValidationError: Missing element for Any`), so any round-trip must strip it
before sending. That makes the write *succeed* — which is exactly what makes the no-op
behaviour easy to mistake for success.

## 5. Imaging controls

Exposed via the Imaging service and already partly wired into this project (IR mode is
driven from both GUIs today):

- **IR-cut filter** — `ON` / `OFF` / `AUTO` (**verified**; drives the day/night switch and
  the IR illuminator)
- Brightness, contrast, colour saturation, sharpness (1–255 each)
- Wide dynamic range (on/off + level), backlight compensation
- Exposure — `AUTO` / `MANUAL`, exposure time 100–100000 µs
- White balance — `AUTO` / `MANUAL` with Cr/Cb gains
- Focus — manual, with near/far limits (the `AF` in the model name is a motorised lens)
- Vendor extension: `Defogging`, `NoiseReduction`

## 6. Physical I/O

| Item | Detail | Status |
|---|---|---|
| Relay outputs | 4 (`Relays1`–`Relays4`), monostable, 10 s delay, idle-open | *advertised* — siren / light / gate triggering |
| Digital inputs | present, with an `AlarmIn` event topic | *advertised* — external PIR, door contact |
| Audio output | `AudioMainToken` — a speaker exists | *advertised* — makes talkdown physically possible |
| Audio input | G.711 on both profiles | **verified** present on the wire |

The relay outputs are a capability the main guide does not mention anywhere. They would
allow the adapter to trigger a physical siren or light from the cloud.

## 7. PTZ — advertised, unconfirmed

`GetNodes` returns node `ptz0` with 255 presets and `HomeSupported=true`. This is
physically a fixed camera, so this is most likely digital PTZ or vestigial firmware. **No
movement has been commanded or observed.** Do not rely on it without testing.

## 8. What does not work

**ONVIF Profile G edge recording.** The device advertises the `recording`, `replay` and
`search` services, but every call into them faults:

```
Fault: The [action] cannot be processed at the receiver.
```

So there is no SD-card recording to pull from. This matters because it rules the camera
out as a solution to the durable-outage-buffering gap (guide §16.3c) — that still needs
the local `splitmuxsink` ring buffer on the Pi.

**Hikvision ISAPI.** Despite the firmware's Hikvision lineage, `/ISAPI/...` endpoints
return the OEM's soft-404 page, so the richer vendor controls (e.g. independent IR-LED
brightness via `supplementLight`) are unavailable. Standard ONVIF is the ceiling here.

---

## 9. Plan — detection-driven recording

Goal: let the user choose, per camera, how clips get created — **`manual` stays the
default and today's behaviour is unchanged unless a mode is explicitly selected.**

| Mode | Trigger | ONVIF topic |
|---|---|---|
| `manual` (**default**) | user presses Record in the cloud client | — |
| `motion` | any motion | `VideoSource/MotionAlarm` |
| `cellMotion` | motion inside configured grid cells | `RuleEngine/CellMotionDetector/Motion` |
| `human` | human shape detected | `UserAlarm/IVA/HumanShapeDetect` |

### What already exists and does not need rebuilding

The event→clip path was built in §9 of the guide and is still wired up:

```
MQTT topic adapter/+/event  →  IoT Rule  →  clip-to-s3 Lambda  →  S3 + clips table  →  both GUIs
```

`clip_to_s3.py` takes `{"timestamp": <iso8601>, "stream": "cam-02", "labels": [...]}` and
cuts **ts−12 s to ts+33 s**. So a detection only has to become one MQTT publish — **no new
Lambda, no new AWS resources, and clips appear in the existing browser UI automatically.**

### Phase 0 — Observation harness (prerequisite, ~half a day)

Before any recording logic: a small script that holds a PullPoint subscription open for
hours and logs every event with topic, timestamp and payload.

Answers the questions nothing can be sized without: which of the three motion topics
actually fire on this camera; whether they latch (`State=true` … `State=false`) or pulse;
how many events a person walking through the hall generates; the idle false-positive rate
overnight.

#### Phase 0 — DONE, results

Built as `adapter/observe_events.py` (run it with `--analyse <log>` to summarise). Two
captures: a 4.5 h afternoon run (ended by a Pi reboot) and a complete 9.5 h overnight run,
logs in `measurements/`.

**All three topics fire**, including `VideoSource/MotionAlarm`, which nothing had confirmed
before and which Phase 2 depends on. **All three latch** — `true` on detection, `false` on
clear, median hold 5.0 s — so the watcher should trigger on the rising edge and gets event
duration free from the falling edge.

**They discriminate, but only modestly** (±2 s co-occurrence, afternoon run):

| Test | Result |
|---|---|
| `HumanShapeDetect` with no motion topic nearby | 1 / 143 (0.7 %) |
| `CellMotionDetector` with no human nearby | 13 / 125 (10.4 %) |
| `MotionAlarm` with no human nearby | 11 / 126 (8.7 %) |

Human detection is essentially a subset of motion, as physics requires; motion fires
without a human ~10 % of the time (plausibly the cat). So the four modes are genuinely
different triggers, but in a hallway whose only movers are people and one cat, choosing
`motion` over `human` buys about 10 % more clips. **Caveat: rising-edge counts are not
incident counts** — `HumanShapeDetect` logged *more* edges (143) than motion (125) despite
being a subset, because it re-arms faster within one continuous incident. Use the
co-occurrence test, not the raw counts.

**Idle false-positive rate: zero.** Over the 9.5 h overnight run there were four event
clusters, one of which was the camera being physically serviced and is excluded:

| Incident | Duration | Edges | Preceded by |
|---|---|---|---|
| 21:34–21:57 | 1371 s | 76 | — |
| 03:21–03:24 | 131 s | 24 | **5.41 h silence** |
| *04:47–04:49 (servicing — excluded)* | *117 s* | *18* | *1.38 h silence* |
| 07:02–07:03 | 33 s | 6 | **2.22 h silence** |

9.01 of 9.5 hours were zero-event, and **110 heartbeats with no gap over 400 s prove the
harness was observing throughout** — the silence is a measurement, not missing data. That
distinction is the whole reason the harness emits heartbeats.

**The dawn day/night switch produced no spurious events.** Illumination-mode changes
invert the entire image and are a classic false-trigger source; the ~06:50 transition fell
inside a 2.22 h silence. Measured absent rather than assumed away.

**Cooldown sizing** (`HumanShapeDetect`, servicing excluded, 45 s clip per `clip_to_s3`):

| Cooldown | Whole 9.5 h: clips/h | duty | Overnight 22:00–07:00: clips/h | duty |
|---|---|---|---|---|
| none | 3.8 | 4.7 % | 0.78 | 0.97 % |
| 60 s | 1.6 | 2.0 % | 0.22 | **0.28 %** |
| 300 s | 0.6 | 0.8 % | 0.11 | 0.14 % |

Against the busy-afternoon sample (10.2 clips/h, 12.8 % duty at 60 s), **the diurnal
spread in duty cycle is ~45×** — far wider than the 2.5× spread in bitrate, making duty
cycle the largest source of variance in the cost model, not merely its largest lever
(`COSTS-1.4.md` §7.2).

**Consequences for the phases below:** no reordering needed — `MotionAlarm` is alive, so
Phase 2 can proceed as written. A 60 s cooldown is a reasonable default, but note that
`COSTS-1.4.md` §7.2's 5 % duty-cycle assumption corresponds to roughly a **300 s** cooldown
during *busy* hours; overnight, any cooldown is academic because there is almost nothing to
trigger on.

### Phase 1 — Mode selection, no detection yet (~half a day)

- Add `recordingMode` to the `cameras` DynamoDB table, defaulting to `manual`.
- Surface a selector in both GUIs — the cloud client's camera panel and the ONVIF admin
  table — gated on capability the same way IR control already is (`cam-01` is a USB webcam
  with no ONVIF, so it can only ever be `manual`).
- Nothing consumes the value yet.

Deliberately shipped before any event code: it is pure plumbing, independently testable,
and carries no regression risk to the working control plane.

#### Phase 1 — DONE

Registry field `recordingMode` added to the `cameras` table, defaulting to `manual`, with
allowed values `manual` / `motion` / `cellMotion` / `human`. **Nothing reads it yet** — the
event watcher is Phase 2 — so both GUIs say so rather than implying behaviour changed.

| Piece | Where |
|---|---|
| Read path | `list_cameras.py` now returns `recordingMode` and `supportsDetection` |
| Cloud write path | new Lambda `set-camera-mode` + `POST /cameras/mode` (Cognito-authorised, CORS) |
| Local write path | new `POST /api/cameras/<id>/mode` in the ONVIF admin app |
| Cloud GUI | selector per camera panel, disabled when `supportsDetection` is false |
| Local GUI | "Recording" column in the admin table, same gating |
| IAM | `SetCameraModeLambdaRole` — `GetItem`/`UpdateItem` on the `cameras` table only |

`supportsDetection` is derived from the presence of `onvifHost`, since a detection mode
needs an ONVIF event subscription. `cam-01` is a USB webcam with no ONVIF and is therefore
`manual`-only; its selector renders disabled.

**Validation is enforced server-side in both write paths, not just hidden in the UI** — the
APIs are reachable independently of the pages, and a camera silently set to a mode it
cannot honour would surface later as an apparently broken watcher. Verified: setting
`cam-01` to a detection mode is rejected (400, "no ONVIF host"), as are unknown modes and
unknown camera IDs.

**Cross-GUI consistency verified**: both paths write the same registry row, so setting
`cellMotion` in the local admin app was read back as `cellMotion` by the cloud
`list-cameras` Lambda. Both cameras were reset to `manual` after testing.

### Phase 2 — Event watcher + simple motion (the simplest detection)

- New `adapter/event_watcher.py`, run as its own user unit `kvs-event-watcher.service`,
  reusing `aws_device_creds.py` and `camera_control.py`. **Separate from `agent.py` on
  purpose** — if the ONVIF event loop wedges or the camera drops, Start/Stop over MQTT must
  keep working.
- Reads the registry, subscribes PullPoint for every camera whose `recordingMode` is a
  detection mode, and on a rising edge publishes to `adapter/adapter-01/event`:
  `{"timestamp": <now iso>, "stream": "cam-02", "labels": ["motion"]}`.
- **Cooldown is not optional.** `clip_to_s3` cuts a 45-second clip per event; an unfiltered
  motion topic could fire many times a second. Needs a minimum gap (start at ~60 s, tune
  from Phase 0 data) or one person walking down the hall becomes thirty overlapping clips,
  thirty `GetClip` calls and thirty S3 PUTs.
- No IoT policy change needed — **verified**: `KVSAdapterThingPolicy` already grants
  `iot:Publish` on `arn:aws:iot:...:*/adapter/${iot:Connection.Thing.ThingName}/*`, which
  resolves to `adapter/adapter-01/*` and so covers the event topic.

#### Phase 2 — DONE

`adapter/event_watcher.py`, run as `kvs-event-watcher.service` (user unit,
`Restart=always`). Verified end to end on a real detection: **07:17:29 detection →
07:18:07 publish → `clips/cam-02/2026/09/09/071729.mp4`, 45.0 s of actual media.**

**It does not publish over MQTT, and that is deliberate.** The plan assumed it would, but
the IoT policy grants `iot:Connect` only on `client/${iot:Connection.Thing.ThingName}` —
i.e. `adapter-01`, the identity `agent.py` already holds. A second MQTT connection would
be refused, or worse would kick the agent off, since IoT Core drops the older session on
a duplicate client ID. The watcher instead publishes through the **HTTP data plane**
(`iot-data:Publish`), which reaches the same topic and the same IoT Rule without
contending for that identity. This did need one narrow IAM grant on `KVSAdapterRole`:
`iot:Publish` on `topic/adapter/adapter-01/event` only.

**A real defect found and fixed during verification.** `clip_to_s3` cuts `ts-12s .. ts+33s`,
and KVS can only return footage it has already ingested. Publishing at detection time
therefore yields **only the pre-roll**:

| Trigger timestamp | Actual media | Metadata claims |
|---|---|---|
| `ts = now` (naive) | **12.0 s** | 45 s |
| `ts` 40 s in the past | 42.0 s | 45 s |
| **after `PUBLISH_DELAY_SEC = 38`** | **45.0 s** | 45 s |

So the naive version produced a clip showing the *approach* to an event and none of the
event itself, while the database recorded it as 45 seconds — the metadata lied. The
watcher now waits out the post-roll before publishing, keeping the **detection** time in
the payload so the clip stays centred on the event; only the write is deferred. Worth
noting the duration in DynamoDB is nominal (computed from the requested window), so it
cannot be used to detect this class of failure — only probing the media can.

**The gate logic is unit-testable rather than camera-dependent.** Rising-edge detection
and cooldown live in a `ClipGate` class, and `adapter/bin/replay-gate.py` replays a
recorded `observe_events.py` log through it. Against the real 9.5 h Phase 0 capture:

| Cooldown | motion | cellMotion | human |
|---|---|---|---|
| 0 s | 28 clips | 34 | 37 |
| **60 s** | **13** | 15 | 17 |
| 300 s | 7 | 7 | 7 |

That reproduces the hand-computed Phase 0 figures and means the cooldown can be retuned
against recorded data without standing in front of a camera.

`MODE_TOPICS` maps all three modes, so **Phase 3 (human) is already functional** — it is
the same code path with a different topic string, exactly as the plan predicted.

### Phase 3 — Human shape — DONE

No new code: `MODE_TOPICS` in `event_watcher.py` already maps `human` to
`tns1:UserAlarm/IVA/HumanShapeDetect`, and everything downstream is the identical code
path proven end to end in Phase 2. Verified by setting `cam-02` to `human` and confirming
the watcher picked up the registry change and re-subscribed to the human topic.

As the plan predicted, this was free — it was sequenced third only to honour
simplest-trigger-first, not because it needed more work.

### Phase 4 — Cell-grid motion — DONE (detection), BLOCKED (configuration)

Splits in two, because the camera only cooperates with half of it.

**Detection: done.** `cellMotion` works through the same watcher as Phases 2 and 3 —
verified subscribing to `tns1:RuleEngine/CellMotionDetector/Motion` and firing through
the identical publish path. No new code was required.

**Configuration: not possible over ONVIF on this camera.** The plan called for a
sensitivity slider and a grid mask written back through
`SetVideoAnalyticsConfiguration`. That call is a **silent no-op** here (§4): it returns
success and changes nothing. The ONVIF 2.0 analytics service exposes no callable
operations, and vendor ISAPI 404s. There is no network path to change these values —
only the camera's own web UI.

Two traps worth recording, because both make the failure look like success:

- The empty `Layout` ElementItem the camera returns cannot be re-serialised by zeep
  (`ValidationError: Missing element for Any`). Stripping it makes the write **succeed** —
  and a successful write that changes nothing is far more misleading than a rejected one.
- `ActiveCells` decodes to 32 bits, but with `Layout` empty the camera never reports the
  grid's Columns/Rows. Any grid UI would have to guess the geometry.

**What shipped instead:** a read-only analytics panel in the admin GUI
(`GET /api/cameras/<id>/analytics`) showing `Sensitivity`, `MinCount`, `AlarmOnDelay`,
`AlarmOffDelay` and the decoded `ActiveCells` mask, with the reason it cannot be edited
stated on the page. Read-only is the honest deliverable: these values explain *why*
detection behaves as it does — `AlarmOnDelay`/`AlarmOffDelay` at 100 are directly
responsible for the ~5 s latch measured in Phase 0 — and surfacing them is useful even
when changing them isn't possible.

If tuning becomes necessary, the routes are: the camera's web UI (manual), or a
different camera whose ONVIF write path actually works. Worth testing
`SetVideoAnalyticsConfiguration` on any future camera **before** relying on it.

### Phase 5 — The ingest question (a decision, not a task)

This one needs an explicit choice, because it is where detection-driven recording either
does or does not deliver the cost win it promises.

`clip_to_s3` reaches **12 seconds before** the trigger. KVS can only return footage it
already ingested, so pre-roll requires the producer to have been running before the event.
That collides with §1.2's rule ("never leave `kvs-cam0N.service` running unattended").

| Option | Pre-roll | Ingest cost | Note |
|---|---|---|---|
| Producer runs while a detection mode is active | yes | unchanged — 24/7 | evidence trail, no saving |
| Event starts the producer, stops after N s idle | **lost** | only during events | KVS also needs seconds to spin up |
| Local ring buffer (`splitmuxsink`, guide §16.3c) feeds the clip | yes | only during events | most work; converges with the outage-buffering gap |

`COSTS-1.4.md` §7.2 puts numbers on why this decision matters more than anything else in
this plan: duty cycle is worth **up to ~20×**, the largest lever in that document. At a 5 %
duty cycle — plausible for an indoor hallway — `cam-02` main-stream recording falls from
$3.94 to **~$0.20/camera-month**, which is a bigger saving than the codec change, the
retention tier and the entire day/night illumination span *combined*. It is also the one
lever that makes the others' variance irrelevant: if you are only recording 5 % of the
time, it stops mattering that the night bitrate is 2.5× the day one.

There is a second-order consequence worth carrying into any storage decision: event-gated
recording produces short discontinuous clips rather than a continuous timeline, which
suits object storage and undermines KVS's continuous-timeline model further still
(`COSTS-1.4.md` §7.2).

So option 1 is the safe default only in the sense that it changes nothing; it is also the
option that forfeits the entire benefit. Worth deciding deliberately.
