# H.264 / H.265 codec selection — Phase 0 verification

> **Provenance.** These measurements were taken in the successor repository
> (`VideoSafeZone`) on 2026-09-26, on identical hardware and against the same two cameras
> and the same AWS account. The file was brought here on 2026-09-27 with the H.265
> pipeline, because the ported code cites its section numbers. It is kept verbatim, so it
> describes that repository's feature in full — including the admin-GUI codec switch and
> the ONVIF Media2 capability probe, which this repository **does not have**. Guide §21.4
> says what that costs here; guide §21 is authoritative for what this repository does.

Measured 2026-09-26 on the Pi 4B (Rev 1.5, 4 GB) against `cam-02` (ONVIF) and `cam-01`
(PW310), before any code for per-camera codec selection was written. Purpose: find out what
the hardware, the camera, KVS and GStreamer actually do, so the design rests on
observations instead of the assumptions in `NETWORK.md` §4 and `COSTS-1.4.md` §7.4.

**The design built on these results is guide §21** (`Demo-AWS-Video-revCosts4.md`); this file
keeps the raw evidence. Every row is marked the way `Camera-Features.md` does it: **verified** = observed working
here; *not verified* = still pending.

---

## 1. Encode capability on the Pi — verified

| Check | Result |
|---|---|
| `gst-inspect-1.0 v4l2h264enc` | present — `/dev/video11 bcm2835-codec-encode` |
| `gst-inspect-1.0 v4l2h265enc` | **absent** |
| `/dev/video19 rpi-hevc-dec` | HEVC **decode** only (`v4l2slh265dec`) |
| `x265enc`, `h265parse`, `rtph265depay/pay` | present (GStreamer 1.26.2) |

The GStreamer V4L2 plugin registers `v4l2h26Xenc` only when a matching M2M encoder device
exists, so element presence is a usable hardware probe.

## 2. `cam-02` H.265 — verified (0.1)

ONVIF only reports H.265 through **Media2** (`ver20/media`); the venv's bundled WSDL set
has Media1 (`ver10`) only, so the probe used raw SOAP with WS-UsernameToken digest.

| | `VideoEncodeMain` | `VideoEncodeSub` (in use) |
|---|---|---|
| Current | H264 High, 2560×1440, 15 fps, 3000 kbps VBR, GOP 45 | H264 High, 640×360, 15 fps, 500 kbps VBR, GOP 45 |
| H265 option | 2560×1440 … 1280×720, 128–8192 kbps | 640×360, 480×360, 352×288, 64–2048 kbps |

`SetVideoEncoderConfiguration` (Encoding → `H265`) on `VideoEncodeSub` **stuck** on read-back;
the RTSP URI did not change; MediaMTX's source dropped ~7 s and reconnected by itself with
tracks `[H265, G711]`. `ffprobe`: `hevc`, Main, 640×360@15. A decoded keyframe was clean.
MediaMTX local HLS served it as fMP4, tag `hvc1`. Reverting to H264 restored a configuration
**byte-identical** to the saved original.

MediaMTX's own fMP4 *recording* of the H.265 feed (the outage-buffer format) is tagged
`hvc1` too — checked later the same day with a temporary recording path, 5 × 10 s segments.
`ffmpeg -c:v copy` keeps whatever tag its input has (an `hev1` input merged to an `hev1`
clip), so `outage_uploader.merge()` forces `hvc1` for HEVC regardless.

Quirks: the read-back keeps `Profile="High"` after switching to H265 (not an HEVC profile —
the bitstream is Main); GOP is 45 frames, so `cam-02`'s KVS fragments are 3 s, not 2 s.

MediaMTX logged `cam02` dial timeouts 12:46:16–12:47:08, ~3 min after the first revert.
**Not caused by the encoder change:** the second switch pair (13:54 → 13:56) was followed by
no drop at all, while the camera was also unreachable 13:47:50–13:52:59 (`no route to
host`, 5 min) with no ONVIF activity against it. The camera (or its network path) drops out
on its own now and then. The only switch-related artefact is transient: the first reconnect
after a switch can read a half-reconfigured SDP (`invalid SPS: not enough bits`), and
MediaMTX's next attempt 5 s later succeeds.

## 3. KVS with H.265 — verified (0.3)

Throwaway stream `cam-99` (`MediaType=video/h265`), fed `cam-02`'s H.265 through a
passthrough producer: `rtph265depay ! h265parse config-interval=-1 !
video/x-h265,stream-format=hvc1,alignment=au ! kvssink`.

| Check | Result |
|---|---|
| kvssink (SDK v3.6.0) | codec ID `V_MPEGH/ISO/HEVC`, content type `video/h265`, 59/60 fragments `PERSISTED` in 180 s |
| `GetHLSStreamingSessionURL` LIVE | `CODECS="hvc1.1.6.L90.0"`, fMP4; `ffprobe` hevc/Main/`hvc1`; decoded frame clean |
| `GetClip` 60 s | MP4, hevc Main, tag **`hvc1`** (Safari-compatible — no remux needed) |

`stream-format=hvc1` is mandatory: kvssink only sends CPD when the caps carry `codec_data`
(`gstkvssink.cpp:1166`), and its H.265 pad template does not pin a stream format.

`UpdateStream` accepts `MediaType` (metadata for consumers/console only), so switching an
existing camera's codec does **not** require deleting and recreating the stream —
`NETWORK.md` §4 step 3 is wrong on this point.

## 4. Codec switch on one stream — verified (0.4)

`cam-99`: H.265 10:38:22–10:41:22Z, then H.264 10:43:29–10:45:59Z.

| Request | Result |
|---|---|
| New LIVE HLS session after the switch | plays, `CODECS="avc1.640016"` |
| `GetClip` spanning the switch | **fails**: `InvalidCodecPrivateDataException: The codec private data is not consistent between all fragments.` |
| `GetClip` inside one codec | works |
| ON_DEMAND HLS spanning the switch | playlist lists all 36 segments + a 2nd `EXT-X-MAP`, master advertises only `hvc1`; segment 18 (first H.264) → **HTTP 400** `The requested fragment does not have the same codec private data as the rest of the session.` |

Rule: every playback session and every clip must lie within one codec. Clip windows that
straddle a switch must be clamped to the switch time.

## 5. Bitrate, same scene back to back — verified, single sample

`cam-02` SubStream, empty hallway, daylight, 60 s each, video only, ~2 min apart:

| H.264 | H.265 | Saving |
|---|---|---|
| 166 kbps | 114 kbps | **31 %** |

Below `COSTS-1.4.md` §7.4's assumed 40–50 %. One daylight sample; night (noise-dominated)
still to measure. A 60 s KVS clip with motion in it averaged 220 kbps H.265 — bitrate
follows the scene far more than the codec.

## 6. `cam-01` software H.265 — verified, with two blocking findings (0.2)

Real PW310 content (not `videotestsrc`, which proved far too easy to encode:
synthetic 720p "ball" ran at 23 fps on ~1 core; the real camera does not come close).
`x265enc speed-preset=ultrafast tune=zerolatency key-int-max=30`, CPU of the whole
`gst-launch` process, 20 s runs:

| Resolution @15 fps | fps held | CPU (of 4 cores) |
|---|---|---|
| 1280×720 | **no — ~9.8 fps** | 3.3 |
| 960×540 | yes, 15.4 | 2.8 |
| 640×360 | yes, 15.4 | 1.55 |
| *today: HW H.264 1280×720* | 15 | *0.11* |

SoC ≤ 63.7 °C, `get_throttled=0x0` throughout (short runs; not a sustained or night test).

**Finding A — `v4l2jpegdec` shuts down if the encoder stalls it.** With x265 directly
downstream the pipeline delivered 7–11 frames and then nothing, with no error. Debug log:
`gst_v4l2_buffer_pool_dqbuf:<v4l2jpegdec0:pool0:src> Empty last buffer, signalling eos` at
~1.3 s, then the decoder's task pauses for good. x265's start-up blocks the streaming
thread > 1 s, the camera loses frames, and the bcm2835 decoder returns an empty `LAST`
buffer that GStreamer takes as EOS. Fix that worked: a leaky
`queue max-size-buffers=30 max-size-bytes=0 max-size-time=0 leaky=downstream` in front of
x265 (plus a hardware scale to NV12 and a `videoconvert` to I420, so x265 never holds the
decoder's buffers). `jpegparse` before the decoder is not an option (not-negotiated).
Also: with `-e`, SIGINT on a stalled `v4l2jpegdec` pipeline hangs forever waiting for EOS.

**Finding B — open blocker: the fixed chain will not publish to MediaMTX.** The same chain
ends fine in `fpsdisplaysink sync=true` (15.3 fps), and synthetic x265 → `rtspclientsink`
works (with or without the leaky queue), but camera → x265 → `rtspclientsink` stalls every
time: MediaMTX accepts the RECORD and announces `[H265]`, no packet ever arrives.
Backtrace: the encoder's streaming thread waits in `libgstrtspserver` (payloader pad
block), rtspclientsink's control task waits in `gst_rtsp_connection_receive_usec`; the only
warning is `rtpsession: Can't determine running time for this packet without knowing
configured latency`. Not path reuse, not the queue type. Unresolved.

**Decision (2026-09-26): `cam-01` stays H.264-only.** 720p is out of reach, 640×360 would
cost 1.5 cores 24/7 (the publisher never stops, because preview and outage buffer carry the
selected codec) at a quarter of the resolution, and Finding B would have to be solved first.
The GUI shows H.265 for `cam-01` as unavailable, with the reason. Finding B is therefore
parked, not fixed — revisit it only if a software-encode path is ever wanted again.

## 7. Browser matrix — in progress

Test page: hls.js (`@latest`, resolved to 1.7.3) against KVS ON_DEMAND sessions of `cam-99`
(§3/§4 windows) and the two GetClip MP4s; capability probes via
`MediaSource.isTypeSupported` / `canPlayType`.

| Browser | MSE `hvc1` | Live HLS H.265 | Clip MP4 H.265 | H.264 controls |
|---|---|---|---|---|
| Firefox 156, Windows 10/11 x64 | not supported | **fails** — hls.js `manifestIncompatibleCodecsError` | **fails** — `NS_ERROR_DOM_MEDIA_METADATA_ERR` | both play |
| Chrome 154, same Windows machine | supported (`hvc1` and `hev1`) | plays 640×360 | plays 640×360 | both play |
| Edge 153, same Windows machine | not supported | **fails** — `manifestIncompatibleCodecsError` | **fails** — `play()` rejected, "no supported source" | both play |
| Firefox and Edge, a **second** Windows PC | — | plays (user report) | plays (user report) | — |
| Firefox and Edge, first PC, **after installing "HEVC Video Extensions"** ("HEVC-Videoerweiterungen", Microsoft Store 9NMZLZ57R3T7) | — | plays (user report) | plays (user report) | — |
| Safari (Apple), no add-on | — | plays (user report) | plays (user report) | — |
| Chromium 153, Raspberry Pi OS (the Pi itself), headless | not supported | — (probe only) | — (probe only) | — |

**Support depends on the browser *and* the machine — neither alone predicts it.** On PC 1
only Chrome plays H.265; on PC 2 Firefox and Edge do. Chrome and Edge are both Chromium and
still disagree on PC 1. **Confirmed by the fix:** Firefox and Edge on Windows decode HEVC
through the operating system's decoder — installing Microsoft's HEVC Video Extensions on
PC 1 made both play. Chrome played on PC 1 without it. Safari needs nothing. Either way the GUI cannot infer support from the user agent or the
platform — only the runtime probe is trustworthy. (Chrome and Edge also answer
`canPlayType("application/vnd.apple.mpegurl")` with `maybe` — the client must keep preferring
hls.js when `Hls.isSupported()`, as it does today, rather than branching on that.)

**The probe predicts the outcome in both directions.** Where `isTypeSupported('video/mp4; codecs="hvc1…"')` is
false, HLS fails; where `canPlayType` is empty, the clip fails. So the per-viewer warning
can be decided *before* playback, and a clip must not be handed to `<video>` in a browser
that cannot decode it (the raw error is unintelligible). hls.js reports the case with a
specific error detail (`manifestIncompatibleCodecsError`), which the live view can also map
to the same message.

Both GUIs therefore tell a viewer whose browser lacks H.265 what to do (Windows: the
Store link), decided by the runtime probe.

Still to test: Chrome Android, a desktop Linux browser with a GPU.

## 8. Status (Phase 0 closed 2026-09-26)

Everything that gates the implementation is verified. Left open, none of it blocking:

- *not verified* — night bitrate, H.264 vs H.265 back to back on `cam-02` (feeds
  `COSTS-1.4.md` §7.4, whose 40–50 % assumption met 31 % in daylight, §5). The x265 night
  and thermal questions were dropped with the `cam-01` decision (§6).
- *not verified* — Chrome on Android, a desktop Linux browser with a GPU (§7). Not needed
  for the design: the runtime probe decides per viewer.
- **verified** — audio on with H.265 (`cam-02`, 11:54–11:56Z): `stream-cam02.sh`'s audio
  branch with `rtph265depay` instead of `rtph264depay`; 49 fragments persisted in 150 s,
  **0 rejects** (`0x30000005`); live HLS `CODECS="hvc1.1.6.L90.0,mp4a.40.2"`; 45 s `GetClip`
  = HEVC `hvc1` + AAC 8 kHz at 25.0 kb/s. The §18.3 arithmetic is unchanged because frame
  rate and GOP are unchanged. Camera restored byte-identical afterwards.
- Test stream `cam-99` deleted 2026-09-26 after the browser tests (its archived fragments
  and the ON_DEMAND test URLs went with it).
