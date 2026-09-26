# FoundAndFixed.md — every defect found in this project, and what fixed it

The single place where this project's bugs are written up: what broke, how it was found,
why it happened, and what fixed it. Other documents keep only the rule or decision a bug
produced, plus a reference to it here — **`FoundAndFixed.md #N`**. Numbers are permanent:
new entries are appended, and an entry is never renumbered or reused.

**In scope:** defects in this project's own code, configuration, build and setup, and
documentation (an instruction that fails when followed is a defect too). **Not here,
deliberately:** quirks of third-party components, which this project works around but
cannot fix (the camera firmware in `Camera-Features.md` and `AUDIO.md` §3, the `kvssink`
buffer behaviour in guide §16.3c), withdrawn *predictions* (`AUDIO.md` §7), and generic
advice (guide §15 "Known traps").

> **Provenance.** This inventory was written in the successor repository
> (`VideoSafeZone`, templated from this one) while bringing the project up on a second
> Pi, and was brought back here on 2026-09-26. Entries #1–#23 are shared history and were
> fixed in this repository first. #24–#30 arrived with the path-portability commit.
> #31, #32, #35, #36, #37, #39 and #40 were verified as live defects *here* and fixed
> here on 2026-09-26 — each with the verification recorded in the entry.
>
> **Two entries describe fixes this repository has NOT taken**, and say so in place:
> **#25**'s deployment-identity config layer (`adapter/config.py`,
> `/etc/adapter/adapter.env`) exists only in the successor; this repository still carries
> AWS identifiers as literals. **#31**'s password rotation is outstanding in both, since
> the credential is in shared git history.

---

## Overview

40 defects, from the first build (2026-08-20) to the move onto a second Pi (2026-09-24/25).

| # | Defect | Area | Found | Status |
|---|---|---|---|---|
| 1 | The Pi crashed outright during the SDK build — three mechanisms | build / platform | 2026-08-20 | fixed |
| 2 | Nested `--parallel` in the SDK ignores `-j1` / `PARALLEL_BUILD` | build | 2026-08-20, again 2026-09-24 | fixed (patch 1) |
| 3 | OpenSSL's test submodules crashed the Pi during clone | build | 2026-08-20 | fixed (patch 2) |
| 4 | GCC 14 rejects the SDK's implicit `pthread_getname_np` | build | 2026-08-20 | fixed (patch 3) |
| 5 | The camera chain was never persisted — producer crash-looped silently | systemd | 2026-08-20 | fixed |
| 6 | A retained Last Will gets the MQTT CONNECT rejected | control plane | 2026-08-20 | fixed |
| 7 | `agent.py` built `kvs-cam-02.service`, silently did nothing for cam-02 | control plane | 2026-08-20 | fixed |
| 8 | A Lambda exception reached the browser as a bare `NetworkError` | cloud client | 2026-08-20 | fixed |
| 9 | `mediaSourceRequiresReset` on every player reload | cloud client | 2026-08-20 | fixed |
| 10 | Player retried forever after Stop, with no "stopped" state | cloud client | 2026-08-20 | fixed |
| 11 | Browsers kept serving a stale client after deploys | cloud client | not recorded | fixed |
| 12 | A system unit declared a dependency on a user unit | systemd | not recorded | fixed |
| 13 | `v4l2h264enc` negotiated Baseline — black video in browsers | media pipeline | not recorded | fixed |
| 14 | HLS sessions died after exactly five minutes | cloud client / Lambda | 2026-09-06 | fixed |
| 15 | 48 kHz audio silently lost more than half its frames (shared DTS) | audio | audio work | fixed |
| 16 | AAC over RTSP became LATM — ingested, then refused at playback | audio | audio work | fixed |
| 17 | A global `input` CSS rule broke the audio checkboxes | cloud client | audio work | fixed locally; root rule open |
| 18 | The guide's outage test (§10.2) could not detect anything | docs / test | outage work | fixed (`awsblock.sh`) |
| 19 | The §17/M1 spec's MPEG-TS recording would have been mute | docs / spec | outage work | avoided (fMP4) |
| 20 | Segment names parsed as UTC — rolling buffer grew without bound | outage buffer | outage work | fixed |
| 21 | Supervisor blocked 45 min on AWS — during the outage it watches for | outage buffer | outage work | fixed |
| 22 | Recovery on one probe made the supervisor flap | outage buffer | outage work | fixed |
| 23 | The reachability probe cost twice its timeout | outage buffer | outage work | fixed |
| 24 | LAUNCH A3's patches were not actionable; patch 3 impossible up front | docs / build | 2026-09-24 | fixed |
| 25 | Absolute `/home/vladimir/MyProjects/VMS` paths broke on the move | code / docs | 2026-09-24 | fixed |
| 26 | The MediaMTX tarball overwrites the project's `mediamtx.yml` | docs / setup | 2026-09-25 | fixed |
| 27 | The documented venv package list was incomplete | docs / setup | 2026-09-25 | fixed |
| 28 | Troubleshooting advised `-j2` for the OpenSSL build crash | docs | 2026-09-25 | fixed |
| 29 | The guide never downloaded `AmazonRootCA1.pem` | docs / setup | 2026-09-25 | fixed |
| 30 | Six unit files existed only on the old Pi; `kvs-cam@` sketch incomplete | docs / systemd | 2026-09-25 | fixed |
| 31 | cam-02's RTSP credentials committed in `mediamtx.yml` | security | 2026-09-25 | removed; **password rotation pending** |
| 32 | API-added MediaMTX paths vanished on every MediaMTX restart | MediaMTX | 2026-09-25 | fixed |
| 33 | Journal lived in RAM only; the first fix didn't take on Pi OS | platform / docs | 2026-09-25 | fixed |
| 34 | Passwordless sudo assumed; the check was fooled by a cached password | platform / docs | 2026-09-25 | fixed |
| 35 | MediaMTX's generated `auto.crt`/`auto.key` were not git-ignored | repo hygiene | 2026-09-25 | fixed |
| 36 | Admin GUI rollback deleted a path with `POST` — a 404, silently | admin GUI | 2026-09-25 | fixed |
| 37 | Start/Stop and status silently no-op'd for GUI-registered cameras | control plane | 2026-09-25 | fixed |
| 38 | Instructions that failed when followed literally (docs consistency review) | docs | 2026-09-25 | fixed |
| 39 | Outage supervisor crash-looped on a Pi without the USB stick | outage buffer | 2026-09-25 | fixed |
| 40 | cam-01 never published: `gstreamer1.0-rtsp` missing from the setup lists | docs / setup | 2026-09-25 | fixed |

### What they have in common

- **Most were silent.** #5, #7, #13, #15, #16, #18, #20, #21, #32, #36, #37 and #39 all
  passed "it ran without errors". The recurring cause is a tool that reports success on
  the wrong question. `systemctl is-active` says `inactive` for a unit that doesn't exist
  (#7, #37). `check=False` swallows a failure (#7). `ffmpeg` plays what a browser won't
  (#13). KVS ingest accepts what KVS playback refuses (#16). `sudo -n` passes on a cached
  password (#34). CLAUDE.md "Verifying changes" is the countermeasure.
- **Ingest is more permissive than playback** (#13, #15, #16, #19): verify at the
  consuming end — a decoded frame, a browser, `GetHLSStreamingSessionURL` — not at
  ingest.
- **Two copies of one fact drift** (#7, #25, #30, #31, #37): unit names, paths, endpoints
  and camera addresses now each have one source (CLAUDE.md "Paths", the registry,
  `/etc/adapter/`).
- **Instructions are code too.** #24–#30, #33–#34, #38 and #40 were wrong or missing
  documentation. They surfaced only when the setup was followed literally on a fresh Pi.
- **A 4 GB Pi building C/C++** (#1–#4) needs memory discipline at every level of a
  nested build, not just the top one.

---

## Build and platform

### #1 — The Pi crashed outright during the SDK build — three mechanisms

**Found:** 2026-08-20, over five failed build attempts. **Referenced from:** guide §1.4, §4.

A Pi 4B running VS Code Remote-SSH's server (1.3+ GB of Node processes) plus a
from-source C++ build is oversubscribed on 4 GB. The KVS SDK build did not just fail — it
took the whole Pi down, by three distinct mechanisms:

- **Kernel OOM with no early intervention.** An `earlyoom` setting of `-s 50` (act only
  once swap is *also* below 50 % free), tried to rescue large compiles, let available
  memory crater from 64 % to 12 % in one 60-second window before it acted — a full crash.
- **Swap thrashing on the SD card.** Once, mid-build: no OOM-killer log, no panic, a
  silent cutoff correlated with `brcmfmac` (WiFi) SDIO timeouts just before it.
- **Build load on the interrupt cores.** This kernel isolates cores 1–2
  (`isolcpus=1,2 irqaffinity=0,3`); an unpinned build competed with WiFi interrupt
  servicing. `systemd-run --property=AllowedCPUs=` silently did nothing (cpuset not
  delegated to user cgroups).

Firmware was also found over a year out of date (EEPROM 2025-05-08 vs 2026-05-17).

**Fix:** guide §1.4's four hardening steps — `earlyoom -m 20 -s 95` preferring compiler
processes; a low-priority disk swapfile with `vm.swappiness=10`; EEPROM update; `taskset
-c 1,2` for the build. The final build ran with zero crashes and zero `earlyoom`
interventions. (On the second Pi, `isolcpus` is not set — `detect-hw.sh --print`
reports it — so the pinning protects nothing there.)

### #2 — Nested `--parallel` in the SDK ignores `-j1` and `PARALLEL_BUILD`

**Found:** 2026-08-20; recurred 2026-09-24 on the second Pi. **Referenced from:** guide
§4.2, LAUNCH.md A3, README §3.

`CMake/Utilities.cmake`'s `build_dependency()` builds each dependency with `cmake --build
. --parallel`, no job count — a bare `--parallel` becomes `-j$(nproc)` and overrides any
inherited `MAKEFLAGS`. `-DPARALLEL_BUILD=OFF` fixes the top-level copy (log4cplus dropped
from **370 → 14** concurrent tasks). But OpenSSL is built by a **nested** vendored copy,
`dependency/libkvscproducer/kvscproducer-src/CMake/Utilities.cmake`, which hardcodes
`--parallel` with no option at all: OpenSSL still spawned **398 tasks** and crashed the Pi.

**Recurrence, 2026-09-24:** on the second Pi the patch was not applied (#24), so OpenSSL
again compiled one job per core; `earlyoom` (configured by #1 to prefer compilers) sent
SIGTERM to every `cc1` at once — `build.log` showed a burst of `cc: fatal error:
Terminated signal terminated program cc1` and `EXIT_CODE=1`. Nothing was wrong with the
code.

**Fix:** patch 1 — `sed -i 's/--build \. --parallel/--build ./'` on the nested file
(LAUNCH.md A3). With it the 2026-09-25 build ran at 14 tasks, zero `earlyoom` kills.

### #3 — OpenSSL's test submodules crashed the Pi during clone

**Found:** 2026-08-20. **Referenced from:** guide §4.2, LAUNCH.md A3.

OpenSSL's `ExternalProject_Add` also fetched four large optional submodules (`boringssl`,
`krb5`, `pyca-cryptography`, `wycheproof` — fuzzing and test vectors, not needed for
`make install_sw`). Cloning `boringssl` alone caused repeated system crashes during
clone/checkout — most likely sustained SD-card I/O pressure, the same failure mode as
#1's swap thrashing.

**Fix:** patch 2 — `GIT_SUBMODULES ""` in the `project_libopenssl` block.

### #4 — GCC 14 rejects the SDK's implicit `pthread_getname_np`

**Found:** 2026-08-20. **Referenced from:** guide §4.2, LAUNCH.md A3.

`Thread.c` in kvspic uses `pthread_getname_np`, a GNU extension that needs `_GNU_SOURCE`
defined before `<pthread.h>`. Older GCC only warned about the implicit declaration;
**GCC 14 made it a hard error**, so the build fails outright on Debian trixie.

**Fix:** patch 3 — `add_definitions(-D_GNU_SOURCE)` (under `UNIX AND NOT APPLE`) in
kvspic's `CMakeLists.txt`, once, globally. Its file only exists after the configure step
has downloaded kvspic (#24).

### #33 — The journal lived in RAM only, and the first fix didn't take on Pi OS

**Found:** 2026-09-25. **Referenced from:** LAUNCH.md A1.

The second Pi kept the journal in `/run/log/journal` only. Every reboot lost all logs —
so the first failed build (#2) left no trace across the reboot — and `journalctl --user
-u <unit>` printed "No journal files were found" for **every** user unit, which every
`--user -u` check in LAUNCH.md depends on.

The first fix (`mkdir /var/log/journal`) did nothing: Raspberry Pi OS ships
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` with
`Storage=volatile` (to spare the SD card), which makes journald ignore the directory.
(A related misreading: an empty `/var/log/journal` was first reported as missing.)

**Fix:** a later drop-in, `/etc/systemd/journald.conf.d/90-vms-persistent.conf`, with
`Storage=persistent`, `SystemMaxUse=200M`, `SystemMaxFileSize=20M`, then `journalctl
--flush`. Verified: `system.journal` and `user-1000.journal` on disk, the pre-switch boot
still listed, `journalctl --user -u` works.

### #34 — Passwordless sudo assumed; the check was fooled by a cached password

**Found:** 2026-09-25. **Referenced from:** LAUNCH.md A8.

`agent.py`/the admin GUI run `sudo systemctl start|stop` and `sudo provision-camera.sh`
with no terminal. The code and docs assumed Raspberry Pi OS grants passwordless sudo; the
current image grants `(ALL : ALL) ALL` with a password. Worse, LAUNCH A8's check `sudo -n
true` **passed**: this image sets `Defaults timestamp_type=global`, so a password typed
anywhere in the last ~15 minutes satisfies `sudo -n` in every session — services
included. Start/Stop would have worked just after any `sudo`, and failed otherwise.

**Fix:** `/etc/sudoers.d/020_vms-adapter`, NOPASSWD for exactly `systemctl
start|stop kvs-cam(@cam)?NN.service` and `provision-camera.sh cam-NN camNN` (regex
arguments, sudo ≥ 1.9.10), validated by `visudo` before install. The check is now `sudo
-k -n` (ignores the cache). Verified allowed and refused cases, including a smuggled
second unit.

---

## Media pipeline and audio

### #5 — The camera chain was never persisted; the producer crash-looped silently

**Found:** 2026-08-20. **Referenced from:** guide §2.8, §7.1, LAUNCH.md Part B.

Hours into later phases, the stream was dead — not downstream: `camera-init.sh` →
MediaMTX → `publish-cam01.sh` had only ever been run by hand, never made into units. The
cloud-facing pieces (`kvs-cam01.service`, the agent) were persisted and auto-recovering,
so `kvs-cam01.service` crash-looped (`Restart=on-failure`) against a 404 on
`rtsp://127.0.0.1:8554/cam01`, with nothing to show for it. A later version of LAUNCH
Part B repeated it: `kvs-camera-publish` was missing from the launch list and everything
else came up "active".

**Fix:** user units for all three pieces (`kvs-camera-init`, `kvs-mediamtx`,
`kvs-camera-publish`), in the launch list, and the rule that `is-active` is not proof —
verify media (LAUNCH.md Part C).

### #13 — `v4l2h264enc` negotiated Baseline; browsers rendered black

**Found:** date not recorded. **Referenced from:** README "Results", CLAUDE.md, guide
§2.6, `AUDIO.md` §5, `OUTAGE.md` §4.1.

`/dev/video11`'s `h264_profile` control defaults to High, but GStreamer's `v4l2h264enc`
negotiated **Baseline** — nothing in the pipeline asked for it. `ffmpeg`/`ffprobe` played
the stream; browsers (MSE) rendered **black**. The guide's Checkpoint 1 even recorded
`Video: h264 (Baseline)` and called Baseline "the safer choice" — the note that let the
bug pass.

**Fix:** `profile=(string)high` in the encoder caps in `publish-cam01.sh`, and the rule
to finish every pipeline check in a browser with a decoded frame (CLAUDE.md "Verifying
changes"). Re-verified 2026-09-25 on the second Pi: `H.264 High, level 4.0`.

### #15 — 48 kHz audio silently lost more than half its frames (shared DTS)

**Found:** during the audio work (`AUDIO.md`). **Referenced from:** guide §18.3,
`AUDIO.md` §2/§4.1, README, CLAUDE.md.

*Symptom:* cam-02 audio at 48 kHz — fragments persisted, both tracks in the HLS
manifest, `ffprobe` happy — and over half the audio missing: **15 kb/s delivered of
32 kb/s sent**, `kvssink` logging `0x30000005` at 1.65/s.

*Cause* (`gstkvssink.cpp`, `gst_kvs_sink_handle_buffer`): GStreamer audio buffers carry no
DTS, and `kvssink` synthesises one as `last_dts + 40 ms` from a counter **shared with the
video track**. With more than one audio frame between two video frames, the synthesised
timestamps overrun the next video DTS, go backwards, and are rejected
(`STATUS_CONTENT_VIEW_INVALID_TIMESTAMP`). `voaacenc` emits 1024-sample frames, so frame
duration is `1024/rate`: 21 ms at 48 kHz against a 66.7 ms video frame.

*Diagnosed* by A/B-ing reject rates against a prediction derived from the source —
`identity silent=false` is a no-op in this GStreamer build and `python3-gi` is not
installed, so DTS values could not be read directly.

| Rate | Frame duration | Rejects | Delivered (of 32 kb/s) |
|---|---|---|---|
| 48 kHz | 21 ms | **1.65 /s** | 15 kb/s — over half lost |
| 8 kHz (native) | 128 ms | **0** | 27.8 kb/s |

**Fix:** the rule *audio frame duration must exceed the video frame interval* — below
~15.4 kHz at 15 fps. cam-02 runs 8 kHz, cam-01 16 kHz (64 ms: marginally inside, measured
zero rejects). Re-count rejects whenever a camera's frame rate changes (guide §18.3).

### #16 — AAC over RTSP became LATM: ingested, then refused at playback

**Found:** during the audio work. **Referenced from:** `AUDIO.md` §2/§4.2, README,
CLAUDE.md, guide §18.4 (the rule; `stream-cam01.sh`/`publish-cam01.sh` comments retell the story).

*Symptom:* cam-01 with AAC encoded in the publisher and passed through at the producer —
the obvious design, one encode. `kvssink` ingested it (0 rejects, 30 fragments/min), then
playback failed: `InvalidCodecPrivateDataException: AAC CPD must be of length 2 or 5, but
was 4`.

*Cause:* `rtspclientsink` payloads AAC as MPEG-4 **LATM**, and the LATM round-trip
re-wraps the AudioSpecificConfig into the 4-byte `14081fe0` (`channelConfiguration=0` plus
trailing bits) instead of the canonical 2 bytes. The payloader is a per-pad property, not
settable from `gst-launch`; a caps filter after `aacparse` didn't change it (MediaMTX still
reported `MPEG-4 Audio LATM`).

**Fix:** never send AAC over RTSP. `publish-cam01.sh` sends LPCM (16 kHz mono S16BE,
loopback only); `stream-cam01.sh` encodes (`rtpL16depay ! audioconvert ! voaacenc !
aacparse`) so `voaacenc`'s own `codec_data` reaches `kvssink`. Encoding at the producer
also keeps both tracks on one RTSP timeline for A/V sync.

---

## Control plane and systemd

### #6 — A retained Last Will gets the MQTT CONNECT rejected

**Found:** 2026-08-20. **Referenced from:** guide §7.2.

`retain=True` on the agent's Last Will made AWS IoT drop the connection at CONNECT —
`AWS_ERROR_MQTT_UNEXPECTED_HANGUP`, no CONNACK error code — while the same certificate,
policy and topic worked for a normal `publish()`. IoT Core evidently authorizes retained
LWTs more strictly than regular publishes.

*Isolated* by stripping the connection to nothing (no Will, no subscribe), then adding
pieces back one at a time until the failing one was obvious; `awscrt.io.init_logging(
awscrt.io.LogLevel.Debug, 'stderr')` is what surfaces the hang-up's context.

Found alongside: `conn.publish(...)` returns a **tuple** `(future, packet_id)` in
`awsiotsdk` 1.31.0 / `awscrt` 0.36.1 — `.result()` without `[0]` silently swallowed
publish errors.

**Fix:** `retain=False` on the Will (enough — subscribers still see it fire), and `[0]`
before `.result()`.

### #7 — `agent.py` built `kvs-cam-02.service` and silently did nothing for cam-02

**Found:** 2026-08-20. **Referenced from:** guide §16.3(a), CLAUDE.md,
`camera_control.py`.

`agent.py` built the unit name as `f"kvs-{camera}.service"`, which for `cam-02` gave
`kvs-cam-02.service` — a unit that doesn't exist (the unit is `kvs-cam02.service`).
`systemctl` exited non-zero, but it was called with `check=False`, so the failure was
swallowed and the API reported success. Caught only by reading `journalctl` and seeing
the wrong unit name in the logged `sudo` command.

**Fix:** `camera_control.mediamtx_path_name()`/`unit_name()` as the one place the
`cam-02` → `cam02` conversion happens. (Its template-unit blind spot is #37.)

### #12 — A system unit declared a dependency on a user unit

**Found:** date not recorded. **Referenced from:** CLAUDE.md.

A templated system unit (`kvs-cam@.service`) declared `Requires=kvs-mediamtx.service`,
which is a **user** unit. System and user units belong to independent systemd instances,
so the system manager could not find it: "Unit not found".

**Fix:** drop the cross-manager dependency. A unit in one manager can never
`Requires=`/`After=` a unit in the other.

### #30 — Six unit files existed only on the old Pi; the `kvs-cam@` sketch was incomplete

**Found:** 2026-09-25. **Referenced from:** LAUNCH.md A8, guide §16.6.

LAUNCH Part B enabled units that nothing in the docs created. Only five units were
written out in the guide; `kvs-cam02`, `kvs-agent`, `onvif-admin`, `kvs-event-watcher`,
`kvs-outage-buffer` and `kvs-outage-uploader` existed only as files on the old Pi. The
guide's `kvs-cam@.service` sketch also lacked `User=` and the
`GST_PLUGIN_PATH`/`LD_LIBRARY_PATH` lines, so every GUI-provisioned producer would have
failed with `No such element "kvssink"`.

**Fix:** LAUNCH.md A8 generates all 13 units (the missing six reconstructed from the code)
with this clone's literal paths, plus checks. Verified with `systemd-analyze verify`.

### #36 — Admin GUI rollback deleted a MediaMTX path with `POST` — a 404, silently

**Found:** 2026-09-25, while testing camera re-matching. **Referenced from:** CLAUDE.md.

When provisioning failed during registration, `app.py` rolled back the MediaMTX path it
had just added with `requests.post(".../v3/config/paths/delete/...")`. MediaMTX's delete
route only accepts `DELETE` and answers `POST` with `404 page not found`; the response
was never checked, so a failed registration always left an orphan path.

**Fix:** `requests.delete(...)`. Verified: the path is gone after a simulated
provisioning failure, and nothing is written to the registry.

### #37 — Start/Stop and status silently no-op'd for every GUI-registered camera

**Found:** 2026-09-25, while writing the sudo rule (#34). **Referenced from:**
`camera_control.py`, CLAUDE.md.

`camera_control.unit_name()` always returned `kvs-camNN.service`, but cameras registered
through the admin GUI run as template instances, `kvs-cam@camNN.service`
(`provision-camera.sh` enables that name). Start/Stop (agent and both GUIs), the status
column and the outage buffer's "is the producer running" check all targeted a unit that
doesn't exist. `systemctl is-active` printed `inactive` for it — the same word as for a
real stopped unit — so nothing looked wrong. Only cam-01/cam-02, which have their own
units, worked. It is #7's pattern again.

**Fix:** `unit_name()` returns `kvs-cam@<path>.service` when
`/etc/adapter/channels/<path>.env` exists — the file `provision-camera.sh` writes for
exactly those cameras — and `kvs-<path>.service` otherwise. Verified against systemd's
`LoadState` (`not-found` before, `loaded` after); the sudo rule (#34) already allowed both
forms.

---

## Cloud client and Lambdas

### #8 — A Lambda exception reached the browser as a bare `NetworkError`

**Found:** 2026-08-20. **Referenced from:** guide §8.6.

Pressing Start or reloading sometimes showed `NetworkError when attempting to fetch
resource`. `get_hls_url.py` attached its CORS header only on its own `return`; when
`get_hls_streaming_session_url` raised `ResourceNotFoundException` (producer not running),
the exception escaped, API Gateway returned its own 502 **without CORS headers**, and the
browser could not read the cross-origin response at all — `fetch()` threw instead of
resolving. `curl` cannot reproduce this (it ignores CORS), so backend tests passed.

**Fix:** wrap each handler in `try`/`except` and return every path — 200, 503 "stream is
not currently live — press Start", 500 — through the code that attaches CORS headers.
Applied to both Lambdas.

### #9 — `mediaSourceRequiresReset` on every player reload

**Found:** 2026-08-20. **Referenced from:** guide §8.6.

The client's `load()` created a fresh `new Hls()` per call and attached it to the same
`<video>` without releasing the previous instance's `MediaSource` — two overlapping
`MediaSource` objects on one element, which is exactly what the error means.

**Fix:** keep the instance in a module-level variable; `hls.destroy()` before creating
the next.

### #10 — The player retried forever after Stop, with no "stopped" state

**Found:** 2026-08-20. **Referenced from:** guide §8.6.

hls.js's recommended fatal-error recovery (`hls.startLoad()` on `NETWORK_ERROR`) is right
for a live stream's transient blips, but after Stop the stream had ended permanently and
the retry never finished. Even once retries stopped, the viewer saw a frozen last frame
under a spinner — indistinguishable from "still loading".

**Fix:** a `userStopped` flag checked before any retry; Stop tears the player down at
once (`hls.destroy()`, clear `src`); a state-driven overlay ("Stream stopped. Press Start
to watch again." / "Starting stream…"), cleared on `Hls.Events.FRAG_BUFFERED`. The video
wrapper needs `aspect-ratio: 16 / 9`, or the overlay collapses before first load.

### #11 — Browsers kept serving a stale client after deploys

**Found:** date not recorded. **Referenced from:** guide §8.5.1, CLAUDE.md.

After `aws s3 cp` of a new `client/index.html`, browsers kept serving the old page from
cache (S3 static website), so a fix appeared not to work.

**Fix:** every client upload sets `--cache-control "no-cache, must-revalidate"`. Behind
CloudFront the same header makes it revalidate (`x-cache: RefreshHit`), so an upload is
live immediately without `create-invalidation`.

### #14 — HLS sessions died after exactly five minutes

**Found:** 2026-09-06. **Referenced from:** guide §8.6.

A few minutes into watching, the browser's buffering ring appeared and never went away,
while the backend was entirely healthy. Three links in a chain:

1. `get_hls_url.py` requested `Expires=300` — the KVS minimum — so every session URL was
   dead after five minutes regardless of stream health.
2. The client received `expires_in` and never used it.
3. The fatal-`NETWORK_ERROR` branch called `hls.startLoad()`, which re-requests the
   **same** expired URL — a loop that cannot succeed — and showed no overlay, so it looked
   exactly like normal buffering.

*Proved, not inferred:* poll the master playlist every 30 s — `200` at t=+270 s, `403` at
t=+300 s, to the second; the fixed Lambda's URL still `200` at t=+385 s.

It surfaced on cam-02 first only because cam-02's fragments are 2.93 s against cam-01's
1.93 s (the camera's keyframe interval), leaving less headroom at the live edge — "only on
camera X" meant "camera X is the most sensitive detector".

**Fix:** `Expires=3600`; the client refreshes at 80 % of `expires_in`; the error handler
allows two `startLoad()` retries for real blips, then fetches a **new** session behind a
"Reconnecting…" overlay.

### #17 — A global `input` CSS rule broke the audio checkboxes

**Found:** during the audio work. **Referenced from:** `AUDIO.md` §6.4.

The audio checkboxes rendered detached from their labels and overflowing the panel. Not
the new markup: `client/index.html`'s pre-existing global rule `input { display: block;
width: 100%; … }`, written for the login form, matches every `<input>`; on a checkbox,
`width: 100%` spans the panel and pushes the label out. A `flex: none` attempt did nothing
(it touches neither `width` nor `display`). The working fix came only after rendering the
layout headlessly in Chromium instead of reasoning about it.

**Status:** fixed for the audio controls. **Still open:** the rule itself should be scoped
to `#login input`; not done, because it changes the login form's styling as a side effect.

---

## Outage buffering

### #18 — The guide's outage test (§10.2) could not detect anything

**Found:** first executed while building durable outage buffering. **Referenced from:**
guide §10.2, `OUTAGE.md` §1.2/§7.

Three independent faults, each enough on its own:

1. The `iptables` snippet is IPv4-only. This LAN is dual-stack and AWS resolves to
   `2a05:d014:…`, so every "blocked" connection went over IPv6 and returned HTTP 200 —
   with the rules apparently applied and packet counters even incrementing.
2. 120 s is exactly `kvssink`'s own `DEFAULT_BUFFER_DURATION_SECONDS`, so KVS lost nothing
   and the test proved nothing about buffering.
3. The suggested grep (`retry|reconnect|error`) matches none of the lines that show a
   buffer filling (`droppedFrame`, `storage overflow`, `Overall storage byte size`).
   The test had also never been run: its results file did not exist.

Found while fixing it: **blocking a resolved IP is useless** — the IoT endpoint rotated
through `18.196.251.80`, `3.69.141.146`, `18.185.210.34` and `18.153.244.214` within
minutes, so only blocking AWS *ranges* (`3/8, 18/8, 35/8, 52/8, 54/8`, `2a05::/16`) works.

**Fix:** `adapter/bin/awsblock.sh` (IPv4 + IPv6 ranges, verifies the block landed), outages
well past 120 s, and the method recorded in `measurements/reconnect_timeline.md`.

### #19 — The §17/M1 spec's MPEG-TS recording would have been mute

**Found:** during the outage-buffer design. **Referenced from:** `OUTAGE.md` §1.3/§3.1.

One of two earlier outage-recording specs recorded to MPEG-TS. MediaMTX's MPEG-TS
recorder cannot carry LPCM or G.711 — cam-01's and cam-02's audio on the MediaMTX leg —
so it would have recorded silently mute footage (the ingest-permissive/playback-strict
pattern again).

**Fix:** never built that way; `recordFormat: fmp4` is mandatory (`mediamtx_api.py`).

### #20 — Segment names parsed as UTC: the rolling buffer grew without bound

**Found:** outage-buffer testing (B1). **Referenced from:** `OUTAGE.md` §5.2.

`recordPath`'s `%Y-%m-%d_%H-%M-%S` is formatted in the machine's local zone; parsing it as
UTC put every segment two hours in the future on a CEST box, so the retention cutoff never
matched. Measured 9 segments where 4–5 were expected — it would have filled the stick
silently.

**Fix:** parse with `astimezone()`, with an mtime fallback so an unparseable name can
never become un-prunable.

### #21 — The supervisor blocked 45 minutes on AWS, during the outage it watches for

**Found:** outage-buffer testing (B1). **Referenced from:** `OUTAGE.md` §5.2.

`load_registry()` called DynamoDB on the tick path with boto3's defaults (60 s connect /
60 s read, with retries). With AWS unreachable the process sat in `poll_schedule_timeout`
for **45 minutes**, never reaching the connectivity check — never detecting the outage.

**Fix:** all AWS access moved to a background thread (the tick loop makes no network call),
and the scan pinned to `connect_timeout=3, read_timeout=5, max_attempts=1`.

### #22 — Recovery on a single probe made the supervisor flap

**Found:** outage-buffer testing (B1). **Referenced from:** `OUTAGE.md` §5.2 (`outage_buffer.py`'s
comment retells it).

The IoT endpoint's DNS rotates across AWS ranges; one rotation briefly landed on a
reachable address and the supervisor declared recovery — finalising the capture and
opening another. Measured 3 finalise/reopen cycles within one outage, fragmenting it into
separate clips.

**Fix:** asymmetric thresholds — 2 consecutive failures to declare an outage, 3
consecutive successes to declare recovery. Acting early on failure is cheap; acting early
on recovery stops recording.

### #23 — The reachability probe cost twice its timeout

**Found:** outage-buffer testing (B2). **Referenced from:** `OUTAGE.md` §5.3.

`socket.create_connection` applies its timeout per resolved address, and the IoT endpoint
has A and AAAA records, so `PROBE_TIMEOUT = 4` cost 8 s per probe. Two probes made
detection 86 s against a 120 s pre-roll — working, on a third of the intended margin, and
at the mercy of how many addresses DNS returns.

**Fix:** an explicit resolve-then-try loop under a total `PROBE_BUDGET_SEC = 4`. Measured
0.02 s reachable, hard-capped 4 s unreachable.

---

## Setup, documentation and repository (second Pi, 2026-09-24/25)

### #24 — LAUNCH A3's patches were not actionable, and patch 3 can't be applied up front

**Found:** 2026-09-24, when the build failed (#2 recurrence). **Referenced from:**
LAUNCH.md A3, README §3, guide §4.2.

LAUNCH A3 listed the three SDK patches only as comments inside a code block, so the build
was started without them. README showed a one-command `cmake … && make` build. And patch
3's target file (`kvspic-src/CMakeLists.txt`) does not exist until the configure step has
downloaded kvspic, while `Thread.c` compiles only later in `make` — so "apply all three
patches first" was impossible as written.

**Fix:** A3 as runnable commands with a proof after each step, and two stages: patches
1–2 → `cmake` configure → patch 3 → `make -j1`. The 2026-09-25 build ran this way in 16
minutes with zero `earlyoom` kills.

### #25 — Absolute `/home/vladimir/MyProjects/VMS` paths broke when the clone moved

**Found:** 2026-09-24. **Referenced from:** CLAUDE.md "Paths", guide §1.3, LAUNCH.md A2.

Seventeen hardcoded paths across 11 files in `adapter/` (certificates, venv, WSDL
directory, helper scripts), plus the docs' `VMS_HOME` exports and systemd examples, still
named the old Pi's clone location. Nothing could find its files in
`~/Projects/VideoSafeZone`. The venv's `python3.13` directory was spelled out too.

**Fix:** code resolves `$VMS_HOME` if set, else the repo root from its own location;
the venv's `pythonX.Y` comes from `sys.version_info`. Units get literal paths generated
per clone (#30). Deployment identity moved to `/etc/adapter/adapter.env` in the same
spirit.

> **Both halves now applied in this repository** (2026-09-26). Paths came first;
> deployment identity followed, ported from the successor and verified against live AWS:
> `adapter/config.py` and `adapter/bin/adapter-config.sh` read
> `/etc/adapter/adapter.env`, and the eight Python modules and four pipeline scripts that
> carried literals now take region, Thing, role alias, both IoT endpoints and the evidence
> bucket from it. Proven by starting `kvs-cam02.service` on config-derived values alone:
> 8 fragments `PERSISTED` to KVS, zero credential errors. An earlier note here argued the
> refactor was not worth doing on a single-Pi deployment; that was overtaken by the
> decision to make this repository portable too.
>
> Still outside the file, deliberately: `client/index.html` (API Gateway and Cognito IDs
> it is built against) and `cloud/` IAM documents (account in ARNs).

### #26 — The MediaMTX release tarball overwrites the project's `mediamtx.yml`

**Found:** 2026-09-25. **Referenced from:** guide §2.5, LAUNCH.md A5.

The documented `tar xzf mediamtx.tar.gz` extracts the release's default `mediamtx.yml`
over the tracked, customised one — silently: MediaMTX starts fine without the project's
settings. LAUNCH A5's proof ("`git status` must not list `mediamtx.yml` as modified")
also gave a false failure whenever the file had legitimate uncommitted edits.

**Fix:** `tar xzf mediamtx.tar.gz mediamtx` (binary only) and `curl -f`; the proof is a
checksum taken before extraction and checked after.

### #27 — The documented venv package list was incomplete

**Found:** 2026-09-25. **Referenced from:** README §4, LAUNCH.md A4.

README's `pip install` omitted `WSDiscovery` (imported by `onvif_discovery.py`) and `lxml`,
so the admin GUI and discovery could not import.

**Fix:** `boto3 awsiotsdk onvif-zeep-async WSDiscovery flask requests lxml`; verified by
importing all 17 adapter modules against the venv.

### #28 — Troubleshooting advised `-j2` for the OpenSSL build crash

**Found:** 2026-09-25. **Referenced from:** guide §15.

The troubleshooting row "Build dies around OpenSSL/curl → `make -j4` on 4 GB — use `-j2`"
contradicted §4.2's own finding: the outer `-j` never reaches the nested build (#2).

**Fix:** the row now points at patch 1 and names the `Terminated signal … cc1` symptom.

### #29 — The guide never downloaded `AmazonRootCA1.pem`

**Found:** 2026-09-25. **Referenced from:** guide §6.4.

`agent.py`'s MQTT connection uses `AmazonRootCA1.pem`, but the guide only downloaded
`SFSRootCAG2.pem` (the credentials endpoint's CA). A fresh setup had no MQTT CA.

**Fix:** both downloads in guide §6.4 and LAUNCH.md A7, with a proof for each endpoint.

### #31 — cam-02's RTSP credentials were committed in `mediamtx.yml`

**Found:** 2026-09-25. **Referenced from:** LAUNCH.md E3, CLAUDE.md, guide §16.2.1.

cam-02 was kept alive across MediaMTX restarts (#32) by hardcoding its path in the
tracked `mediamtx.yml` — camera IP, user and password in the `source:` URL — and the file
was pushed to GitHub (commit `aaf04bd`).

**Fix:** `mediamtx.yml` has no camera paths; addresses and credentials live only in the
registry (`rtspUrl`) and a 0600 local cache (#32). Verified here 2026-09-26: `grep -c
'password=' mediamtx/mediamtx.yml` is 0, and cam-02's path is recreated from the registry
after both a normal restart and a `kill -9` of MediaMTX.

**Still required, in both repositories:** change the camera's password. It remains in the
git history this repository and the successor share, so removing it from the working tree
does not retract it. Rotate at the camera, then Re-register cam-02 (LAUNCH E3).

### #32 — API-added MediaMTX paths vanished on every MediaMTX restart

**Found:** 2026-09-25. **Referenced from:** guide §16.2.1, LAUNCH.md E2/E3, CLAUDE.md.

The admin GUI adds a registered camera's path through MediaMTX's API, and MediaMTX never
writes API changes back to `mediamtx.yml`. Every MediaMTX restart therefore dropped every
GUI-registered camera until someone clicked Re-register — the reason for #31.

**Fix:** `adapter/sync_mediamtx_paths.py` as `ExecStartPost=` of `kvs-mediamtx.service`:
after every start (automatic crash restarts included — a separate unit ordered after
MediaMTX would miss those) it adds each passthrough camera's path from the registry, or
from a 0600 cache when AWS is unreachable; it never deletes a path and leaves unchanged
ones alone. Verified by `kill -9` on MediaMTX: the path was back after the automatic
restart.

### #35 — MediaMTX's generated `auto.crt`/`auto.key` were not git-ignored

**Found:** 2026-09-25. **Referenced from:** LAUNCH.md A5, `.gitignore`.

MediaMTX v1.20 generates a self-signed certificate and private key (`CN=mediamtx`) in
`mediamtx/` on its first start. `auto.key` was ignored only by the generic `*.key` rule;
`auto.crt` not at all — it would have gone into the next commit.

**Fix:** both named explicitly in `.gitignore`.

### #38 — Instructions that failed when followed literally

**Found:** 2026-09-25, reading every current document against the system and against each
other. **Referenced from:** guide §2.8, §4.1, §7.3, §16.6, §17, LAUNCH.md Part B/C/F,
README.

Most of what the review found was drift — statements the system had outgrown, fixed in
place without an entry. These are the ones that *break something* when a reader follows
them as written:

- **Guide §4.1 installed `gstreamer1.0-omx-generic`**, which does not exist on Debian
  trixie, so the whole `apt install` failed. LAUNCH A3 and README had dropped it; the guide
  had not.
- **Guide §7.3 restarted the agent with `sudo systemctl restart kvs-agent.service`**, but
  `kvs-agent` is a *user* unit: the system manager has no such unit, so the proof step
  restarted nothing (the very next line already used `journalctl --user`).
- **`adapter/bin/awsblock.sh`, the outage simulator LAUNCH Part C prescribes, needs
  `iptables`/`ip6tables`**, which a fresh Raspberry Pi OS image doesn't have (it ships
  `nft` only). Nothing said to install it; the old Pi had it from an earlier session.
- **Guide §2.8's `kvs-mediamtx` unit lacked the `ExecStartPost=` path sync**, so a reader
  building units from the guide rather than LAUNCH A8 got MediaMTX with no camera paths
  (#32). §16.6 enabled template instances by camera ID (`kvs-cam@cam-01`) where the real
  ones are named by path (`kvs-cam@cam01`) — #37's mix-up, in the docs.
- **LAUNCH Part B never enabled `kvs-outage-buffer`/`kvs-outage-uploader`**, while Part C
  checked that both were running and README's start list enabled them. Part F and README
  stopped only `kvs-cam01`/`kvs-cam02`, leaving GUI-registered producers billing.
- **Twelve references to a `COSTS.md` that doesn't exist**, with v1.3's section numbers
  and figures ($4.13/month, "4.7×, near 700 cameras") that COSTS-1.4 had revised.

**Fix:** each corrected where it appears — package dropped, `systemctl --user`, an
`apt install iptables` step in LAUNCH Part C, pointers from the guide's historical units
and code listings to LAUNCH A8 and the current code, the outage units in Part B, `stop
'kvs-cam*'` in Part F and README, and every `COSTS.md` reference remapped to
`COSTS-1.4.md`'s sections and figures.

### #39 — The outage supervisor crash-looped on a Pi without the USB stick

**Found:** 2026-09-25, verifying LAUNCH Part B on the second Pi. **Referenced from:**
`outage_buffer.py`.

`kvs-outage-buffer` showed `activating`, not `active` — 80 restarts. At startup,
before its loop ever called `buffer_ready()`, `main()` created `OUTAGE_DIR` to scan for
orphan captures. With the stick not set up (LAUNCH.md A10), `/mnt/vms-buffer` didn't exist,
and creating it needs root: `PermissionError`, exit, `Restart=on-failure`, repeat. The
design says a missing stick means *idle, disarmed* — the sentinel check exists for exactly
that — and the docs promise the outage units idle unless enabled; the one unguarded line
ran first. `is-active` alone would not have shown it: the state word was `activating`,
and only the restart counter and the journal told the story.

**Fix:** nothing touches the buffer until `buffer_ready()` passes; the orphan scan runs
once, on the first ready tick, and readiness changes are logged ("buffer unavailable (not
mounted) -- idle …"). Verified on the Pi without a stick (active, no restarts) and with a
simulated stick appearing mid-run (ready logged, one orphan capture reported once).
(`outage_uploader.py` was already gated on `buffer_ready()`.)

### #40 — cam-01 never published: `gstreamer1.0-rtsp` missing from the setup lists

**Found:** 2026-09-25, "cam01 does not provide video" on the second Pi. **Referenced
from:** LAUNCH.md A3, README §3, guide §4.1.

`kvs-camera-publish` had restarted 167 times: `WARNING: erroneous pipeline: no element
"rtspclientsink"`. Camera detection worked; the element that publishes into MediaMTX did
not exist, so `rtsp://127.0.0.1:8554/cam01` was a 404 and nothing downstream had a source.
`rtspclientsink` ships in `gstreamer1.0-rtsp`. Only guide §2.4 installed it; LAUNCH A3's
package list — the one a fresh Pi is set up from — and README §3 did not. (`rtspsrc`, what
the producers use to *read* from MediaMTX, is in `plugins-good`, so they were unaffected.)

It survived the Phase 2 hardware test because that test ran `publish-cam01.sh`'s exact
pipeline with the sink swapped for a local file (MediaMTX wasn't installed yet): the one
element missing was the one the test replaced. A test that substitutes a component proves
nothing about that component.

**Fix:** `gstreamer1.0-rtsp` (and `v4l-utils`) in LAUNCH A3 and README §3, and
`gst-inspect-1.0 rtspclientsink` in A3's proof. Verified: publisher `active` with a stable
restart count, MediaMTX `cam01` ready, H.264 High 1280×720, decoded frame correct.

