# VideoSafeZone: next development round, ported from VMS

**Written:** 2026-10-18, in the VMS repository (`~/MyProjects/VMS`, HEAD `fdbb0eb` plus uncommitted
work), against the successor as published on GitHub (`vizdr/VideoSafeZone`, `master` at
`c13db2e`, 2026-09-28, "Paging improvement").
**For:** the next working session in `VideoSafeZone`, which runs on the same Pi 4B board from its
own SD card and OS (D1). This file reaches that card through VMS's GitHub repository (step 0.1);
copy it into the successor repository (step 0.2), and from then on that copy is the one to update.
Claude Code's memory doesn't cross cards either: this file is the whole hand-off.

The six requests this plan answers:

| # | Request | Where |
|---|---|---|
| R1 | Take over the VMS bugs that are relevant to the successor | Phase 1 (and Phase 3 for #64) |
| R2 | USB `cam-01`: bug fixes only, no new features | Phase 1.4, and §8 "Out of scope" |
| R3 | ONVIF discovery and trigger testing as in VMS | Phase 3 |
| R4 | H.264 / H.265 selector in the local admin GUI | Phase 4 |
| R5 | Unregister for ONVIF cameras | Phase 2 |
| R6 | Recording stream (main/sub) and fps (10/15); live view always on the sub stream when there is one | Phases 5 and 6 |

---

## 1. Ground rules for the whole round

1. **Port behaviour, not files.** VMS's files carry PIR, USB-scan and `pir-` code that must not
   come over. Every task below names the VMS function or section to take; copy that, and merge it
   into the successor's own structure (`codec_caps.py`, `rematch_cameras.py`, `onvifEndpointRef`,
   which VMS doesn't have).
2. **The shared AWS account and the data contract** (VMS `PIR-MQTT-VMS-PI4.md` §4.3) hold for
   every change:
   - rows in `cameras` and `clips` are updated **field by field**, never overwritten whole;
   - **add, never redefine**: new fields are fine; new values in an existing enum are not
     (`recordingMode` keeps exactly `manual`, `motion`, `cellMotion`, `human`);
   - every clip row carries `cameraId`, `startTs`, `s3Key`, `labels`, `durationSec`, `videoCodec`;
   - the successor deploys only its unprefixed resources and **never** `pir-*` (rule 5b);
     VMS never deploys unprefixed ones (5a).
   §7 lists every registry field this plan adds, checked against VMS.
3. **Deploys.** The successor owns the unprefixed Lambdas, `kvs-demo-api`, bucket
   `vms-demo-client-596633517506` and distribution `E1B12167KKII6B`. Before each deploy, check
   `LastModified` (VMS no longer deploys them, but `pico2w-*` and other projects share the
   account). **`clip-to-s3` is still used by VMS** for cam-02's ONVIF detection clips, so a change
   there must stay backward compatible.
4. **Verification is live, as in both repos' CLAUDE.md:** `bytesReceived` must grow (never
   `ready`), decode a frame and look at it, check cloud playback via `GetHLSStreamingSessionURL`,
   check pages in headless Firefox via Marionette (headless Chromium renders blank on this Pi).
5. **FoundAndFixed numbering has diverged.** The successor's `#51` is "clips older than the
   newest 50" (paging); VMS's `#51` is the cam-01 publisher hang. Numbers are permanent in both,
   so: **each bug ported in this round gets the successor's next free number (52, 53, …), and its
   entry starts with "Found in VMS as #N"**. §6 is the mapping table; fill in the successor
   numbers as entries are written.

---

## 2. Starting state: what the successor has and lacks

Checked in the GitHub clone at `c13db2e`:

| Area | Successor today | VMS today |
|---|---|---|
| Admin GUI routes | scan, cameras, register/re-register, status, analytics, mode, audio, outage-buffer, **codec**, on-wire, ir, start/stop | all of those, plus **unregister**, **detection** + **detection/test**, **codec GET**, **stream GET/POST**, (USB, PIR: not to port) |
| Codec switch | exists: `onvif_media2.Media2Client.set_codec`, token stored at registration by `codec_caps.py`, **done = camera read-back** | done = **MediaMTX receives the new codec**; encoder found per request from the registered `rtspUrl`; size from the wire (`plan_config`) |
| Event watcher | `MODE_TOPICS` hardcoded to cam-02's topics; subscription never renewed or unsubscribed | `onvif_events.py`: per-camera topics from `GetEventProperties`; `PullSubscription` (renew 20 s, `PT2S` pulls, always unsubscribe) |
| Discovery | first profile's URI, IR, endpoint ref | plus audio track (`_audio`), recording triggers, camera web page (`web_url`) |
| Unregister | none | `unregisteredAt` soft mark, skipped by every reader |
| Stream / fps | none (registers `profiles[0]`) | main/sub + 10/15/20/25 fps, keyframe interval kept in seconds |
| IP change | **`rematch_cameras.py`** (endpoint-ref based, timer every 5 min) | none (re-register by hand) |
| H.265 chain | `rtph265depay ! h265parse ! …hvc1`, **no timestamper** (#57/#58 live) | `… ! h265timestamper ! identity single-segment=true` |
| GUI-registered camera audio | `stream-channel.sh` links audio to fakesink only (#59 live) | G.711 → AAC branch, law from MediaMTX |
| cam-01 publisher | `exec gst-launch` (#51 live), no publisher restart at Start (#61 live) | `publish_run` watchdog; Start restarts the publisher on an audio mismatch |
| Admin status poll | DynamoDB `get_item` per poll (#55 live) | local registry cache |
| IR buttons | ON = "Force Night" / "IR On" (#63 live) | Day = `ON`, Night = `OFF` |
| `awsblock.sh` | no `63.176.0.0/12` (#54 live) | includes it |
| SDK patch 4 (kvssink ms timestamps) | not in LAUNCH A3 | LAUNCH A3, `VMS patch 4` |

Re-check this table against the local clone at the start (step 0.3): the GitHub state may have moved.

---

## 3. Decisions (settled 2026-10-18)

**D1. The successor runs on the same Pi 4B board, from its own SD card with its own Raspberry Pi
OS.** Switching projects means shutting down and swapping the card, so only one stack ever runs,
and nothing on the SD cards is shared: units, `/etc/adapter`, the KVS SDK build, `certs/`, the venv,
MediaMTX and Claude Code's memory are separate per card. The successor's card was set up on
2026-09-24/25 (its FoundAndFixed #24–#40) and should already have all of them.
**What still crosses between the two cards** (checked 2026-10-18), each handled in Phase 0:
- **The cameras themselves.** Encoder codec, stream and fps, motion-detection settings and
  **ONVIF event subscriptions** live in the camera. cam-03 has 10 subscription slots, and the
  successor's current event watcher leaks them (#64): cam-03's shared registry row is on Cell grid,
  so the successor's watcher subscribes to it at first boot, never renews or unsubscribes, fills
  the slots, and after the swap back VMS can't subscribe either. Step 0.5 disables that watcher
  until Phase 3.
- **The AWS side**: registry, clips, KVS streams, IoT Thing `adapter-01` (same certificate on both
  cards). Since one card runs at a time, the two-Pis-one-MQTT-session coupling (VMS plan §4.4,
  and one candidate cause of #60) can't happen as long as no second board uses the Thing.
- **Cameras registered from the other card have no producer on this one.** cam-03 was registered
  from VMS, so the successor's card has no `/etc/adapter/channels/cam03.env` and no `kvs-cam@cam03`.
  Its `unit_name()` then returns `kvs-cam03.service`, which doesn't exist, and Start silently does
  nothing (the #37 trap). Its Re-register deliberately never touches systemd. Fixed in step 2.4;
  until then, step 0.6 provisions cam-03 by hand. The same holds the other way round for any camera
  the successor registers (§9).
- **The USB stick** (`/mnt/vms-buffer`) moves with the board, and both projects use the same layout
  on it: `live/<path>/` (ring), `outage/<id>/state.json` (captures), plus VMS's `pir/`, which the
  successor ignores. Each side's uploader takes any `pending-upload` capture it finds, including
  the other's. Rule: **before shutting down for a card swap, wait until no capture is
  `pending-upload`** (`grep -l pending-upload /mnt/vms-buffer/outage/*/state.json`), and stop
  the producers.
- **The board's IP and Wi-Fi.** Same MAC, so the same DHCP lease (`192.168.178.53`) and the same
  network faults (#47, #52). Wi-Fi power save (#47) is a per-OS setting: check it on that card.
- **The VMS sources this plan ports have to reach that card.** Through GitHub: VMS is pushed up to
  `fdbb0eb`, but the work Phases 1–3 port is still uncommitted (step 0.1).
- **That card's OS versions are unknown here.** This card runs Debian 13 (trixie), GStreamer 1.26.2,
  MediaMTX v1.20.1 (the successor's LAUNCH pins the same MediaMTX). Phase 1 needs
  `h264timestamper` and `h265timestamper` (GStreamer's codectimestamper plugin) and MediaMTX's
  `tracks2[].codecProps`. Step 0.3 checks them before any porting.

**D2. Cloud live view on the sub stream: a second KVS stream per camera, `<cameraId>-live`,
pushed only on demand** while someone watches, and only when the recording stream isn't already
the sub stream (Phase 6.2). The `stream/cam-*/*` IAM wildcard already covers the name.

**D3. Live view doesn't need Start.** "Start" means "record to the cloud", "Live" means "watch".
Live works with recording stopped and costs only the sub stream while the page is open. Agree on
the page's button wording before Phase 6.2.

**D4. Unregistered cameras are skipped by `list-cameras` and hidden in both GUIs.** Checked, and
reasonable:
- `list-cameras` is now the successor's own Lambda; VMS's page uses its copy `pir-list-cameras`,
  so filtering there can't affect VMS. VMS's contract rule 4 ("filter in the page, not in a shared
  Lambda") was written while `list-cameras` was shared; it doesn't apply to it any more.
- Leaving the rows out of the response is enough to hide them in the cloud page, with no page logic.
  The admin GUI reads the table itself: it leaves them out of its camera table and shows them only
  in Discover ("Unregistered: was cam-03"), which Re-register needs.
- **Still needed as a safety net:** an open page or a direct API call can still name the camera, so
  `publish-cmd` (except stop), `record-clip`, `get-hls-url`, `set-camera-mode`, `set-camera-audio`
  and `set-camera-outage-buffer` answer 409, and `agent.py` refuses anything but stop.
- **One consequence to accept:** clips are listed per camera (`/clips?camera=`), so **an
  unregistered camera's evidence clips are no longer reachable from the cloud page** until it is
  re-registered. They stay in S3 and the `clips` table, and tiering and expiry carry on. If that
  ever matters, an "unregistered cameras' clips" view is a later addition; don't stop skipping the
  cameras because of it.
- Because the table is shared, unregistering in either project hides the camera in all four GUIs
  (VMS's already skip the mark). This also removes the coupling VMS's plan §4.4 lists ("an
  unregistered camera stays visible in the successor's page"); see §9.

**D5. Only 10 and 15 fps, and no other rate shown anywhere.** `FPS_CHOICES = (10, 15)`.
- The selector lists only 10 and 15, each only where the encoder accepts it. A camera currently
  at another rate (VMS can set 20 or 25 in the shared camera) shows an empty choice, "choose 10 or
  15", not its rate. It is never changed without someone choosing.
- If the encoder accepts neither, there's no fps selector, only the reason ("this camera offers
  neither 10 nor 15 fps").
- Registration preselects 10.
- The measured rate still appears in a warning after a switch when the camera sends fewer frames
  than asked ("asked 10, MediaMTX receives 8.6"). That reports a fault; it doesn't offer a rate.

---

## 4. Phases

Each phase: tasks, the VMS source, the verification that proves it, and the docs to touch. Phases
2–6 depend on Phase 1 being verified; 3–6 depend on Phase 2 (each new route must check
`is_unregistered`). Do not start a phase while the previous one has an unverified item.

### Phase 0: Preparation (session start)

**On the VMS card, before the swap:**

0.1 **Commit and push VMS.** Its working tree holds the trigger-test, `webUrl` and event-watcher work
this plan ports (`onvif_events.py`, `onvif_discovery.py`, `event_watcher.py`, `app.py`, both
`index.html`, …) and this file. The successor's card can only get them from GitHub
(`github.com/vizdr/VMS`). Then: producers stopped, no `pending-upload` capture on the stick (D1),
shut down, swap the card.

**On the successor's card:**

0.2 **The repository.** In `~/Projects/VideoSafeZone`, look for work that never reached GitHub
before pulling: `git status`, `git log origin/master..`, `git stash list`. Then `git pull`. Copy
this file in as `VMS-PORT-PLAN.md` and add it to the successor's `CLAUDE.md` doc list. Clone VMS
alongside as the **read-only port source**: `git clone https://github.com/vizdr/VMS.git
~/MyProjects/VMS` (no venv, no units, no setup: only for reading and copying functions). "VMS
source" in this plan means that clone.

0.3 **Re-check §2 against the local tree**, and the card's versions, since that OS is not this one
(D1):
```bash
cat /etc/os-release | head -2; gst-inspect-1.0 --version | head -1
gst-inspect-1.0 h264timestamper >/dev/null && gst-inspect-1.0 h265timestamper >/dev/null && echo timestampers-ok
~/Projects/VideoSafeZone/mediamtx/mediamtx --version            # tracks2 / codecProps (step 1.3)
venv-adapter/bin/python3 --version                              # VMS code uses `X | None` (3.10+)
iw dev wlan0 get power_save                                     # must be off (#47)
```
**If the timestampers are missing, stop and decide before Phase 1:** #57's fix depends on them
(H.265 to KVS fails without them), and #62's cam-01 part too. Options then: upgrade that card's
GStreamer, or move it to the OS this card runs.

0.4 **SDK patch 4** on that card's own build (its A3 had patches 1–3): apply VMS LAUNCH A3 patch 4,
then rebuild only the plugin, as VMS LAUNCH A3 says: `make src/gstreamer/gstkvssink.o` while
producers may run, then stop them and `make gstkvssink` (linking over a loaded plugin can crash a
running producer). Proof: `grep -n "VMS patch 4" "$KVS_SDK/src/gstreamer/gstkvssink.cpp"`.

0.5 **Before the stack runs at all, stop the subscription leak:**
`systemctl --user disable --now kvs-event-watcher` (D1, #64). It stays off until Phase 3 replaces
the watcher. While it's off, detection clips are made by neither card, because only one card runs.
If cam-03 refuses subscriptions on return to VMS anyway ("Invalid Hostname"), free the slots with
`Unsubscribe` to `/onvif/event_service/0…9` (#64).

0.6 **Bring the stack up and provision cam-03 locally.** Start the successor's units as its LAUNCH
Part B says. cam-03's MediaMTX path comes from the registry (`sync_mediamtx_paths.py`), but its
producer doesn't exist on this card: run `adapter/bin/provision-camera.sh` for cam-03 (enabled,
not started) until step 2.4 automates it. Verify with `bytesReceived` growing for cam-01, cam-02 (if
present) and cam-03, and a decoded cloud frame per camera, **before changing any code**.

0.7 **Baseline** the live cloud side the successor will change: `LastModified` and `CodeSha256` of
`list-cameras`, `get-hls-url`, `publish-cmd`, `set-camera-mode`, `clip-to-s3`, `record-clip`; the
live `index.html` md5 from CloudFront. Compare with the local tree; if the live code differs from
it, find out why before deploying anything.

0.8 Record contract rules 4 and 5b (§1.2) in the successor's `CLAUDE.md` (VMS plan §4.3 says they
"need to be recorded in the successor's CLAUDE.md as well"; still open), and the card-swap rules
from D1 in its `LAUNCH.md`.

### Phase 1: Bug fixes from VMS (R1, R2)

Only bugs that are live in the successor's code (§2). Each gets a FoundAndFixed entry (§6) and is
verified on this Pi, as VMS did when porting the other way: "verified as live here before being
fixed here".

**1.1 H.265 producer chain: #57, #58.** In `producer-lib.sh` `video_depay_chain`, append
`! h265timestamper ! identity single-segment=true` to the H.265 chain (VMS guide §21.2 has the
exact string). Without it, a camera whose H.265 leaves `h265parse` without DTS (cam-03) never
reaches KVS while the unit stays `active`.
*Verify:* cam-03 on H.265: no `Dropping frame with flag` in the journal, fragments `PERSISTED` at
the camera's GOP, LIVE HLS `CODECS="hvc1…"`, a clean decoded frame from the cloud URL. Then with
audio on: zero `0x30000005` rejects over at least 4 minutes (#58's 75 s window was too short).

**1.2 SDK patch 4: #62 (part 1).** Add VMS `LAUNCH.md` A3 patch 4 (whole-millisecond DTS in
`gstkvssink.cpp`) to the successor's A3, with its proof line
`grep -n "VMS patch 4" "$KVS_SDK/src/gstreamer/gstkvssink.cpp"`. Applied and rebuilt in step 0.4;
this task is the A3 text, for a fresh card.

**1.3 Audio for GUI-registered cameras: #59.** Three parts, all needed:
- discovery reads the microphone from the profile (VMS `onvif_discovery._audio`) and the card shows it;
- registration writes `audioCapable` (G.711 only);
- `stream-codec.py` prints `AUDIO_TRACK=pcma|pcmu` (from MediaMTX `tracks2[].codecProps.muLaw`) and
  `stream-channel.sh` gets the G.711 → AAC 8 kHz branch with the depayloader chosen by law (VMS
  `stream-channel.sh`). Keep the fakesink branch for video-only (#43).
*Verify:* cam-03 (mu-law) and cam-02 (A-law) with audio on: KVS LIVE HLS carries AAC, audible, zero
rejects.

**1.4 cam-01, bug fixes only (R2): #51, #61, #62 (part 2).**
- **#51** Port only the `publish_run` function into the successor's `publish-cam01.sh`, replacing
  both `exec gst-launch-1.0` lines: it polls MediaMTX `bytesReceived` for `cam01` every 10 s,
  SIGKILLs the pipeline after 30 s without growth, and exits 1 on any end of the pipeline. Do
  **not** take VMS's whole script (its `cam01.env`-driven pipeline is VMS's USB feature work).
  *Verify:* `kill -STOP` the gst-launch: killed within ~46 s, video back ~7 s later; a normal Stop
  ends `inactive`, result `success`.
- **#61** In `stream-cam01.sh`, at Start: compare `audioEnabled` with the tracks MediaMTX carries
  for `cam01`, restart `kvs-camera-publish` when they disagree (both ways), go video-only if the
  microphone track still doesn't appear. *Verify:* toggle audio on, Start: journal says
  "restarting kvs-camera-publish to match", tracks `['LPCM','H264']`, KVS LIVE HLS has audio.
- **#62 part 2** In cam-01's audio branch, `h264timestamper ! identity single-segment=true` after
  `h264parse`. *Verify:* zero rejects in 160 s with audio on.

**1.5 Admin status poll without AWS: #55.** `GET /api/cameras/<id>/status` checks the ID against
the supervisor's local registry cache (`outage_buffer.read_registry_cache()`), DynamoDB only when
there is no cache. *Verify:* `awsblock.sh` on; the admin page keeps working, status answers in tens
of ms.

**1.6 IR direction: #63.** Both GUIs: Day = `ON` (filter engaged, colour), Night = `OFF`. Status
line names the effect ("sent: day (colour, IR off)"). *Verify on cam-02* (cam-03 ignores the setting;
see #63): Night gives a grayscale picture.

**1.7 Test tooling: #54.** Add `63.176.0.0/12` to `awsblock.sh`, and the rule "check the detector's
endpoint with `getent ahosts <IOT_DATA_ENDPOINT>` before believing an outage test" to `OUTAGE.md`.

**1.8 Clip duration: #53 (open in VMS, the successor's code).** `clip-to-s3` writes `durationSec` =
the requested 45 s, not what `GetClip` returned (cam-02 H.265: a 9 s file listed as 45 s; cam-03
clips were full length). Fix: read the duration from the MP4's own `moov`/`mvhd`, next to the
existing `mp4_video_codec`; also apply in `record-clip`. Then find out why cam-02 returned 9 s
(GOP? fragment timing?), when cam-02 is back. Deploy with the §1.3 care: VMS uses this function.

**1.8b Cloud player that spins forever: #65.** The successor's page has the same player code. Port
from VMS `client/index.html` and `get_hls_url.py`:
- `since` (seconds since Start, on the page's clock) and `LIVE_REPLAY` from that moment in
  `get-hls-url`, so a quick Stop → Start can't get a session built from the previous run;
- `watchStalls` (new session after 20 s without progress; a message after three);
- a failed playlist or level load goes straight to a new session instead of `hls.startLoad()`;
- hls.js pinned instead of `@latest`.
Live view as `/hls`'s default comes with Phase 6. *Verify* as VMS did: the deployed page in headless
Firefox against the real Lambdas (a local proxy stands in for API Gateway; Firefox's
local-network-access prefs `network.lna.*` must be off in the test profile). Stop → Start 5 s apart
must show 503s until the new run's first fragment; stopping the producer outside the page must
end in "stream is not live", not a spinner.

**1.9 Not fixed in code, documented:**
- **#60** (cloud Start lost during an MQTT drop): open in VMS too. Record it; Phase 6's "the Pi
  didn't answer" state covers the user-facing half for Live.
- **#52** (Wi-Fi drops after group rekeying) and **#56** (USB 3 stick vs 2.4 GHz): environmental.
  Mention in the successor's LAUNCH/README hardware notes only if it runs on this Pi and network
  (Ethernet recommended).

### Phase 2: Unregister (R5)

VMS source: guide §16.2 "Unregistering a camera", `camera_control.is_unregistered`,
`app.py` `unregister_camera`, `agent.py` lines ~44–47, `outage_buffer.py` registry cache,
`sync_mediamtx_paths.py`, `event_watcher.load_cameras`.

2.1 `camera_control.is_unregistered(row)`: true when `unregisteredAt` is set.

2.2 `POST /api/cameras/<id>/unregister`, ONVIF cameras only (`mode == passthrough` and
`onvifHost`), in this order: set `unregisteredAt` (field-level `SET`); stop and **disable** the
producer (`set_stream(False)`, `set_boot_start(False)`); delete the MediaMTX path (HTTP `DELETE`,
#36), plus the `_live` path once Phase 6 exists. Idempotent: a second call returns the existing
mark.

2.3 **Every reader that acts on cameras skips such rows.** The VMS list, plus two successor-only
modules:
- admin GUI camera list and status poll (via the cache, #55);
- outage supervisor's registry cache (and with it the uploader);
- `event_watcher.py`;
- `sync_mediamtx_paths.py` (adds no path and keeps the row out of its local cache);
- `agent.py`: refuses everything except `stop`;
- **`rematch_cameras.py`**: must skip unregistered rows, or within 5 min it moves (= recreates)
  the deleted path;
- **`codec_caps.py` refresh**: skip;
- the two supervisor details from VMS §16.2: disarming a path with no configuration is a no-op,
  and the supervisor never creates a network camera's path.

2.4 **Re-register** from Discover. Match a scanned device by **`onvifEndpointRef` first**, then by
host (VMS matches by host only; the successor can do better). Label "Unregistered: was cam-03".
Re-register removes the mark (`REMOVE unregisteredAt`), adds the path back, enables the
`kvs-cam@` instance **without starting it**; cam-02 keeps its own unit as today.
**Also provision a camera that has no producer on this card** (D1: registered from the other
card). Re-register, for any camera other than cam-01/cam-02, runs `provision-camera.sh` when
`/etc/adapter/channels/<path>.env` is missing, enabled and not started. The admin GUI's status
column says "no producer on this Pi: Re-register" for such a camera instead of "inactive", and
its Start button is disabled; a cloud Start for it logs the same in `kvs-agent` (#37's lesson:
never let a missing unit look like a stopped one).

2.5 Cloud (D4): `list-cameras` leaves out rows with `unregisteredAt`, so the page shows them
nowhere without any page change; `publish-cmd` (except stop), `record-clip`, `get-hls-url`,
`set-camera-mode`, `set-camera-audio`, `set-camera-outage-buffer` answer 409 for them. The admin
GUI's camera table leaves them out too; Discover still labels them for Re-register.

*Verify:* unregister cam-03 → row marked; `kvs-cam@cam03` inactive and disabled;
`/v3/paths/get/cam03` 404; both GUIs hide it, `GET /cameras` doesn't list it; a direct
`POST /cameras/cam-03/start` answers 409, and an MQTT start sent anyway logs "ignoring" in `kvs-agent`;
`rematch_cameras.py --dry-run` skips it; `systemctl --user restart kvs-mediamtx` does not bring the
path back. Then re-register: path back, unit enabled and stopped, Start → `PERSISTED`, a decoded
cloud frame. Check in VMS's admin GUI (or `pir-list-cameras`) that VMS reads the same row the same way.

### Phase 3: Discovery, per-camera triggers, trigger test (R3, and bug #64)

VMS source: guide §9.3; `adapter/onvif_events.py` (whole module); `onvif_discovery.py`
(`web_url`, detection on the card); `app.py` `_registration_detection`, `_read_detection`,
`_record_detection`, `GET /detection`, `POST|GET|DELETE /detection/test`, `_record_trigger_test`;
`event_watcher.py`; FoundAndFixed #64.

3.1 Port `onvif_events.py`: `MODE_TOPICS` (known names), `_MODE_WORDS`, `topic_details`,
`detection_map`, `supported_modes`, `PullSubscription` (lifetime `PT600S`, **renew every 20 s**,
`PT2S` pulls with an 8 s deadline, 1 s pacing, hung pull = "nothing yet", **always unsubscribe**),
`parse_notification`, `is_on`.

3.2 Discovery card shows "Recording triggers: Manual, Cell grid" (from `GetEventProperties`), the
microphone (1.3), and a link to the camera's own web page where one answers (`web_url`, from
`GetNetworkProtocols`, linked only if a page answers HTML or 401/403).

3.3 Registration writes `detectionModes`, `detectionTopics` (mode → topic and on/off field) and
`webUrl`, field by field. `GET /api/cameras/<id>/detection` re-reads and records them for cameras
registered earlier.

3.4 **Test triggers** (`POST/GET/DELETE /api/cameras/<id>/detection/test`): listen up to 60 s for
every offered trigger at once, stop early when all fired, ignore the subscription's opening report
(cam-03 sends `Initialized` and backlog 0.3 s after subscribing), save `detectionTests` (mode →
seen, when, how many). The page polls locally, no AWS call (#55).

3.5 Rewrite `event_watcher.py` on `PullSubscription`: subscribe to the camera's own topic from
`detectionTopics`, read its own field, refuse (with a reason in the log) a mode the camera doesn't
list. Keep `ClipGate`, cooldown and `PUBLISH_DELAY_SEC` unchanged; `replay-gate.py` must still pass.

3.6 GUIs: both grey out modes not in `detectionModes`; next to the selector the note "Some triggers
fire only after they are switched on in the camera's own settings", with the `webUrl` link ("home
network only" in the cloud page); last test result ("Last test: Cell grid ✓"). The admin mode
route refuses unsupported modes. **New beyond VMS:** `set-camera-mode` refuses them too (VMS only
greys them out in its page; note it as a VMS follow-up, §9). `list-cameras` adds `detectionModes`,
`detectionTests`, `webUrl` (additive). Note: VMS's `CLAUDE.md` says `/cameras` stays identical to
the shared `list-cameras`; after this phase it no longer is (harmless, VMS uses its own copy),
so update that sentence in VMS.

*Verify:* cam-03 with motion detection ticked in its web UI: the card shows Cell grid only; Test
triggers sees Cell grid within ~20 s of walking past; Cell grid mode produces full-length clips
(~45 s); after 10 min of watching, one subscription throughout and all 10 subscription slots free
afterwards. If a leak happens during development: send `Unsubscribe` to
`/onvif/event_service/0…9` (#64). cam-02, when back: its Motion and Human topics found by name.

### Phase 4: Codec selector aligned with VMS (R4)

The successor already has a selector. What it must take from VMS (guide §21.5) is mainly *when a
switch counts as done*, because cam-03 showed that a camera's read-back can be stale
(reported H264 2880×1620 while sending H.265 2560×1440).

4.1 **Done = the wire.** Success only when MediaMTX receives the new codec and `bytesReceived`
grows (45 s budget). The successor's `set_codec` today raises when the camera's read-back doesn't
match: with cam-03 that is a false failure. Keep the read-back as information, not as the test.

4.2 **Find the encoder per request** from the Media2 profile whose `GetStreamUri` equals the
registered `rtspUrl` (path and query, credentials ignored; VMS `stream_profile`, `same_stream`).
The stored `videoEncoderToken` goes stale as soon as Phase 5 switches streams; update it after each
switch instead of relying on it.

4.3 **Keep the rest of the configuration** (VMS `plan_config`): change only the encoding, move other
fields to the nearest value the new codec allows, take the picture size from the wire.

4.4 No camera call when the wire already has the target codec.

4.5 Registry: `videoCodec`, `videoCodecCaps`, `videoEncoderToken`, `videoCodecProbe` with the
successor's existing meanings; `codec_caps.py` stays the owner of the caps logic. cam-01 shows
H.264 only, H.265 disabled with the reason as tooltip.

4.6 `GET /api/cameras/<id>/codec`, read once per table load, never polled (#55). Status line shows
what MediaMTX receives (codec and size), and the camera's claim only when it differs.

*Verify:* cam-03, both directions twice: camera accepts in ~1 s, video in MediaMTX after ~6–7 s,
producer persisting on the new chain within ~25 s, LIVE HLS opened afterwards plays the new codec,
KVS `MediaType` corrected by `stream-codec.py`. A clip across the switch is split in two
(`codec-switch` labels).

### Phase 5: Recording stream and frame rate (R6, first half)

VMS source: guide §21.6; `onvif_media2.py` `streams`, `fps_choices`, `fps_config`,
`with_credentials`; `app.py` `get_stream_choice`, `set_stream_choice`, `_set_camera_fps`,
`_registration_fps`, `_measure_fps`, `_verify_fps`, `_wait_for_new_session`.

5.1 `GET/POST /api/cameras/<id>/stream` with `{"stream": "main"|"sub"}` and/or `{"fps": 10|15}`;
any other fps is a 400. "Main" = the largest picture, not the first profile (profile names are the
vendor's). `onvif_media2.FPS_CHOICES = (10, 15)`; `GET` returns only those of the two the encoder
accepts, and the camera's current rate only when it is one of them (D5: no other rate is shown).

5.2 **Stream switch:** write `rtspUrl` first (a MediaMTX restart rebuilds the path from the
registry), patch the MediaMTX path to the other profile's URI with the registered credentials,
proof = MediaMTX's new session with video. Then record `videoCodec` from the wire (cam-03's sub
claims H.264 and sends H.265) and the new `videoEncoderToken`.

5.3 **Frame rate:** set on the encoder of the pulled stream, **keyframe interval kept in seconds**
(`GovLength` scaled; a KVS fragment is one keyframe interval). Proof = counted frames on the wire
(4 s of packet timestamps). Fewer frames than asked is reported, not rolled back.

5.4 Registration form offers stream and fps (10/15 only), **preselects 10 fps**. Existing cameras
keep their rate until someone chooses one; at a rate other than 10 or 15 the selector shows
"choose 10 or 15" (D5).

5.5 Audio: 8 kHz AAC (128 ms frames) satisfies guide §18.3's rule at both rates (frame interval
100 ms at 10 fps, 67 ms at 15). Zero rejects were measured at 15 fps (cam-02) and 20 (cam-03),
**not at 10**: measure it once with audio on.

5.6 Unregistered cameras: 409 ("Re-register it first").

*Verify (as VMS measured on cam-03):* 15 → 10 fps (keyframe interval halves in frames, stays the
same in seconds), main → sub (800×448, codec from the wire), sub → main, 10 → 15. Each with the
producer persisting afterwards and a decoded cloud frame.

### Phase 6: Live view always on the sub stream (R6, second half)

"Live view" covers both the admin GUI's local preview and the cloud page's live player. Recording
(manual, detection clips, outage buffer) keeps using the stream chosen in Phase 5.

**VMS built this on 2026-10-18** (VMS guide §21.7): `adapter/live_view.py`,
`adapter/bin/provision-live.sh`, `stream-codec.py --live`, `KVS_STREAM`/`LIVE` in
`stream-channel.sh`, the `camera_control.live_*` names, `liveRtspUrl` in
`sync_mediamtx_paths.py`, `previewPath` in the admin GUI, and `view=live` in `get_hls_url.py`.
Port 6.1 and the Pi half of 6.2 from there; they match the design below. **One difference:** VMS
ties the live producer to the recording producer (`Wants=`/`PartOf=`: Start and Stop drive both),
so in VMS live view still needs Start. D3 here makes Live independent of Start, which is the
deadline, keep-alive and `live`/`live-stop` part below; VMS has none of it to port. Two VMS
findings to keep: the live path must pull continuously, not on demand, because the producer's
`stream-codec.py` needs the path's tracks before anyone reads it; and each `kvs-cam@` instance
needs its own watchdog FIFO label, since all instances share `/run/vms-producer`.

**6.1 Local preview (LAN only, no cost).**
- When the recording stream isn't the sub stream and the camera has one, add a second MediaMTX path
  `<path>_live` (e.g. `cam03_live`) with the sub URI, pulled continuously, no recording. Not
  `sourceOnDemand`: the live producer's `stream-codec.py` waits for the path's tracks, which an
  on-demand path has only once someone reads it (found building it in VMS).
- Registry: `liveRtspUrl` (sub URI with credentials, like `rtspUrl`; absent when live = recording
  stream). `sync_mediamtx_paths.py` creates/removes the `_live` path from it.
- The admin preview plays `_live` when present, otherwise the recording path.
- **Audit everything that lists MediaMTX paths or maps a path to a camera** before adding the first
  `_live` path: `mediamtx_api` (`/v3/paths/list`, `flowing_paths`), the outage supervisor (must
  never arm a `_live` path), `rematch_cameras.py` (must move both paths), Unregister (deletes both),
  `camera_control.mediamtx_path_name` callers.
- Phase 5's stream switch updates `liveRtspUrl`: switching recording to sub removes the `_live` path.

*Verify:* cam-03 recording on main: preview shows 800×448 from `cam03_live`; the recording path's `bytesReceived` unaffected; outage buffer
records only `cam03`. Check the camera copes with two RTSP sessions (main + sub) for an hour.

**6.2 Cloud live view (D2, D3).**
- KVS stream `<cameraId>-live` (e.g. `cam-03-live`), created when a camera first gets a
  `liveRtspUrl`, with a short data retention (1 h; KVS HLS needs retention > 0, confirm on the
  first test). Registry: `liveStream` (name), absent when live = recording stream.
- Producer: system unit template `kvs-live@.service` (generated by A8, like `kvs-cam@`), running
  `stream-channel.sh` against `rtsp://127.0.0.1:8554/<path>_live` into `<cameraId>-live`, through
  `producer_run` (#44/#46), audio following `audioEnabled`.
- **It must stop by itself** (the cost rule "never leave a producer running unattended"): the agent
  writes a deadline (now + 5 min) to `/run/vms-live/<path>` on every `live` command; the producer's
  watchdog loop stops the pipeline and exits **0** when the deadline passes (so
  `Restart=on-failure` leaves it stopped). Don't use `RuntimeMaxSec=` for this: systemd counts its
  timeout as a failure and `Restart=on-failure` restarts it. `agent.py` stops all `kvs-live@*`
  at its own start.
- Control: new MQTT commands `live` and `live-stop` in `agent.py` (refused for unregistered
  cameras). Check first that VMS's `agent.py` ignores an unknown command cleanly, since the two Pis
  share `adapter-01` (VMS plan §4.4).
- `get-hls-url` takes `view=live`: stream `liveStream` when set, otherwise the recording stream (as
  today). Clips and ON_DEMAND playback always use the recording stream.
- Page (D3): a **Live** control separate from Start/Stop. It sends `live`, polls as after Start
  today (`liveWaitUntil`, 45 s), re-sends `live` every 2 min while the player is visible
  (`visibilitychange`), sends `live-stop` on close (best effort; the deadline is the guarantee).
  When nothing arrives within 45 s, say "the Pi didn't answer: it may be offline from AWS" instead
  of "not available" (#60).
- Camera without a sub stream, or recording already on sub: Live uses the recording stream; if that
  producer isn't running, Live starts it with the same deadline rule (decide in D3 whether that is
  wanted, since it then also records).

*Verify:* cam-03 recording on main and stopped: Live in the cloud page plays 800×448 within ~15 s;
closing the page stops `kvs-live@cam03` within the deadline (journal shows the deadline exit,
unit `inactive`, result `success`); a page left open keeps it running; with recording running on
main, Live shows the sub picture while `GetClip` for the same minutes returns main resolution. KVS
cost check: `cam-03-live` ingest only during viewing.

### Phase 7: Documentation and final regression

7.1 FoundAndFixed: one entry per ported bug (§6), each "Found in VMS as #N", with the verification
done here. Overview table updated.

7.2 Guide (successor's `Demo-AWS-Video-revCosts4.md`; its §-numbers differ from VMS's): unregister
(§16-equivalent), triggers and testing (§9-equivalent), codec switch (§21), stream/fps and live
view (new subsection after §21), with the measurements from this round.

7.3 `CLAUDE.md`: unregister and its reader list (with `rematch_cameras.py`), `onvif_events.py`,
the `_live` path and `kvs-live@` (system unit), the live deadline rule, the contract rules (0.8),
and that the board is shared with VMS by SD-card swap (D1: what crosses, and the swap rules).

7.4 `LAUNCH.md`: A3 patch 4 with the plugin-only rebuild; A8 generates `kvs-live@.service` and
`RuntimeDirectory` for its deadline; a "card swap" section (D1: stick uploads finished, producers
stopped; after the swap, cameras registered from the other card need Re-register; subscription-slot
recovery for #64).

7.5 README: feature list (unregister, trigger test, stream/fps, live on sub).

7.6 Final regression on the successor stack: cam-01 Start/Stop with audio on and off, cam-02 and
cam-03 Start → cloud frame, a detection clip, a manual clip, an outage test with `awsblock.sh`
(clip backfilled), unregister/re-register, codec and stream switches. Then the VMS-side check: VMS's
page (`d10sy0s307vyid.cloudfront.net`) still lists cameras and plays clips.

---

## 5. Verification toolbox (commands)

```bash
# media actually flowing (never trust `ready` or is-active)
curl -s localhost:9997/v3/paths/get/cam03 | jq '.bytesReceived, .tracks'      # twice, must grow
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam03
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/cam03 -frames:v 1 /tmp/f.png  # look at it
# producer health
journalctl -u kvs-cam@cam03 -f | grep -E 'PERSISTED|Dropping frame|0x30000005|pipeline ended'
# cloud: get-hls-url (or GetHLSStreamingSessionURL) -> ffprobe / one frame from the URL
# ONVIF subscriptions: one at a time, unsubscribed after (event watcher journal)
# pages: headless Firefox via Marionette (VMS PIR plan Phases 8 and 11 show how)
```

---

## 6. VMS bug triage (#51–#65) and number mapping

| VMS # | Bug | Relevant to successor? | Phase | Successor # |
|---|---|---|---|---|
| 49 | USB config rollback read backups too late | No (VMS-only USB feature) | – | – (already "VMS only" there) |
| 51 | cam-01 publisher hangs silently, stays `active` | **Yes**, `exec gst-launch` there | 1.4 | |
| 52 | FRITZ!Box drops Wi-Fi after rekey bursts | Environmental; same network | 1.9 (docs) | |
| 53 | H.265 detection clip 9 s listed as 45 s | **Yes**, the successor's `clip-to-s3` | 1.8 | |
| 54 | `awsblock.sh` missed the IoT endpoint's range | **Yes** | 1.7 | |
| 55 | DNS failure froze the admin page (status poll on AWS) | **Yes** | 1.5 | |
| 56 | USB 3 stick deafens 2.4 GHz devices | Hardware note only (no Pico there) | 1.9 (docs) | |
| 57 | kvssink dropped every H.265 keyframe (no DTS) | **Yes** | 1.1 | |
| 58 | H.265 + audio: video clock 1000 h ahead | **Yes** (with 57; diagnosis corrected by 62) | 1.1 | |
| 59 | GUI-registered camera could never record audio | **Yes** | 1.3 | |
| 60 | Cloud Start lost during MQTT drop | Yes, open; documented, UI state in 6.2 | 1.9, 6.2 | |
| 61 | cam-01 audio on: Start didn't restart the publisher | **Yes** (bug fix on cam-01) | 1.4 | |
| 62 | Audio frames lost: ms tie in SDK merge; invented DTS on cam-01 | **Yes** | 1.2, 1.4 | |
| 63 | IR ON/OFF backwards in both GUIs | **Yes** | 1.6 | |
| 64 | Detection never fired on cam-03; watcher leaked subscriptions | **Yes** | 3 | |
| 65 | Cloud player spun forever (stale session after Stop → Start, no player watchdog) | **Yes**, same page code | 1.8b | |

Bugs found during this round get their own successor numbers; when one also applies to VMS, add it
to §9.

---

## 7. Registry fields: additions and the data contract

All additive; no existing field changes meaning. VMS reads none of the new successor-only fields.

| Field | Written by | Meaning | VMS |
|---|---|---|---|
| `unregisteredAt` | admin Unregister | soft mark, every reader skips | same field, same meaning |
| `audioCapable` | registration (now for ONVIF too) | camera has a G.711 microphone | same |
| `detectionModes`, `detectionTopics`, `detectionTests`, `webUrl` | registration, `GET /detection`, Test triggers | as VMS guide §9.3 | same |
| `videoCodec`, `videoCodecCaps`, `videoEncoderToken`, `videoCodecProbe` | codec and stream switches | the successor's existing meanings | VMS writes them with these meanings |
| `rtspUrl` | registration, stream switch | the stream that is **recorded** | same meaning (VMS pulls it) |
| `liveRtspUrl` *(new)* | stream switch / registration | sub stream for live view, only when ≠ `rtspUrl` | ignored |
| `liveStream` *(new)* | when the `-live` KVS stream is created | KVS stream name for live view | ignored |

Two consequences to keep in mind: the `cameras` table is shared, so **unregistering in either
project hides the camera in both**, and a stream switch in either project changes what the other
records next time it runs. `list-cameras` must never return `rtspUrl` or `liveRtspUrl`
(credentials).

---

## 8. Out of scope (deliberately)

- **New cam-01 features (R2):** VMS guide §22.3–§22.8, i.e. USB scan (`usb_camera.py`,
  `/api/usb-cameras*`), apply/backup/revert (`configure-camera.sh`), live v4l2 controls, mic level,
  `cam01.env`-driven pipeline rewrite, registry hardware facts from the scan. Only the Phase 1.4 bug
  fixes.
- **PIR** (`pir_*`, `observe_pir.py`, mosquitto, the Pico), the `pir-` cloud resources and the PIR
  ring in RAM.
- SSM `credentialRef` (Phase 2 of credential storage), a separate IoT Thing per Pi.

---

## 9. Follow-ups for VMS (after the successor round)

- `pir-set-camera-mode` should refuse modes outside `detectionModes` (3.6).
- Re-register could match by `onvifEndpointRef` first (2.4); VMS has no endpoint refs yet.
- VMS `CLAUDE.md`: drop "`/cameras` stays identical to the shared `list-cameras`" (3.6).
- `PIR-MQTT-VMS-PI4.md` §4.2: add rows for the new successor fields (`liveRtspUrl`, `liveStream`)
  and the `-live` KVS streams; record that #53's fix landed in `clip-to-s3`.
- `PIR-MQTT-VMS-PI4.md` §4.3–§4.4: rule 4 now applies to cam-01 only (`list-cameras` is the
  successor's own); drop the coupling "an unregistered camera stays visible in the successor's page".
  `agent.py`'s refusal stays, for the shared MQTT session.
- The other half of D1's card swap: a camera the successor registers has no producer on the VMS
  card. VMS's Re-register needs the same "provision when the channel file is missing" (2.4), and
  VMS's status column the same "no producer on this Pi" state.
- VMS LAUNCH/README: the card-swap rules (stick uploads finished, producers stopped) and the
  subscription-slot recovery (#64), since a swap can leave cam-03's slots used by the other card.
- VMS FoundAndFixed #53: link to the successor entry when fixed.

---

## 10. Session checklist

- [ ] VMS card: 0.1 VMS committed and pushed, uploads finished, producers stopped, card swapped
- [ ] Successor card: 0.2 local work checked, pulled, plan copied, VMS cloned read-only · 0.3 §2 and OS versions (timestampers!) · 0.4 patch 4 rebuilt · 0.5 event watcher disabled · 0.6 stack up, cam-03 provisioned, video flowing · 0.7 cloud baseline · 0.8 contract and swap rules written down
- [ ] 1.1 H.265 chain · 1.2 patch 4 · 1.3 ONVIF audio · 1.4 cam-01 (#51, #61, #62) · 1.5 status poll · 1.6 IR · 1.7 awsblock · 1.8 clip duration · 1.8b player (#65) · 1.9 docs
- [ ] 2 Unregister / Re-register, all readers incl. `rematch_cameras.py`, cloud 409s
- [ ] 3 Triggers per camera, Test triggers, watcher on `PullSubscription`, both GUIs, `set-camera-mode` check
- [ ] 4 Codec switch done-by-wire, encoder per request, `plan_config`
- [ ] 5 Stream main/sub, fps 10/15, registration form, audio at 10 fps measured
- [ ] 6.1 Local preview on `_live` path · 6.2 cloud Live on `<id>-live` with deadline stop
- [ ] 7 FoundAndFixed, guide, CLAUDE.md, LAUNCH, README, final regression, VMS page still works
