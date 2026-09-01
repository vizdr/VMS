# Cost model: KVS MVP vs. S3 archive vs. Videoloft reference tariff

Companion to `Demo-AWS-Video.md`. Purpose: establish what the prototype architecture
would cost if operated as a product, compare it against a published commercial price, and
derive the architectural conclusion that follows.

**Status of the numbers.** AWS list prices, verified against the Kinesis Video Streams and
S3 pricing pages. **Revised August 2026** against measured data from the reference
product's own playback API (see `Demo-AWS-Video.md` Appendix B) — the bitrate assumption
was corrected from 1.5 Mbps to a measured ~0.9 Mbps, and every dependent figure with it. Region-dependent — Frankfurt (`eu-central-1`) runs a few percent above
`us-east-1`; the figures below use `us-east-1` list and are therefore mildly optimistic.
Enterprise discounts are not modelled. **Re-verify before quoting any of this**; the
ratios are robust, the absolute figures are ±40 % on the bitrate assumption alone.

---

## 1. Unit conversion

Video bitrate is quoted in **bits** per second; cloud billing is quoted in **bytes**.
Hence the factor 8 that appears in every calculation.

```text
  B Mbit/s
÷ 8         → MB/s          bits to bytes (Mb ≠ MB)
× 3600      → MB/hour
× 24        → MB/day
× 30        → MB/month       (30.44 is the true mean; ~1.5 % understated)
÷ 1000      → GB
```

Compact form for continuous recording:

```text
GB/month ≈ B(Mbps) × 324
```

Bitrate figures below are quiescent-scene values. Measured VBR outliers during vehicle
motion ran ~40 % higher, so **add 10–15 % for a scene with intermittent activity** and
more for continuous daytime traffic.

| Profile | Bitrate | GB/day | GB/month | Source |
|---|---|---|---|---|
| 720p15 (MVP, §2.7) | 1.0 Mbps | 10.8 | 324 | our encoder target |
| **2 MP @ 10 fps archive (measured)** | **0.9 Mbps** | **9.7** | **292** | **measured, Appendix B §19.5** |
| Live grid preview | below archive, unquantified | — | — | inferred, Appendix B §19.9 |
| 2 MP @ 10 fps (old assumption) | 1.5 Mbps | 16.2 | 486 | *superseded* |
| 1080p30 (reference) | 2.0 Mbps | 21.6 | 648 | typical |
| **`cam-02` sub-stream (measured, current)** | **0.047 Mbps** | **0.51** | **15.2** | **measured, 60s stream-copy capture, §6.4** |
| **`cam-02` main / 4MP stream (measured)** | **0.937 Mbps** | **10.1** | **303.6** | **measured, 60s stream-copy capture, §6.4** |
| **`cam-02` main / 4MP @ 10 fps (measured, benchmarking only)** | **0.6475 Mbps** | **7.0** | **209.8** | **measured, ONVIF-forced 10fps, §3.1** |

Note the corrected row against our own: **they carry 2.25× our pixel count at 90 % of our
bitrate.** §3.1 explains why.

`cam-02`'s own rows are our own measurement, not a proxy — see §6.4 for method and the
resulting recomputation of every `cam-02`-labelled figure downstream. The reference
product's 0.9 Mbps row above is unrelated to `cam-02` (it's Videoloft's own camera, not
ours) and stays as the baseline for §4.3/§5's revenue-viability argument, which is about
them, not us. The last row (10 fps) exists only to frame-rate-match that comparison
(§3.1) — it is not `cam-02`'s running configuration and isn't used anywhere in the cost
model, which stays on the 15 fps row above it.

Two corrections the arithmetic omits:

- **Protocol overhead.** The bitrate is H.264 payload. Container framing, TLS records and
  TCP/IP headers add **3–8 %** on the wire. Treat every figure as a floor.
- **Decimal vs. binary GB.** Networking uses 10⁹; AWS storage pricing generally means
  2³⁰. A ~7 % discrepancy — immaterial here, worth knowing in a review.

---

## 2. Unit prices used

| Service | Dimension | Rate |
|---|---|---|
| KVS | data ingested | $0.0085 / GB |
| KVS | data stored (hot tier) | $0.023 / GB-month |
| KVS | data consumed via HLS | $0.0119 / GB |
| KVS | data consumed, standard | $0.0085 / GB |
| S3 | data ingress | **free** |
| S3 | S3 Standard storage | $0.023 / GB-month |
| S3 | PUT / POST / LIST | $0.005 / 1,000 |
| S3 | GET | $0.0004 / 1,000 |
| Internet egress | S3 or KVS → internet | ~$0.09 / GB |
| CloudFront | egress to internet | ~$0.085 / GB |
| DynamoDB | on-demand write + storage | ~$0.07 / camera-month |

**Critical:** retrieving media from KVS to a destination outside AWS incurs standard data
transfer charges *on top of* the KVS consumption charge. Egress is ~7.5× the KVS HLS
consumption rate, so viewing cost is dominated by transfer, not by the service.

---

## 3. The reference tariff

Videoloft price calculator, captured August 2026:

> **1 × 2 MP camera, 24/7 continuous, 2-day cloud retention — €5.39 / month**

Longer retention tiers are offered up to 2 years; 30 days is marked "Popular". Their
published adapter specification caps cloud upload at 10 fps.

**Measured workload.** Seven consecutive 90-second media responses from their playback
API measured 10.01–10.10 MB (one 13.83 MB outlier during vehicle motion):

```text
10.05 MB × 8 ÷ 90 s = 0.893 Mbps
10.10 MB × 8 ÷ 90 s = 0.898 Mbps        →  ~0.9 Mbps

ingested      292 GB / month
stored         19.4 GB steady state  (2 days × 9.7 GB/day)
```

Remaining unknowns: whether €5.39 includes VAT, and the EUR/USD rate used here
(~$1 ≈ €0.91).

### 3.0 Three profiles, not one

The reference product maintains **three encoding profiles per camera**, and conflating
them is the easiest way to get a cloud-video cost model badly wrong:

| Profile | Bitrate | Used for | Cost driver |
|---|---|---|---|
| Archive | ~0.9 Mbps (measured) | 24/7 recording | ingest + storage, 720 h/month |
| Live preview | below archive, unquantified | multi-camera grid | egress, only while watched |
| Live full | unmeasured | single-camera focus | egress, briefly |

> **Honesty note.** An earlier revision quoted the preview tier at ~0.05 Mbps and derived
> a "20× cheaper" figure from it. That rested on dividing a session's total bytes by the
> full camera list, when only a handful of tiles were actually streaming and only for a
> couple of minutes (Appendix B §19.9). The number is withdrawn; the *direction* is not
> in doubt — grid tiles are ~200 px with a stills/video toggle, and observed part sizes
> span 0.06–0.56 Mbps.

The conclusion survives without the number. Viewing cost is dominated by egress at
~$0.09/GB whatever the architecture, so **any** reduction in preview bitrate translates
linearly into reduced viewing cost. Our MVP has one profile; adding a downscaled preview
profile is the highest-value cost optimisation available to it, and the payoff scales with
the number of tiles on screen.

### 3.1 Why 0.9 Mbps and not 1.5

The original 1.5 Mbps assumption applied a generic bits-per-pixel figure. Normalising
removes the resolution difference and exposes the real gap:

```text
bits per pixel per frame = bitrate ÷ (width × height × fps)

measured (theirs)   900,000 ÷ (1920 × 1080 × 10) = 0.0434 bpp
our MVP target    1,000,000 ÷ (1280 ×  720 × 15) = 0.0723 bpp
old assumption    1,500,000 ÷ (1920 × 1080 × 10) = 0.0723 bpp   ← the error
```

The assumption reused our own bits-per-pixel figure. Theirs is **40 % lower**, for
reasons that are mostly *not* about encoder skill:

1. **The bitrate is a camera setting, not their achievement.** `x-vl-transcoded: false`
   proves the media is passed through unmodified. Whatever the installer configured on
   the camera is what gets stored. Videoloft exercises no control over it at all.
2. **A static night scene compresses extremely well.** An empty car park at 10 fps is
   almost entirely skip macroblocks; P-frames collapse to near nothing. Our webcam
   pointed at a moving subject is a far harder source.
3. **Camera-side noise reduction.** Sensor noise at night is high-entropy and defeats
   motion estimation, so surveillance cameras apply aggressive temporal 3DNR precisely to
   stop bitrate exploding. That filtering happens before the encoder.
4. **Encoding tools.** A camera SoC encoder typically runs High profile with CABAC;
   Baseline/CAVLC costs 10–20 % more bits for the same quality.
5. **No transcode generation loss.** Our MJPG → decode → H.264 path forces the encoder to
   spend bits reproducing JPEG artefacts. Encoding a pristine source is always cheaper
   than re-encoding a lossy one.
6. **Hardware encoder efficiency.** `v4l2h264enc` on the Pi has limited rate control and
   no B-frames; hardware encoders commonly need 20–40 % more bits than `x264` at equal
   quality.

**Conclusion: our pipeline structurally cannot match this, and should not try.** Items
1, 2, 5 and 6 are properties of the prototype's constraints, not defects. The correct
response is to document the gap and note that the pass-through path (§16.3b of
`Demo-AWS-Video.md`) eliminates items 5 and 6 entirely.

**`cam-02`, measured at the same 10 fps, beats even the reference figure.** All of the
`cam-02` bitrates elsewhere in this document (§6.4: 0.047 / 0.937 Mbps) were measured at
`cam-02`'s own 15 fps — not frame-rate-matched to Videoloft's 10 fps, so not directly
comparable on a bpp basis to the row above. Measured separately, purely for this
comparison: ONVIF `FrameRateLimit` set to 10 on `cam-02`'s main-stream encoder
configuration for the duration of a 60-second capture, then reverted to its normal 15 fps
immediately after — no other change:

```text
cam-02 @ 10fps (measured)   647,500 ÷ (2560 × 1440 × 10) = 0.0176 bpp
```

At matched frame rate, `cam-02`'s camera runs **~59 % lower bpp than Videoloft's own
figure** (0.0176 vs. 0.0434 — about 2.5× more efficient), while carrying **1.78× the
pixel count** (2560×1440 vs. 1920×1080). Items 1–4 above explain the shape of this too —
`cam-02` is also genuine passthrough, also apparently a static/indoor scene during the
test window, and items 5–6 don't apply to it at all (no MJPG transcode step, no
`v4l2h264enc` rate-control limitations — passthrough, same as the reference product's own
setup). What this *doesn't* mean: that 0.0176 bpp is `cam-02`'s general-purpose
efficiency figure — it's one 60-second sample under whatever lighting and motion
happened to be present, and §1's quiescent-scene caveat applies here as much as
anywhere. Re-measure under varied conditions before relying on it beyond this
comparison. **Scoped deliberately:** this 10 fps figure is for benchmarking against
Videoloft only — the cost-model figures in §4.1/§6.4/§7.1/§8 stay keyed to `cam-02`'s
actually-configured 15 fps (0.937 Mbps), since that's what would actually be ingested if
the main stream were switched on, not the temporary 10 fps test configuration.

---

## 4. Separating recording cost from viewing cost

The single most useful modelling decision. **Recording cost is architecture-dependent;
viewing cost is very nearly architecture-independent** because internet egress dominates
it and egress is priced the same whatever produced the bytes.

### 4.1 Recording — per camera, 24/7, 0.9 Mbps measured, 2-day retention

| Line item | KVS (MVP design) | S3 archive |
|---|---|---|
| Ingest 292 GB | $2.48 | **$0.00** |
| Storage 19.4 GB-month | $0.45 | $0.45 |
| PUT requests (60 s segments, 43.2 k) | — | $0.22 |
| Index (DynamoDB + Lambda) | included | $0.07 |
| **Recording subtotal** | **$2.93** | **$0.74** |
| | ≈ €2.67 | ≈ €0.67 |

**Ratio: 4.0×.** One line item accounts for essentially all of it — KVS charges $0.0085
for every ingested gigabyte, S3 charges nothing for ingress.

**`cam-02`, measured, is a different camera from the row above** — the table above uses
the reference product's own 0.9 Mbps (their camera, §3). `cam-02` is ours, measured
directly (§6.4: 60s stream-copy capture): its main/4MP stream runs **0.937 Mbps**, close
enough to 0.9 that the two are easy to conflate, but it's a real number for our own
camera, not a stand-in for theirs. Recording cost at that measured bitrate, H.264 vs.
H.265 (§6.4's hardware caveat still applies — `cam-01` can't do this):

| | H.264 (measured) | H.265 @ 40 % cut | H.265 @ 50 % cut |
|---|---|---|---|
| KVS recording | $3.05 | $1.83 | $1.52 |
| S3 recording | $0.76 | $0.57 | $0.52 |
| vs. its own H.264 figure (KVS) | — | 60 % | 50 % |
| vs. its own H.264 figure (S3) | — | 75 % | 68 % |

**The KVS:S3 ratio narrows from 4.0× to ~2.9–3.2×.** Not because S3 gets relatively more
expensive — it drops too — but because KVS's recording cost is nearly all ingest (moves
almost 1:1 with bitrate) while S3's is only partly storage (moves with bitrate) plus
fixed PUT/index charges (don't move at all). The same asymmetry that shifts the §7.1
fleet break-even shows up here as a shrinking, not closing, ratio.

`cam-02`'s currently-configured stream (the sub-stream MediaMTX actually pulls, measured
0.047 Mbps) is far below either of these — KVS recording ≈$0.15, S3 ≈$0.31. At that
bitrate codec choice barely moves the bill either way; the H.265 comparison above only
matters if `cam-02` (or a future camera) runs at main-stream quality.

### 4.2 Viewing — per GB actually watched

| | KVS HLS | S3 + CloudFront |
|---|---|---|
| Service charge | $0.0119 | ~$0.0000 (GET) |
| Internet egress | $0.09 | $0.085 |
| **Per GB watched** | **$0.102** | **$0.085** |

Roughly comparable, and both are dominated by transfer. CloudFront also caches, so a
second operator watching the same footage is nearly free; S3-direct and KVS both charge
again.

**The break-even that makes this concrete.** A measured 53-minute playback session on the
reference product transferred 373 MB — one operator, one camera (Appendix B §19.5). At
$0.09/GB that is ~$0.034 of egress. Against $0.74/month to *record* that camera on S3:

```text
$0.74 ÷ ($0.034 / 0.89 h) ≈ 19 hours
```

**Roughly 20 hours of viewing per month costs as much as recording that camera 24/7 for
the entire month.** A security desk with a video wall inverts the economics completely —
continuous viewing of a single camera would cost ~36× its recording cost.

**Under H.265, that 20-hour figure moves — and not by the same 40–50 % as everything
else.** This is still about the reference product's own hypothetical (their 0.9 Mbps,
their measured session) — not `cam-02`, which has its own, separate H.265 treatment in
§6.4/§7.1 now that it's been measured. Recomputing the S3 recording cost at their 0.9 Mbps
under the same two cuts (storage line only: $0.45×0.6+$0.29=$0.56 at 40%,
$0.45×0.5+$0.29=$0.51 at 50%) — the same method as §6.4. The $/GB rate itself doesn't
change; what changes is how many GB one hour of viewing costs, since H.265 needs fewer
bytes for the same footage. Redoing the arithmetic at 0.2534 GB/h (40 % cut) and
0.2112 GB/h (50 % cut) against that H.265 recording cost ($0.56 / $0.51, not the H.264
$0.74) — keeping the same $0.09/GB generic egress rate the baseline example above used
(not the $0.085 CloudFront-specific rate from the table above, since this traffic is the
reference product's own, of unknown delivery architecture):

```text
H.265 @ 40 %:  $0.56 ÷ ($0.2534 GB/h × $0.09/GB) ≈ 25 hours
H.265 @ 50 %:  $0.51 ÷ ($0.2112 GB/h × $0.09/GB) ≈ 27 hours
```

**The break-even stretches from ~20 hours to ~25–27 hours.** Recording cost and viewing
cost don't shrink by the same amount under H.265 — recording carries S3's fixed PUT/index
charges that don't respond to bitrate at all, while viewing cost is pure $/GB and moves
in lockstep with bitrate — so a more efficient codec buys more viewing headroom per
dollar of recording cost, the same asymmetry as §4.1 and §7.1, one level down.

Three consequences follow directly:

- **Sub-stream for live monitoring, main stream only for evidence.** This is a cost
  decision an order of magnitude more significant than any storage optimisation.
  The reference product implements exactly this — a downscaled preview tier for the grid,
  separate from the archive stream (Appendix B §19.9). The magnitude of the saving is not
  measurable from the captures available, but the mechanism is: egress is linear in bytes,
  so halving preview bitrate halves the cost of every tile on every wall, every hour.
- **CDN caching is not a nicety.** When several operators watch the same footage, cache
  hits are the difference between paying once and paying N times. The reference product
  forgoes this (no `via:`/`age:`/`x-cache:` headers) because arbitrary-range serving is
  near-uncacheable — a real, ongoing cost of that design choice.
- **Playback pulls at roughly real time**, not in large read-ahead bursts (the measured
  session averaged 0.94 Mbps of fetch across 53 minutes), so viewing cost accrues
  linearly with watch time and is straightforward to forecast per seat.

### 4.3 Total against the reference tariff

Assuming light viewing (5 GB/month/camera):

| | Recording | Viewing | Total | vs. €5.39 (≈$5.92) retail |
|---|---|---|---|---|
| MVP as built (KVS) | $2.93 | $0.51 | **$3.44 ≈ €3.13** | **58 % of revenue** |
| S3 archive | $0.74 | $0.43 | **$1.17 ≈ €1.06** | 20 % of revenue |

The earlier revision of this document put the KVS figure at 91 % of revenue on the
1.5 Mbps assumption. The corrected 58 % is less dramatic but leads to the same place: a
SaaS business needs infrastructure COGS below roughly 20–25 %, and only the S3 column
reaches it — before support, engineering, the viewing application, payment processing or
sales.

**Under H.265**, viewing volume is cut the same way as §4.2 (3.0 GB / 2.5 GB instead of
5.0 GB), priced at each service's own rate:

| | Recording | Viewing | Total | vs. €5.39 (≈$5.92) retail |
|---|---|---|---|---|
| KVS, H.265 @ 40 % | $1.76 | $0.31 | **$2.07** | **35 % of revenue** |
| KVS, H.265 @ 50 % | $1.46 | $0.26 | **$1.72** | **29 % of revenue** |
| S3, H.265 @ 40 % | $0.56 | $0.26 | **$0.82** | 14 % of revenue |
| S3, H.265 @ 50 % | $0.51 | $0.21 | **$0.72** | 12 % of revenue |

H.265 takes the KVS design from 58 % of revenue down to **29–35 %** — a real
improvement, and closer to viable than it looked at H.264. **It still doesn't cross the
20–25 % COGS threshold.** The S3 design, meanwhile, drops further still, from 20 % to
**12–14 %**, widening its already-comfortable margin. Codec efficiency narrows the gap
between the two architectures; it doesn't reverse which one is viable.

---

## 5. The conclusion this forces

**Videoloft cannot be using Kinesis Video Streams for bulk archive.** At their published
price and measured bitrate, KVS raw infrastructure consumes ~58 % of the retail line
before a single non-infrastructure cost is counted. No SaaS business survives that.

Independent confirmation from Appendix B: their playback API serves **arbitrary time
ranges** (`x-vl-seek: 1.144` on an unaligned request), remuxes container formats on the
fly (`x-vl-transcoded: false`, `/stream/mpegts/` as a path segment), and returns none of
the headers KVS emits. This is a custom packager over object storage, not KVS.

The S3 design lands at 20 % at list prices, and materially better with committed-spend
discounts — which a fleet of their size will certainly hold. That is a viable business.

**What they must therefore have built themselves:** the time index, the HLS packager, and
clip export — precisely the three things KVS sells. The trade is a one-off engineering
cost against $2.27 per camera per month, forever (§4.3's total recording-plus-viewing
Delta at the measured 0.9 Mbps bitrate — a correction from an earlier revision of this
document, which quoted $3.84 here, carried over from the superseded 1.5 Mbps assumption
and never updated alongside it).

**A robustness check, not a rescue: would H.265 change this conclusion?** No — and it's
worth stress-testing rather than assuming. §4.1/§4.3 show that even in the most favorable
case for KVS — a stream already well-tuned per §3.1 (High-profile CABAC, no transcode
generation loss, camera-side noise reduction) *plus* a further best-case HEVC cut on top
— a KVS-based design still lands at **29–35 % of revenue**, above the 20–25 % ceiling a
SaaS business needs. There is no codec assumption within HEVC's realistic savings range
that makes a naive KVS-backed archive viable at this retail price. This also means §5's
conclusion doesn't depend on knowing which codec the reference product actually runs
(nothing in Appendix B indicates HEVC — §3.1's gap is fully explained by H.264 tuning
choices, not a codec difference) — the structural argument holds regardless: bulk archive
on KVS doesn't clear this bar, codec choice narrows the gap but never closes it, so a
custom S3-based packager remains the only architecture that does.

---

## 6. Sensitivity

### 6.1 Bitrate (dominant uncertainty)

| Bitrate | GB/mo | KVS recording | S3 recording | Ratio |
|---|---|---|---|---|
| **0.9 Mbps (measured)** | 292 | $2.93 | $0.74 | 4.0× |
| 1.0 Mbps (MVP target) | 324 | $3.25 | $0.83 | 3.9× |
| 1.5 Mbps | 486 | $4.88 | $1.04 | 4.7× |
| 2.0 Mbps | 648 | $6.51 | $1.25 | 5.2× |

KVS scales almost linearly with bitrate (ingest dominates). S3 barely moves, because its
cost is storage and requests — **and requests don't depend on bitrate at all.** The
architectures diverge further as quality rises.

### 6.2 Segment length — a first-order decision

At $0.005 per 1,000 PUTs, continuous recording generates a surprising request bill:

| Segment | PUTs/camera/month | PUT cost | vs. storage ($0.75) |
|---|---|---|---|
| 6 s (HLS convention) | 432,000 | $2.16 | **288 %** |
| 10 s | 259,200 | $1.30 | 173 % |
| 30 s | 86,400 | $0.43 | 58 % |
| 60 s | 43,200 | $0.22 | 29 % |
| 300 s | 8,640 | $0.04 | 6 % |

> **This parameter is derived, not observed.** The reference product's read path is
> decoupled from its storage cadence — `x-vl-seek: 1.144` on an unaligned request shows
> the packager cuts arbitrarily rather than serving stored objects directly, so no
> segment length is visible from outside. The 60 s figure below follows from the request
> arithmetic and is a sound design choice; it is *not* evidence of what they store.

At the HLS-conventional 6 seconds, **request charges are 3× storage charges** and the S3
advantage collapses. Long segments are therefore mandatory for the
archive path — and long segments mean high live latency (players buffer ~3 segments), so
live must run on a separate path. **The cost model dictates the architecture.**

### 6.3 Retention

Storage reaches steady state: with continuous recording and N-day retention you hold
exactly N days' worth regardless of how long the system runs.

| Retention | Held (0.9 Mbps) | S3 Standard | Note |
|---|---|---|---|
| 2 days | 19.4 GB | $0.45 | the reference plan |
| 7 days | 68 GB | $1.56 | |
| 30 days | 292 GB | $6.72 | IA viable beyond this point |
| 1 year | 3.5 TB | $81 | Glacier tiers essential |

This is why the published price ladder from 2 days to 2 years is far flatter than
intuition suggests: **storage is the cheap dimension; ingest and egress are not.**

Two traps when tiering: S3 Standard-IA bills a 30-day minimum duration and Glacier
classes 90–180 days, so tiering footage that expires at 2 days *increases* cost. And each
Glacier object carries ~40 KB overhead — archive 60 s segments, never 6 s ones.

### 6.4 Codec — H.265 on `cam-02`

`cam-02` (the real ONVIF camera, running genuine passthrough) is the only channel where
this is viable — `cam-01` (PW310) is hardware-locked to H.264 encode; the Pi's VideoCore
VI has no HEVC encode block (`NETWORK.md` §4).

**Measured directly, 2026-09-01**, running on the Pi with MediaMTX live: 60-second
`ffmpeg -rtsp_transport tcp -i <url> -t 60 -c copy -f mp4` stream-copy captures of each
stream, bitrate computed as video-payload bytes × 8 ÷ actual capture duration — avoids
relying on RTSP's unreliable live bitrate metadata, and avoids re-encoding, which would
change the number being measured. (A third figure, `cam-02`'s main stream forced to
10 fps via ONVIF to frame-rate-match Videoloft's own reference figure, lives in §3.1
rather than here — it's a benchmarking comparison, not a cost-model input, since it was
never `cam-02`'s running configuration.)

| Stream | Resolution | Configured in `mediamtx.yml` | Measured bitrate |
|---|---|---|---|
| `stream1` (sub) — **what `cam-02` actually ingests today** | 640×360 @ 15 fps | yes (`cam02` path source) | **0.047 Mbps** |
| `stream0` (main, "4MP") — not currently used | 2560×1440 @ 15 fps | no | **0.937 Mbps** |

Two consequences follow before any H.265 arithmetic:

- **`cam-02`'s actual recording cost today is trivial regardless of codec.** At
  0.047 Mbps, KVS recording ≈$0.15/camera-month (2-day retention, §4.1) — about 19× below
  every H.264 baseline this document previously used as a stand-in for `cam-02`. H.265
  would save a few cents here; not worth pursuing on its own.
- **The 0.9 Mbps figure this document used earlier as a provisional proxy for `cam-02`
  turns out to almost exactly match the *main* stream (0.937 Mbps), not the stream
  actually configured.** That was luck, not derivation — the proxy was borrowed from the
  reference product's own measured bitrate (§3), a different camera entirely. The table
  below uses the real 0.937 Mbps figure, replacing the placeholder.

| H.264 baseline | KVS $ (H.264) | H.265 @ 40 % cut | KVS $ (H.265) | H.265 @ 50 % cut | KVS $ (H.265) |
|---|---|---|---|---|---|
| **0.047 Mbps (measured, current sub-stream)** | **$0.15** | 0.028 Mbps | $0.09 | 0.024 Mbps | $0.08 |
| **0.937 Mbps (measured, main/4MP stream)** | **$3.05** | 0.562 Mbps | $1.83 | 0.469 Mbps | $1.52 |
| 1.0 Mbps (MVP target, for reference) | $3.25 | 0.60 Mbps | $1.95 | 0.50 Mbps | $1.63 |
| 1.5 Mbps (for reference) | $4.88 | 0.90 Mbps | $2.93 | 0.75 Mbps | $2.44 |
| 2.0 Mbps (for reference) | $6.51 | 1.20 Mbps | $3.90 | 1.00 Mbps | $3.25 |

The H.265 comparison only has practical weight for the main-stream row — the sub-stream
row is included for completeness, not because a few cents of saving justifies the
engineering effort described in the browser-HEVC complication further down this
section.

**Because KVS recording cost is linear in bitrate (ingest dominates), the dollar saving
equals the bitrate saving exactly — 40–50 % off, no discounting.** That is *not* true on
the S3 side: per §4.1, only the storage line (~62 % of S3's $0.76 recording total at
0.937 Mbps) scales with bitrate — PUT and index costs are per-segment, not per-byte. The
same H.265 switch would cut S3 recording cost by only ~25–32 %. Put
differently: **H.265 pays off hardest on exactly the architecture (KVS) this document
otherwise argues against at scale** — it narrows, but does not close, the ~4× ratio in
§4.1.

**Viewing cost compounds the same saving.** KVS HLS and S3+CloudFront egress are both
priced per GB transferred (§4.2, ~$0.10/GB watched); H.265 output is fewer bytes for the
same footage, so that line drops by the same ~40–50 % for any viewer that can decode it
directly. Recording and viewing savings stack, unlike a segment-length change (§6.2),
which only ever touches the request-count line.

**The complication this table doesn't price in: browser HEVC playback isn't guaranteed**
(`NETWORK.md` §4). `hls.js` in Chrome/Firefox/most Android builds can't reliably decode
HEVC; only Safari/iOS is dependable. Two ways to close that gap, neither costed above:

- **On-demand transcode at playback time**, only for non-HEVC clients — no continuous
  second ingest stream, but a real per-viewing-minute compute cost (Lambda has no
  practical path to sustained HW-accelerated transcode; would need a small always-on
  service or Elemental MediaConvert) that isn't quantified here and must be verified
  against current pricing before it's assumed cheaper than the egress it saves.
- **Dual continuous streams** — H.265 archive plus a separate H.264 "live" profile, using
  the Pi's hardware HEVC *decode* block (BCM2711 has one, unlike encode) feeding
  `v4l2h264enc` already proven on `cam-01`. This reproduces the archive/live-preview
  split §3.0 and §4.2 already identify as the reference product's highest-value cost
  lever — except now the archive leg also gets the codec-efficiency win. The trade-off:
  it means paying KVS ingest **twice**, so the codec saving and the dual-stream cost must
  be netted against each other, not assumed independently additive.

Bitrate is now measured (above); what's still a model, not yet a measurement: confirm
`gst-inspect-1.0` exposes a working HEVC decode element on this Pi's build, and rerun the
same 60-second capture method after any actual H.265 switch to confirm the 40–50 % cut
assumption holds for this specific camera and scene rather than just the general HEVC
literature figure.

---

## 7. Fleet scaling

Delta = $2.19 per camera-month (recording only, 0.9 Mbps measured, 2-day retention).

| Cameras | KVS/month | S3/month | Saving/month | Saving/year |
|---|---|---|---|---|
| 1 | $2.93 | $0.74 | $2.19 | $26 |
| 10 | $29.30 | $7.40 | $22 | $263 |
| 100 | $293 | $74 | $219 | $2,628 |
| 1,000 | $2,930 | $740 | $2,190 | $26,280 |
| 10,000 | $29,300 | $7,400 | $21,900 | $262,800 |
| 100,000 | $293,000 | $74,000 | $219,000 | $2,628,000 |

**Break-even on the build.** Assume 3 engineer-months at a loaded €10 k/month ≈ $33 k to
implement segmenting, upload, index, packager and clip export.

```text
$33,000 ÷ $2.19 ≈ 15,100 camera-months
```

So: ~15,100 cameras for one month, or **~1,250 cameras run for a year**. Below a few
hundred cameras, KVS is the rational choice. Above a couple of thousand, building is
obviously correct and the gap compounds indefinitely. This is the number that explains
the product's architecture — and note that it moved by 74 % on a single corrected input,
which is why §10 insists on measurement over estimation.

### 7.1 Delta under H.265 — the break-even moves the *other* way

Intuition says a cheaper codec should make building your own pipeline pay off sooner.
It's the reverse, and the reason is worth sitting with: H.265 erodes KVS's one structural
weakness (per-GB ingest) far more than it erodes S3's, because S3's recording cost was
never ingest-driven to begin with — its PUT/index lines don't move with bitrate at all
(§6.4). Cutting bitrate therefore shrinks the *numerator* of the Delta much faster than
the *denominator*, and the Delta itself is what §7's whole break-even calculation runs
on.

Using `cam-02`'s own measured main-stream bitrate (0.937 Mbps, §6.4 — not the 0.9 Mbps
placeholder this section used before measurement) and §6.4's two HEVC scenarios, applied
only to `cam-02`-type passthrough channels (§6.4's hardware caveat still applies —
`cam-01` can't do this):

| Scenario | KVS recording | S3 recording | Delta/camera-month | Delta/camera-year |
|---|---|---|---|---|
| H.264 baseline (0.937 Mbps, measured) | $3.05 | $0.76 | $2.29 | $27.48 |
| H.265 @ 40 % cut (0.562 Mbps) | $1.83 | $0.57 | $1.26 | $15.12 |
| H.265 @ 50 % cut (0.469 Mbps) | $1.52 | $0.52 | $1.00 | $12.00 |

(This baseline row is close to but not identical to §7's own $2.19/~1,250 figure — that
one is the reference product's own measured 0.9 Mbps, a different camera; this one is
`cam-02`'s own measurement. Two real numbers, coincidentally close, not the same thing.)

Re-running the same $33 k build-cost break-even against the smaller Delta:

```text
H.264 baseline:  $33,000 ÷ $2.29 ≈ 14,400 camera-months  ≈ 1,200 camera-years
H.265 @ 40 %:    $33,000 ÷ $1.26 ≈ 26,200 camera-months  ≈ 2,180 camera-years
H.265 @ 50 %:    $33,000 ÷ $1.00 ≈ 33,000 camera-months  ≈ 2,750 camera-years
```

**H.265 pushes the break-even from ~1,200 to roughly 2,200–2,750 cameras run for a
year** — nearly double. A more efficient codec doesn't just save money in absolute terms;
it makes the *managed service* rational for longer, precisely because it attacks KVS's
weakest line item harder than it attacks S3's. A real fleet's actual break-even sits
between the H.264 and H.265 lines above, weighted by how many channels are
passthrough-capable (`cam-02`-like) versus transcode-locked (`cam-01`-like) — this isn't
a single number until the fleet's camera mix is known.

**Check: does folding in viewing cost change this?** The table above is recording-only,
matching §7's own stated scope. But §6.4 also claims H.265 compounds the saving on the
viewing side, and that claim was never run through the break-even math above — it should
be. There's no measured viewing behaviour for `cam-02` (nobody's watched it enough to
measure), so this reuses §4.3's viewing-volume assumption (5 GB/month/camera, itself
borrowed from the reference product's measured session) as the best available proxy —
worth flagging as a borrowed assumption sitting alongside a real measurement, not another
measurement itself.

Applying the same H.265 cut to that 5 GB assumption (fewer bytes for the same watch time)
and pricing it at both services' HLS/CloudFront rates (§4.2: $0.102/GB KVS, $0.085/GB
S3+CloudFront) — these viewing-side figures are unchanged by `cam-02`'s bitrate
correction, since viewing volume was never derived from the recording bitrate:

| Scenario | Recording Delta | Viewing volume | Viewing Delta | **Total Delta** | Break-even |
|---|---|---|---|---|---|
| H.264 baseline | $2.29 | 5.0 GB | $0.08 | **$2.37** | ~1,160 camera-years |
| H.265 @ 40 % cut | $1.26 | 3.0 GB | $0.05 | **$1.31** | ~2,100 camera-years |
| H.265 @ 50 % cut | $1.00 | 2.5 GB | $0.05 | **$1.05** | ~2,620 camera-years |

(Viewing Delta here is the KVS-viewing-minus-S3-viewing figure straight from §4.3's own
table — $0.51−$0.43, $0.31−$0.26, $0.26−$0.21 — so it's directly cross-checkable against
that table rather than a separately-rounded number.)

Including viewing cost moves every break-even figure slightly *earlier* (more total
savings per camera than recording alone gives), including the H.264 baseline itself
(~1,160 here vs. the ~1,200 recording-only figure above). But it doesn't change the H.265
finding in any material way: **total-Delta break-even lands at ~2,100–2,620
camera-years, essentially the same near-doubling** as the recording-only ~1,200→2,200–
2,750. That's expected, not a coincidence — §4 already established that viewing cost is
"very nearly architecture-independent" (KVS-HLS and S3+CloudFront egress rates are close,
$0.102 vs. $0.085/GB) precisely because both are dominated by the same internet-egress
charge, so folding it in barely moves a conclusion that recording cost already drives on
its own.

---

## 8. What the MVP actually costs to run

The prototype is not operated 24/7, so demo cost is trivial either way.

| Scenario | Config | KVS cost |
|---|---|---|
| Weekend testing, 6 h streaming | 720p15, 1.0 Mbps, 24 h retention | ~$0.05 |
| One camera continuous, 1 month | same | ~$3.50 |
| Ten channels continuous, 1 month | same | ~$35 |

**Cost is not the reason to implement the S3 path in the prototype.** At one camera the
difference is cents. The reason is to demonstrate that the trade-off is understood, and
to have built the same capability from primitives as well as from a managed service.

**What `cam-02` itself actually costs, now measured (§6.4) — replaces an earlier version
of this table that incorrectly applied the H.265 cut to the 1.0 Mbps `cam-01`/MVP figures
above instead of `cam-02`'s own bitrate.** The rows above are `cam-01`'s (PW310,
720p15 MVP target); `cam-02` is a different camera with its own measured numbers, at 24 h
retention to match this section's convention:

| Scenario | KVS cost, sub-stream (0.047 Mbps, current) | KVS cost, main/4MP (0.937 Mbps, measured) | KVS cost, main/4MP under H.265 (40–50 % cut) |
|---|---|---|---|
| Weekend testing, 6 h streaming | ~$0.001 | ~$0.02 | ~$0.01 |
| One camera continuous, 1 month | ~$0.14 | ~$2.81 | ~$1.41–1.69 |
| Ten channels continuous, 1 month (all at main-stream quality) | ~$1.40 | ~$28.10 | ~$14.10–16.90 |

`cam-02` today (sub-stream column) costs less than the rounding error in the `cam-01`
table above it — codec choice is genuinely irrelevant at this bitrate. The middle and
right columns are the realistic comparison if `cam-02` (or a future camera) is switched
to main-stream quality for evidentiary value: H.264 main-stream costs about as much as
`cam-01`'s own MVP config despite carrying **4×** the pixel count (2560×1440 vs
1280×720 — 3,686,400 vs 921,600) — a data point for §16.6's per-channel-cost story once
multi-channel testing starts. At
this single-camera scale the absolute saving is still cents to a couple of dollars — not
the reason to do this yet. The saving only becomes a real number at fleet scale, which is
exactly what §7.1 works out.

Set a $10 monthly budget alarm regardless (`Demo-AWS-Video.md` §1.1). The realistic
failure mode is a `gst-launch` left running for a week, not a design error.

---

## 9. Excluded from this model

Understating these does not change the conclusion, but they should be named:

- **KVS warm tier** — lower storage rate, priced per 1,000 fragments persisted rather
  than per GB, 30-day minimum retention. Worth modelling for long-retention tiers; it
  does not help the 2-day case.
- **KMS** — KVS rotates its data key roughly every 45 minutes; a few dollars per month
  per account, not per camera.
- **Lambda, API Gateway, Cognito, IoT Core** — cents per camera at this scale.
- **CloudFront request charges and minimum commitments.**
- **The packaging tier.** The reference product runs a dynamic packager
  (Appendix B). Because `x-vl-transcoded: false` shows it only remuxes containers rather
  than re-encoding, this is I/O and container rewriting — real, but far cheaper than
  transcode compute. Not modelled here.
- **CDN absence.** Their responses carry no `via:`, `age:` or `x-cache:` headers, so
  arbitrary-range serving forgoes caching and pays full origin egress. Our design's
  pre-generated playlists over static objects *are* cacheable — a genuine advantage of
  the simpler approach, not modelled here either.
- **Support plans, cross-region replication, backup.**
- **The adapter hardware**, sold separately in the commercial product.
- **Enterprise discount programmes** — materially reduce both columns, roughly
  proportionally, so the ratio survives.

---

## 10. How to verify against reality

Do not ship this document with estimates alone. Three measurements make it credible —
one of the three is now done for `cam-02`, method and result in §6.4: a 60-second
`ffmpeg -c copy` stream capture rather than the interface-counter method below, but the
same principle (measure the actual bytes, don't trust live-stream bitrate metadata).
`cam-01` and the AWS-side cross-checks below are still open:

```bash
# 1. Actual bytes on the wire vs. the arithmetic (expect +3–8 %)
cat /sys/class/net/eth0/statistics/tx_bytes    # sample before and after a timed run

# 2. Actual ingested volume as AWS counted it
aws cloudwatch get-metric-statistics --namespace AWS/KinesisVideo \
  --metric-name PutMedia.IncomingBytes --dimensions Name=StreamName,Value=cam-01 \
  --start-time 2026-08-19T00:00:00Z --end-time 2026-08-20T00:00:00Z \
  --period 3600 --statistics Sum

# 3. Actual billed cost by service
aws ce get-cost-and-usage --time-period Start=2026-08-01,End=2026-08-31 \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE
```

Tag every resource with `project=vms-demo` so Cost Explorer can filter. Publishing
measured-versus-modelled in the README is worth more than either figure alone.

---

## 11. Summary

1. Cloud video cost is driven by **ingest** and **egress**, not storage.
2. KVS charges per GB ingested; **S3 ingress is free**. At 292 GB/camera/month that one
   asymmetry is $2.48 versus $0.00 — and it is the only line item that materially
   differs.
3. Against a €5.39 retail tariff, the KVS design consumes ~58 % of revenue and the S3
   design ~20 %. Only the second supports a business.
3a. **There is no single bitrate per camera.** Archive, live preview and live full are
   three different numbers with three different cost profiles. Only the archive figure is
   measured; the preview tier is observed to exist and to be smaller, but was not
   quantifiable from the available captures (§3.0).
3b. Bitrate is the input every figure hangs on, and the one most easily got wrong: a
   1.5 Mbps assumption versus 0.9 Mbps measured moved the break-even point by 74 %.
   Normalise to bits-per-pixel-per-frame before trusting any bitrate estimate (§3.1).
4. Segment length is a first-order cost variable: at 6 s, requests exceed storage 3×.
   The archive path needs 60 s segments, which forces live onto a separate path.
5. The build pays back at roughly **1,250 cameras over a year**; below a few hundred, the
   managed service is the correct engineering choice. (This bullet used to read 700
   cameras, computed under the superseded 1.5 Mbps bitrate assumption §3.1 corrects, and
   duplicated as a separate bullet alongside this one — a leftover from before that
   correction that this consistency pass caught and merged.)
6. For a prototype, KVS remains right. Knowing precisely why it would be wrong at scale
   is the point of this document.
7. `cam-02`'s bitrate is now measured, not assumed (§6.4: 60s stream-copy capture,
   2026-09-01) — 0.047 Mbps on the sub-stream it actually runs today (recording cost
   ≈$0.15/month, codec-irrelevant), 0.937 Mbps on its main/4MP stream (not currently
   used). The 0.9 Mbps figure this document used as a placeholder for `cam-02` before
   measurement turns out to almost exactly match the main stream — coincidence, not
   derivation; it was borrowed from the reference product's own camera.
8. Codec matters as much as architecture: switching `cam-02`'s main stream to H.265
   would cut both KVS ingest and viewing egress by ~40–50 % — the one lever that pays off
   harder on KVS than on S3 — but only if browser HEVC playback is solved first, which
   has its own unmodeled cost (§6.4).
9. This is not a revision of point 5's break-even — the system as built runs H.264, and
   ~1,250 cameras/year stands as the baseline number (for the reference product's own
   camera, not `cam-02`). It is the effect of a *hypothetical codec switch on `cam-02`'s
   main stream*: if passthrough channels moved from H.264 to H.265, that same asymmetry
   would cut the other way for build-vs-buy. Using `cam-02`'s own measured 0.937 Mbps
   baseline (not the earlier 0.9 Mbps placeholder), its break-even moves from ~1,200 to
   roughly **2,200–2,750 cameras run for a year** (§7.1) — a more efficient codec makes
   the managed service rational for *longer*, not shorter, which is easy to get backwards
   on intuition alone. Checked against viewing cost too, not just recording: folding in
   §4.3's egress Delta lands in the same ~2,100–2,620 range (§7.1), because recording
   cost — not viewing — is what drives this comparison either way.
