# Networking notes: MediaMTX, ONVIF discovery, VLANs

Companion notes to the build guide, covering the local media-relay layer, camera
discovery, and network isolation for the camera segment. Written up from discussion while
*planning* the MVP build — kept separate from the runbook because it is reference
material, not a build sequence.

> **Read this as a planning document, and check anything operational against the current
> docs.** It was written against `Demo-AWS-Video-MCh-15.md`, which
> `Demo-AWS-Video-revCosts4.md` has since superseded; the §-numbers below still resolve,
> because the two guides share their numbering, but the *state of the world* has moved on
> in three places, each flagged in the text:
>
> | Section | Then | Now |
> |---|---|---|
> | §1 MediaMTX | run by hand with `&`, a unit "to add later" | `kvs-mediamtx.service`, `LAUNCH.md` Part B |
> | §2 WS-Discovery | "not implemented", a roadmap item | built and shipped — `adapter/onvif_discovery.py` + the local admin GUI |
> | §3 VLANs | no VLAN, one flat network | **unchanged** — still the open item it describes |
>
> The analysis in each section is what it was for, and still stands. `LAUNCH.md` is
> authoritative for anything you actually run, `COSTS-1.4.md` for any figure, and
> `FoundAndFixed.md` for defects.

---

## 1. MediaMTX

### What it is

[MediaMTX](https://github.com/bluenviron/mediamtx) (formerly `rtsp-simple-server`) is a
lightweight, dependency-free media server written in Go. It speaks RTSP, RTMP, HLS,
WebRTC and SRT, but this project only uses one role: **a local RTSP relay running on the
Pi**. It has no encode/decode logic of its own — it accepts a stream pushed to it and
re-serves that same stream to anyone who connects and asks for it.

### Why it's in this architecture

From the build guide §2.5: keeping MediaMTX in the design (even though the
PW310 USB webcam replaced the original synthetic source) is deliberate. It preserves the
**RTSP boundary** that a real IP camera (Hikvision, etc.) would present natively, so
Phases 3–9 of the build stay untouched regardless of what's actually behind the camera
path. A real ONVIF/RTSP camera speaks RTSP directly; the PW310 doesn't, so MediaMTX
absorbs that difference. Everything downstream only ever talks to
`rtsp://127.0.0.1:8554/camXX`.

This is what makes the later pass-through-vs-transcode comparison (doc §16.3b) a one-line
change: point MediaMTX's source at a real camera's RTSP URL instead of a local GStreamer
publisher, and nothing downstream needs to know.

### How it's wired into the pipeline

Two independent GStreamer processes talk to MediaMTX over loopback:

```
v4l2src (PW310) → jpegdec → v4l2h264enc → rtspclientsink ──push──▶ MediaMTX :8554/cam01
                                                                          │
                                                                     (relays)
                                                                          │
kvssink pipeline: rtspsrc rtsp://127.0.0.1:8554/cam01 ◀──pull────────────┘
```

- **Publisher** (`rtspclientsink location=rtsp://127.0.0.1:8554/cam01`) — an RTSP client
  that pushes (ANNOUNCEs) the encoded stream into MediaMTX under path `cam01`.
- **Consumer** (`rtspsrc location="rtsp://127.0.0.1:8554/cam01"`) — a separate process,
  the KVS producer pipeline, that pulls the same stream into `kvssink`.

Because these are decoupled processes, the cloud-facing pipeline can be restarted (e.g.
while iterating on `kvssink` params) without interrupting capture, and `ffprobe`/`ffplay`
can inspect the stream independently for debugging (§2.6) — MediaMTX serves multiple
simultaneous readers of the same path for free.

### Running it

```bash
cd ~ && mkdir -p mediamtx && cd mediamtx
curl -L -o mediamtx.tar.gz \
  https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_linux_arm64v8.tar.gz
tar xzf mediamtx.tar.gz && ./mediamtx &
```

Notes not in the main runbook:

- The archive ships a single static binary plus a default `mediamtx.yml`. **Extract only
  the binary** — a plain `tar xzf` over the repo overwrites this project's own
  `mediamtx.yml` (`FoundAndFixed.md` #26, `LAUNCH.md` A5). RTSP still binds `:8554`; the
  HLS port (8888) is deliberately LAN-reachable now, since the admin GUI's browser-side
  preview uses it, and the control API (9997) is localhost-only.
- Running it with a trailing `&` doesn't survive a reboot or crash, and isn't managed by
  the `kvs-cam01.service` systemd unit (§7.1), which only supervises the KVS producer.
  **Done since:** MediaMTX runs as the user unit `kvs-mediamtx.service` (`LAUNCH.md` A8
  generates it, Part B starts it). The `teardown.sh` mentioned here was never written;
  Part F is the stop sequence. The unit also carries an `ExecStartPost=` that recreates
  registry-backed camera paths, because MediaMTX does not persist API-added paths
  (`FoundAndFixed.md` #32).
- Startup ordering matters once this is under systemd: MediaMTX must be listening before
  the publisher (`rtspclientsink`) tries to connect, and before the KVS producer's
  `rtspsrc` tries to pull. Use `After=`/`Requires=` or `Restart=on-failure` with retry
  rather than assuming instant availability.
- `"Device or resource busy"` on `v4l2src` (known trap in the main doc) is never
  MediaMTX's fault — it never touches V4L2 devices, only RTSP bytes. It means something
  else (ffplay, guvcview) still holds the camera node open.

### Alternatives considered

| Project | Language | Notes |
|---|---|---|
| **gst-rtsp-server** | C (Python via PyGObject/`GstRtspServer`) | Strongest fit for this stack — already 100% GStreamer. Lets the capture pipeline serve RTSP directly (`v4l2src ! ... ! rtph264pay name=pay0`), collapsing the publisher+relay into one process instead of two talking over loopback. Worth prototyping as a simplification. |
| **v4l2rtspserver** | C++ (built on live555) | Purpose-built for V4L2/M2M capture on boards like this. Captures and serves RTSP in one binary. Trade-off: likely less granular control over GOP/caps tuning (`h264_i_frame_period`, `repeat_sequence_header`, the `level=(string)4` caps workaround) than a hand-written `gst-launch` pipeline. |
| **live555** (library) | C++ | The toolkit many IP camera firmwares embed. Low-level — significant code to hand-write vs. `gst-rtsp-server`. |
| **live555ProxyServer** | C++ (live555 example binary) | Ready-made RTSP proxy/relay, but expects an RTSP source upstream already — doesn't solve the "GStreamer pushes in" side as cleanly. |
| **ZLMediaKit** | C++ | Production-grade, RTSP/RTMP/HLS/WebRTC/GB28181, widely used in IP-camera/NVR products. More capable than MediaMTX but heavier to configure/cross-compile than a static Go binary. |
| **SRS** | C++ | Similar scope to ZLMediaKit, more oriented toward RTMP/streaming-platform use cases than a simple local relay. |
| ~~ffserver~~ | C | Removed from ffmpeg since 4.0 — dead, don't use. |
| Python pip packages (`rtsp-server`, etc.) | Python | Generally immature/single-maintainer. Not recommended for anything run unattended. |

**Recommendation:** stay on MediaMTX for the MVP (single static binary, zero-config, and
it cleanly preserves the swappable camera-facing boundary needed for §16.3b). Revisit
`gst-rtsp-server` later as an architectural simplification once the pipeline is stable —
it's a legitimate "evaluated and chose X because Y" talking point.

---

## 2. ONVIF WS-Discovery

> **Status: built.** This section was written when discovery was a roadmap item; it
> shipped in §16.2.1 — `adapter/onvif_discovery.py` (shared by the `discover-onvif.py`
> CLI and the local admin GUI) does discover → register → control end-to-end. The
> gap-analysis row now reads **done**, with one caveat: a camera whose IP changes after
> onboarding still needs a manual re-scan. The protocol explanation below is why it works
> the way it does, and is unchanged.

As originally written, this appeared once in the guide as a roadmap item in the
gap-analysis table (§16.2):

| Capability | Cloud Adapter Mini | This prototype | Effort to close |
|---|---|---|---|
| Camera discovery | hundreds of brands | hardcoded URL | **M** — ONVIF WS-Discovery |

and every RTSP source was a literal string in a per-channel config file:

```json
{"id":"cam-01","url":"rtsp://192.168.178.90:554/Streaming/Channels/102","mode":"passthrough"}
```

That `channels.json` shape was never built. A camera's address and credentials live in
the DynamoDB `cameras` registry (`rtspUrl`), which is the single source of truth; the
per-camera file under `/etc/adapter/channels/` holds only the systemd template's
parameters. Keeping the URL out of any tracked file is deliberate — it used to sit in
`mediamtx.yml`, credentials included, and reached GitHub (`FoundAndFixed.md` #31).

### How WS-Discovery works

WS-Discovery ("Web Services Dynamic Discovery") is a generic W3C/OASIS protocol that
ONVIF adopted for device discovery. It only answers "what ONVIF devices exist on this
LAN and what's their control endpoint?" — it does not hand back an RTSP URL directly.

1. **Multicast probe.** The client sends a UDP multicast SOAP message to
   `239.255.255.250:3702` (the well-known WS-Discovery group/port):
   ```xml
   <Probe><Types>dn:NetworkVideoTransmitter</Types></Probe>
   ```
2. **ProbeMatch.** Every matching ONVIF device on the same L2 broadcast domain replies
   with its **device service address** (an HTTP/SOAP endpoint, e.g.
   `http://192.168.178.90/onvif/device_service`) plus scope URIs (name, hardware,
   location).
3. **Hello/Bye.** Devices also announce on boot (`Hello`) and clean shutdown (`Bye`), so a
   long-running listener can track appearance/disappearance without polling.

### From ProbeMatch to an actual RTSP URL

Separate ONVIF SOAP calls ("Profile S"), against the endpoint found above:

1. `GetCapabilities` → returns the camera's **Media service** endpoint.
2. `GetProfiles` → returns configured stream profiles (e.g. main/sub stream).
3. `GetStreamUri` for a chosen profile token → the actual `rtsp://.../Streaming/Channels/102`.

Most cameras require ONVIF auth (WS-UsernameToken: digest over username/password/nonce/
timestamp) on the Media/Device calls, even though Probe/ProbeMatch itself is
unauthenticated — credentials still need to come from somewhere (site config, or a
manual pairing step).

### Where it would sit in this architecture

> **What was actually built:** a separate LAN-only Flask app, `adapter/onvif-admin/`,
> rather than `agent.py`. The reasoning below pointed the right way — discovery has to
> run from a process on the camera's own segment — and that is precisely why it could
> *not* live in the cloud path: WS-Discovery is UDP multicast, so the browser client
> and Lambda structurally cannot reach it. The agent still owns Start/Stop and IR over
> MQTT; discovery and registration are the admin GUI's job.

- Natural owner: the **agent** (`agent.py`) or a companion daemon it starts — a discovery
  pass on boot and on-demand (e.g. an MQTT command `{"action":"discover"}` on the existing
  command topic).
- Discovered cameras populate the same per-channel shape, so nothing downstream (the
  `kvs-cam@.service` template, per-channel producer) changes — discovery only fills in
  the source URL. *(As built, that shape is a `cameras` registry row, not the
  `channels.json` imagined here.)*
- Fits the shadow-reporting idea from §16.6: a "discovered but unconfigured" camera could
  appear as a candidate before an operator assigns it a channel slot.

### Implementation options

- **Python** (matches `agent.py`'s existing stack, using `awsiotsdk`): `WSDiscovery` (or
  the `ws-discovery` PyPI fork) for the Probe/ProbeMatch exchange, plus `onvif-zeep` (or
  the maintained `onvif-zeep-async` fork) for the ONVIF SOAP calls via the official WSDLs.
  Lowest-friction path — no new language in the stack.
- **C/C++**: `libonvif`, or gSOAP-generated ONVIF client stubs (what most camera
  *firmwares* are built on server-side). More integration work; only worth it if the
  control plane were being rewritten in C for other reasons.

### Caveats

- **Multicast doesn't cross subnets/VLANs** without an IGMP-aware switch and, cross-VLAN,
  a multicast-aware router or reflector. Directly relevant to §3 below: once the camera
  segment is isolated, discovery must run from a device attached to that segment (i.e.
  bind the probe to the Pi's camera-facing interface), not from the trusted-LAN side.
- Conformance varies a lot below "Profile S certified" — some budget/OEM cameras
  implement WS-Discovery inconsistently or not at all. A manual-URL fallback stays
  necessary regardless; discovery is additive.
- This inconsistency is why the gap table rates it **M**, not **S**: the discovery
  handshake itself is roughly a day's work; robust profile/auth handling across
  heterogeneous firmware is where the effort actually goes.

---

## 3. VLANs and network isolation for the camera segment

### The problem

An ONVIF camera is one of the weaker-audited device classes on a home network (default
credentials, unpatched firmware, RTSP/ONVIF services with a spotty security record). This
maps to the "network isolation" gap in the main doc's table (§16.2):

| Capability | Cloud Adapter Mini | This prototype | Effort to close |
|---|---|---|---|
| Network isolation | dual NIC (Enterprise) | single LAN | **S** — second interface + routes |

### What a VLAN is

A VLAN (802.1Q tagging) splits one physical switch fabric into multiple logical broadcast
domains. Without it, every device on the same switch chain — including Wi-Fi clients on
the same router — is one broadcast domain: any broadcast or multicast (including a
WS-Discovery probe) reaches every port.

- **Access port** — untagged, belongs to one VLAN; what an end device plugs into.
- **Trunk port** — carries multiple tagged VLANs over one cable; used between switches or
  to a router that needs to see several VLANs.

Critically: **a switch enforces isolation, but only a router (or an L3-capable switch)
moves traffic *between* VLANs.** A pure VLAN switch with no routing isolates VLAN 10 from
VLAN 20 completely, including from the internet, unless the uplink device is also
VLAN/L3-aware.

### Current setup — no VLAN, one flat network

```
FritzBox 7583 (internet, LAN, WLAN) ── LAN/PoE switch ── ONVIF camera
```

The FritzBox's default subnet (`192.168.178.0/24`) is one broadcast domain. The camera,
any PC, any phone on Wi-Fi are all mutually visible — which is also why a WS-Discovery
probe from anywhere on this network already reaches the camera today with zero extra
configuration.

### FritzBox 7583 limitation

Consumer FritzBox routers **don't support configurable 802.1Q VLAN tagging on the LAN
side.** The LAN switch ports are one flat bridge — no per-port VLAN assignment, no trunk
mode. The only VLAN-ish feature is on the WAN side (some ISPs require a tagged VLAN for
DSL/IPTV uplink), unrelated to this use case. "Guest Wi-Fi" gives wireless clients a
logically separate, internet-only network, but that isolation doesn't extend to wired
ports and can't be routed into from a PoE switch.

**Consequence:** even with a fully VLAN-capable managed switch tagging the camera's port
into its own VLAN, the FritzBox cannot route between that VLAN and the trusted LAN, and
cannot give it internet access either — it doesn't understand VLAN tags on its LAN
interface at all.

### Recommended topology — no VLAN switch needed

Matches the doc's "dual NIC" gap-analysis entry directly: **the adapter itself is the
isolation boundary**, using two physical interfaces, rather than relying on the router to
do VLAN routing it can't do.

Given the Pi 4B's Wi-Fi is currently the uplink to the FritzBox, its **built-in Ethernet
port is free** — no USB-to-Ethernet adapter needed:

```
FritzBox 7583 ──(WLAN, trusted)── Pi wlan0   (AWS: MQTT, KVS — outbound)
                                     │
                                     │  (no IP forwarding between interfaces)
                                     │
                                  Pi eth0 (built-in) ──── PoE switch ──── ONVIF camera
                                  (isolated segment, own subnet, no internet)
```

- **No NAT/forwarding required.** The camera never needs outbound internet access; only
  the Pi's own processes (RTSP/ONVIF client) need to reach it, which happens natively as
  a host on that segment. Leave `net.ipv4.ip_forward` at its Linux default (off) and add
  no forwarding rule between `eth0` and `wlan0` — that absence *is* the isolation. Don't
  build anything; just don't accidentally enable routing later.
- **Addressing.** No FritzBox DHCP exists on this segment. Use a distinct private subnet
  (e.g. `192.168.50.0/24`, vs. the FritzBox's `192.168.178.0/24`) to avoid ambiguity.
  Either statically address both `eth0` (e.g. `192.168.50.1/24`) and the camera via its
  own web UI, or statically address `eth0` and run `dnsmasq` on the Pi to DHCP-serve just
  that interface.
- **A cheap unmanaged PoE switch is sufficient** — isolation comes from physical
  interface separation, not VLAN tagging. 802.1Q tagging only becomes the right tool if
  multiple isolated segments need to share one physical switch (several camera groups
  plus trusted devices on the same box) — not needed at the current one-camera scale.
- **Uplink trade-off, not a blocker:** the AWS-facing link (wlan0 → FritzBox) is Wi-Fi,
  inheriting its jitter/dropout characteristics. The outbound-MQTT design (doc §7)
  already tolerates reconnects, and this incidentally gives more realistic data for the
  §10.2 reconnect-behavior measurements than the synthetic `iptables DROP` test alone.
  (Since measured: `measurements/reconnect_timeline.md`. Note §10.2's own snippet does
  **not** work as written — it is IPv4-only, so on this dual-stack LAN every "blocked"
  connection went over IPv6 and succeeded. Use `adapter/bin/awsblock.sh`;
  `FoundAndFixed.md` #18.)
- **WS-Discovery interaction:** once this segment exists, bind the discovery probe to
  `eth0` explicitly (not the default route interface) — that's the interface actually
  attached to the camera's broadcast domain.

---

## 4. Codec choice: H.264 vs H.265, per camera

### Current state

Both channels are pinned to H.264 today, confirmed in the actual code, not just the
runbook:

- `adapter/onvif-admin/app.py` creates every KVS stream with `MediaType="video/h264"`
  (the path was `cloud/onvif-admin/` when this was written).
- `adapter/bin/stream-cam01.sh` (PW310, transcoded) and `adapter/bin/stream-cam02.sh`
  (real ONVIF camera, genuine passthrough) both use the identical H.264-specific
  GStreamer chain: `rtph264depay ! h264parse ! video/x-h264,... ! kvssink`.

`cam-02`'s camera supports H.265 as an alternate profile, but its registered `rtspUrl`
deliberately points at the H.264 profile (that URL lived in `mediamtx.yml` when this was
written; it is now registry-only — `FoundAndFixed.md` #31) — most ONVIF cameras expose both so an
integrator can pick whichever the downstream system supports. That's a choice, not a
camera limitation.

### Why cam-01 (PW310) has to stay H.264

Not a policy choice — a hardware fact. The Pi 4B's VideoCore VI exposes exactly one H.264
hardware encode block (`v4l2h264enc`, `/dev/video11`). There is a hardware HEVC block on
the BCM2711, but it's **decode-only** (used for 4K video playback), not encode. So `cam-01`
can never produce H.265 without falling back to software `x265enc`, which is considerably
more expensive than the `x264enc` software fallback already noted as a stopgap in the main
doc's §2.5. `cam-01` is architecturally stuck on H.264.

### Why cam-02 (real ONVIF camera, passthrough) is a different case

Initially considered and rejected on a "keep both channels symmetric for clean
measurements" argument — on reconsideration, that argument doesn't hold once the change
is scoped to `cam-02` only:

- The two channels were never actually coupled. `kvs-cam@.service` is a per-channel
  template and the per-camera registry row already treat each camera
  independently — codec is just another per-channel field, not something that needs to
  match across cameras.
- **Passthrough makes the codec free on the adapter.** `stream-cam02.sh` never decodes
  anything — `rtspsrc ! rtph264depay ! h264parse ! kvssink` is a byte-level RTP relay.
  Swapping to `rtph265depay ! h265parse` with `video/x-h265` caps costs the same
  near-zero CPU. Unlike `cam-01`, there's no encode-cost trade-off standing in the way at
  all.
- KVS's HEVC support (ingestion and `GetHLSStreamingSessionURL`/`GetDASHStreamingSessionURL`
  playback) is mature, not bleeding-edge — the friction isn't on the AWS side of the
  pipe.

This is actually a cleaner experiment than the existing transcode-vs-passthrough
comparison: same passthrough architecture, **codec as the only variable**, no
encoder-load confound. It would directly demonstrate H.265's ~40–50% bitrate/bandwidth
saving over H.264 at equal quality — directly relevant to the main doc's §16.6 conclusion
that uplink bandwidth, not the adapter, is the binding constraint at scale.

### The one real remaining constraint: browser playback

Client-side HEVC decode support in the browser HLS path (`hls.js`/MSE) is inconsistent:
reliable on Safari/iOS, generally unsupported on Chrome/Firefox desktop and on most
Android Chrome builds (a licensing gap, not a technical one). `cam-02`'s stream would
likely fail to render in the same generic browser client that plays `cam-01` fine, unless
viewed from an HEVC-capable browser/device. This is a genuine trade-off worth measuring
directly (test Chrome, Firefox, Safari, an Android phone; record what actually happens)
rather than assuming — in keeping with the main doc's "measure, don't assert" approach to
§10.

### Migration steps, if pursued

1. Point `cam-02`'s MediaMTX source at the camera's H.265 profile — via ONVIF
   `GetProfiles`/`GetStreamUri` on the HEVC profile token (most cameras that offer both
   expose them as separate profiles/paths).
2. `stream-cam02.sh`: swap `rtph264depay ! h264parse` → `rtph265depay ! h265parse`, caps
   to `video/x-h265`.
3. `MediaType` is set at KVS stream creation and can't be changed on an existing stream —
   `cam-02`'s current stream (`stream/cam-02/1788026766462`, referenced in
   `cloud/iam/clip-to-s3-policy.json`, `cloud/iam/get-hls-url-policy.json`,
   `cloud/iam/kvs-producer-policy.json`) would need to be deleted and recreated with
   `MediaType="video/h265"`, and those IAM policy ARNs updated to the new stream ARN.
4. Verify the producer SDK build actually has HEVC support compiled in before touching
   AWS — `gst-inspect-1.0 kvssink` and check its accepted caps — same discipline as the
   main doc's §2.6 ("verify before touching AWS").
5. Either accept `cam-02` only plays reliably on HEVC-capable clients, or measure that gap
   explicitly across browsers/devices as its own result.

### Cost implication

`COSTS-1.4.md` §7.4 works the dollar side of this (it was `COSTS-1.3.md` §6.4 when this
was written; v1.4 re-based `cam-02`'s main stream from 0.937 to **1.211 Mbps**, so the
absolute figures there are larger — the H.264 → H.265 @ 50 % delta is $3.94 → $1.97 of
KVS recording per camera-month). Switching `cam-02` to H.265 cuts KVS ingest and viewing
egress by roughly the same 40–50 % as the bitrate reduction, since KVS recording cost is
linear in bitrate. It also shows the saving is larger on KVS than on S3
(S3's PUT/index costs don't scale with bitrate at all), and prices out — qualitatively,
pending measurement — the two ways to close the browser-HEVC gap above: on-demand
transcode at playback time, or a dual H.265-archive/H.264-live stream pair using the Pi's
hardware HEVC decode block feeding the already-proven `v4l2h264enc` encode path.

---

## Open items

- [x] ~~Write `mediamtx.service` systemd unit~~ — done, `kvs-mediamtx.service`.
- [ ] Decide: keep MediaMTX, or prototype `gst-rtsp-server` as a one-process replacement.
      (Weaker now than when written: MediaMTX also does the outage-buffer recording,
      `OUTAGE.md`, so replacing it costs more than the RTSP relay.)
- [x] ~~Implement WS-Discovery~~ — done, but in `adapter/onvif-admin/` rather than
      `agent.py`, for the multicast reason in §2.
- [ ] Wire up the Pi's built-in `eth0` for the isolated camera segment; retire the
      camera's current path through the FritzBox's flat LAN.
- [ ] Decide whether to run `cam-02` on H.265 (see §4) — test the ONVIF HEVC profile URI
      and browser-compatibility matrix before recreating the KVS stream.
