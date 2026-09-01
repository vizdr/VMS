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

Note the corrected row against our own: **they carry 2.25× our pixel count at 90 % of our
bitrate.** §3.1 explains why.

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
cost against $3.84 per camera per month, forever.

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
VI has no HEVC encode block (`NETWORK.md` §4). No bitrate has been measured for `cam-02`
yet, so the table below applies this document's own bitrate-driven cost model (the
`$ ≈ 3.25 × B(Mbps)` relationship implicit in §6.1's own figures — ingest at
$0.0085/GB plus 2-day storage at $0.023/GB-month) to a range of plausible H.264
baselines, with HEVC's typical **40–50 % bitrate reduction at equal quality** applied on
top. Every number here is provisional until `cam-02`'s actual H.264 bitrate is measured
(§10) — same "verify before quoting" discipline as the rest of this document.

| H.264 baseline | KVS $ (H.264) | H.265 @ 40 % cut | KVS $ (H.265) | H.265 @ 50 % cut | KVS $ (H.265) |
|---|---|---|---|---|---|
| 0.9 Mbps | $2.93 | 0.54 Mbps | $1.76 | 0.45 Mbps | $1.46 |
| 1.0 Mbps | $3.25 | 0.60 Mbps | $1.95 | 0.50 Mbps | $1.63 |
| 1.5 Mbps | $4.88 | 0.90 Mbps | $2.93 | 0.75 Mbps | $2.44 |
| 2.0 Mbps | $6.51 | 1.20 Mbps | $3.90 | 1.00 Mbps | $3.25 |

**Because KVS recording cost is linear in bitrate (ingest dominates), the dollar saving
equals the bitrate saving exactly — 40–50 % off, no discounting.** That is *not* true on
the S3 side: per §4.1, only the storage line (~60 % of S3's $0.74 recording total at
0.9 Mbps) scales with bitrate — PUT and index costs are per-segment, not per-byte. The
same H.265 switch would cut S3 recording cost by only ~(40–50 %) × 60 % ≈ 24–30 %. Put
differently: **H.265 pays off hardest on exactly the architecture (KVS) this document
otherwise argues against at scale** — it narrows, but does not close, the 4× ratio in
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

Before committing to either: confirm `gst-inspect-1.0` exposes a working HEVC decode
element on this Pi's build, and measure `cam-02`'s actual current bitrate — everything in
this subsection is a model, not yet a measurement.

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

Using the same 0.9 Mbps H.264 baseline and §6.4's two HEVC scenarios, applied only to
`cam-02`-type passthrough channels (§6.4's hardware caveat still applies — `cam-01` can't
do this):

| Scenario | KVS recording | S3 recording | Delta/camera-month | Delta/camera-year |
|---|---|---|---|---|
| H.264 baseline (0.9 Mbps) | $2.93 | $0.74 | $2.19 | $26.28 |
| H.265 @ 40 % cut (0.54 Mbps) | $1.76 | $0.56 | $1.20 | $14.40 |
| H.265 @ 50 % cut (0.45 Mbps) | $1.46 | $0.51 | $0.95 | $11.40 |

Re-running the same $33 k build-cost break-even against the smaller Delta:

```text
H.265 @ 40 %:  $33,000 ÷ $1.20 ≈ 27,500 camera-months  ≈ 2,290 camera-years
H.265 @ 50 %:  $33,000 ÷ $0.95 ≈ 34,700 camera-months  ≈ 2,890 camera-years
```

**H.265 pushes the break-even from ~1,250 to roughly 2,300–2,900 cameras run for a
year** — nearly double. A more efficient codec doesn't just save money in absolute terms;
it makes the *managed service* rational for longer, precisely because it attacks KVS's
weakest line item harder than it attacks S3's. A real fleet's actual break-even sits
between the H.264 and H.265 lines above, weighted by how many channels are
passthrough-capable (`cam-02`-like) versus transcode-locked (`cam-01`-like) — this isn't
a single number until the fleet's camera mix is known.

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

**Delta if `cam-02`-type channels run H.265 instead.** Applying §6.4's 40–50 % cut to the
rows above (only valid for passthrough channels — `cam-01` is hardware-locked to H.264):

| Scenario | KVS cost (H.264) | KVS cost (H.265, 40–50 % cut) |
|---|---|---|
| Weekend testing, 6 h streaming | ~$0.05 | ~$0.025–0.03 |
| One camera continuous, 1 month | ~$3.50 | ~$1.75–2.10 |
| Ten channels continuous, 1 month (all passthrough) | ~$35 | ~$17.50–21 |

At this scale the absolute saving is cents to a couple of dollars — same conclusion as
above, it's not the reason to do this yet. The saving only becomes a real number at fleet
scale, which is exactly what §7.1 works out.

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

Do not ship this document with estimates alone. Three measurements make it credible:

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
5. The build pays back at roughly **700 cameras over a year**; below a few hundred, the
   managed service is the correct engineering choice.
6. The build pays back at roughly **1,250 cameras over a year**; below a few hundred, the
   managed service is the correct engineering choice.
7. For a prototype, KVS remains right. Knowing precisely why it would be wrong at scale
   is the point of this document.
8. Codec matters as much as architecture: switching `cam-02` to native H.265 would cut
   both KVS ingest and viewing egress by ~40–50 % — the one lever that pays off harder on
   KVS than on S3 — but only if browser HEVC playback is solved first, which has its own
   unmodeled cost (§6.4).
9. This is not a revision of point 6's break-even — the system as built runs H.264, and
   ~1,250 cameras/year stands as the baseline number. It is the effect of a *hypothetical
   codec switch*: if passthrough channels moved from H.264 to H.265, that same asymmetry
   would cut the other way for build-vs-buy. H.265 erodes KVS's ingest penalty much faster
   than it erodes S3's already-ingest-free cost, so *if* the fleet ran H.265, the
   break-even would move from ~1,250 to roughly **2,300–2,900 cameras run for a year**
   (§7.1) — a more efficient codec makes the managed service rational for *longer*, not
   shorter, which is easy to get backwards on intuition alone.
