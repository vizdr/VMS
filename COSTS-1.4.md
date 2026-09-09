# Cost model v1.4: KVS MVP vs. S3 archive vs. Videoloft reference tariff

Companion to `Demo-AWS-Video-revCosts4.md`. Purpose: establish what the prototype
architecture would cost if operated as a product, compare it against a published
commercial price, and derive the architectural conclusion that follows.

**Status of the numbers.** AWS list prices (`us-east-1`; Frankfurt runs a few percent
above, so these are mildly optimistic). Enterprise discounts not modelled. Re-verify
before quoting.

## What changed in 1.4

v1.3 rested on one 60-second sample per stream. Repeating those captures under a second
lighting condition moved several of them by more than the entire span of v1.3's own
sensitivity table — so 1.4 is reorganised around **variance** rather than point
estimates.

| # | Change | Where |
|---|---|---|
| 1 | Illumination identified as the dominant cost variable (2.4–4.2×), absent from v1.3 | §3.1, §7.1 |
| 2 | `cam-01` measured for the first time — v1.3 carried a configured *target* as if measured | §1.2, §3.4 |
| 3 | `cam-02` main-stream figure re-based: 0.937 → **0.62–1.97 Mbps** depending on light | §1.2, §3.5 |
| 4 | Low-resolution streams shown to be cost-*stable*, not merely cheap | §3.2 |
| 5 | **Delta goes negative below ~0.104 Mbps** — S3 loses to KVS at sub-stream bitrates | §6.2 |
| 6 | Duty-cycle control added as a lever, and it dominates every other one | §7.2 |
| 7 | v1.3's "a static night scene compresses extremely well" corrected — true only with aggressive noise reduction | §4.3 |
| 8 | Break-even restated as a **range** (617–1,923 camera-years), not a number | §8 |
| 9 | **Frame rate labelled and normalised everywhere** — Videoloft runs 10 fps, all our streams 15 fps; v1.3 compared them directly | §3.6, §4.3, §5.1, §7.3, §8 |
| 10 | **Resolution tiers matched** — `cam-02` is 4 MP, so it is priced against Videoloft's €5.89 4 MP tier, not the €5.39 2 MP one | §4, §5.3 |
| 11 | Their **tier spacing** (+78 % pixels for +9.3 % price) added as independent evidence for §6.1, from the price list alone | §4.4 |
| 12 | **"MVP as built" mislabel corrected** — that row was fed Videoloft's 0.9 Mbps; our MVP actually runs 0.052 Mbps, ~17× lower. Our own measurements now drive the tables | §5.3 |
| 13 | **Viewing basis corrected** — held constant at 5 GB across streams differing 20× in bitrate, which implied 12 h of watching for one and 214 h for another. Now constant *watch time* (12.3 h/month) | §5.3 |

Unchanged and still load-bearing: the unit prices (§2), the Videoloft analysis (§4), the
recording-vs-viewing split (§5), and the central conclusion (§6). None of them depend on
our own cameras' bitrates.

**Added to 1.4 after first publication** (2026-09-09), from a night of measurement:

| # | Change | Where |
|---|---|---|
| 14 | Night bitrate **independently reproduced** 3 days later (1.703 / 1.765 vs 1.472 / 1.969); the 2.5× day/night ratio is no longer a single draw | §1.2, §3.1 |
| 15 | A lens obstruction was found making both streams read **3.3–3.7× cheaper than reality**; those samples are struck out, and "inspect the frame, not just the number" added as a fourth measurement rule | §1.2, §1.3 rule 4 |
| 16 | **Duty cycle measured, not assumed** — 0.28 % overnight, 2.0 % over 9.5 h, 12.8 % on a busy afternoon. The §7.2 "5 %" assumption is confirmed conservative, and duty cycle is now the largest *variance* source in the model (~45× diurnal, vs 2.5× for bitrate) | §7.2 |
| 17 | Event-gating shown trustworthy: **zero false positives** across 9.01 h of heartbeat-backed silence, and the dawn day/night switch produced **no** spurious triggers | §7.2 |

---

## 1. Method and measurement inventory

### 1.1 Unit conversion

Bitrate is quoted in **bits**/s; billing in **bytes**. Hence the factor 8.

```text
GB/month ≈ B(Mbps) × 324          (30-day month; 30.44 is true mean, ~1.5 % understated)
```

Two corrections the arithmetic omits: **protocol overhead** (container, TLS, TCP/IP add
3–8 % on the wire — treat every figure as a floor) and **decimal vs. binary GB** (~7 %,
immaterial here).

### 1.2 The measurement set

Every bitrate figure this document relies on, with the conditions under which it was
taken. Method throughout: 60-second `ffmpeg -c copy` capture, video payload only, bytes
× 8 ÷ actual duration — no re-encode, and no reliance on RTSP's unreliable live bitrate
metadata. Audio (G711, present on both `cam-02` profiles) is excluded, matching the
pipeline, which discards it.

| Stream | Config | Date | Scene / light | **Mbps** |
|---|---|---|---|---|
| `cam-01` Pi encode | 720p15, 1000 kbps cap | 09-06 09:08 | static, **daylight** | **0.241** |
| `cam-01` Pi encode | 720p15, 1000 kbps cap | 09-05 23:00 | static, **dark** | **1.005** |
| `cam-02` sub | 640×360@15, 500 kbps cap | 09-01 | unrecorded | 0.047 |
| `cam-02` sub | 640×360@15, 500 kbps cap | 09-06 09:08 | empty, **daylight** | **0.056** |
| `cam-02` sub | 640×360@15, 500 kbps cap | 09-05 22:57 | empty, **dark** | **0.049** |
| `cam-02` main | 2560×1440@15, 3000 kbps cap | 09-01 | unrecorded | 0.937 |
| `cam-02` main | " | 09-06 09:09 | empty, **daylight** | **0.615** |
| `cam-02` main | " | 09-06 09:11 | empty, **daylight** | **0.785** |
| `cam-02` main | " | 09-05 22:59 | empty/cat ambiguous, **dark** | **1.472** |
| `cam-02` main | " | 09-05 23:02 | **cat in frame**, dark | **1.969** |
| `cam-02` sub | " | 09-09 04:42 | night, **IR glare (lens obstruction)** | ~~0.015~~ |
| `cam-02` sub | " | 09-09 04:52 | night, camera-lit, obstruction cleared | **0.049** |
| `cam-02` sub | " | 09-09 04:59 | night, camera-lit, **room lights confirmed off** | **0.049** |
| `cam-02` main | " | 09-09 04:42 | night, **IR glare (lens obstruction)** | ~~0.456~~ |
| `cam-02` main | " | 09-09 04:52 | night, camera-lit, obstruction cleared | **1.703** |
| `cam-02` main | " | 09-09 04:59 | night, camera-lit, **room lights confirmed off** | **1.765** |
| `cam-02` main @10 fps | ONVIF-forced, benchmark only | 09-01 | unrecorded | 0.6475 |
| Videoloft archive | 1920×1080@10, their camera | Aug | their scene | 0.893–0.898 |

Bold rows are new in 1.4. The 09-01 rows are v1.3's originals, retained for comparison;
their lighting was not recorded, which is itself the finding that prompted this revision.

**The two struck-through 09-09 04:42 rows are invalid and must not be used.** A physical
lens obstruction (an IR back-reflection — cobweb or similar) was blooming the image into
large flat washed-out areas that compress for almost nothing. They measured a broken
camera, not a lighting condition. Once the obstruction was cleared, the same streams under
the same light measured **3.3× (sub) and 3.7× (main) higher**. See §1.3.

The 04:52 and 04:59 pair is the useful one: taken 7 minutes apart, before and after the
hallway lights were confirmed off, they agree to within **3.6 %** on the main stream and
are identical on the sub. That establishes both that the measurement is reproducible when
conditions are genuinely controlled, and that the camera's own illuminator — not the room
lighting — was setting the scene brightness in both.

### 1.3 What a single sample can and cannot establish

v1.3 labelled 0.937 Mbps "**measured**" and built its §4.1, §6.4, §7.1 and §8 on it (that
document's numbering, not this one's). It *was*
measured. It was also one 60-second draw from a distribution that spans 0.615–1.969 Mbps
on the same stream, same configuration, same camera — a **3.2× range**.

Three rules follow, and they apply to any VBR surveillance stream:

1. **A VBR bitrate is a distribution, not a value.** Quote a range and the conditions, or
   the number is not reusable.
2. **Record the lighting.** It is the largest term (§3.1) and the cheapest thing to write
   down.
3. **Sample in pairs.** One daylight and one dark capture bound the diurnal range at the
   cost of one extra minute.
4. **Look at the frame, not just the number** (added after the 09-09 glare episode). A
   lens obstruction made both streams read 3.3–3.7× *cheaper* than reality, and nothing
   in the numbers themselves looked wrong — a low bitrate is exactly what a cost model
   hopes to see. It was caught only because a frame was decoded and inspected. In a model
   driven by measured bitrate, **a cobweb is a data-integrity problem**, and the cheapest
   number in the table came from the worst image in it. Capture a frame alongside every
   bitrate sample.

**Honesty note on 1.4's own data.** Two lighting states is not a diurnal curve. The 24/7
figures below assume a 12 h/12 h split — an explicit modelling assumption, not a
measurement. Proper characterisation needs hourly sampling across ≥24 h; until then treat
every 24/7 row as bracketed by its own day and night rows, which *are* measured. A second
caveat on the night data: the "empty vs cat" attribution for the 1.472 sample is
uncertain (the frame confirming the cat was captured ~2 minutes after that run began), so
1.472 is best read as an upper bound on night-empty and a lower bound on night-with-cat.

---

## 2. Unit prices used

| Service | Dimension | Rate |
|---|---|---|
| KVS | data ingested | $0.0085 / GB |
| KVS | data stored (hot tier) | $0.023 / GB-month |
| KVS | data consumed via HLS | $0.0119 / GB |
| S3 | data ingress | **free** |
| S3 | Standard storage | $0.023 / GB-month |
| S3 | PUT / POST / LIST | $0.005 / 1,000 |
| S3 | GET | $0.0004 / 1,000 |
| Internet egress | S3 or KVS → internet | ~$0.09 / GB |
| CloudFront | egress to internet | ~$0.085 / GB |
| DynamoDB | on-demand write + storage | ~$0.07 / camera-month |

**Critical:** retrieving media from KVS to a destination outside AWS incurs standard data
transfer *on top of* the KVS consumption charge. Egress is ~7.5× the HLS consumption
rate, so viewing cost is dominated by transfer, not by the service.

### 2.1 Closed-form cost functions

v1.3 expressed these as tables of point values, which obscured that they are simple
linear functions. At **2-day retention**, per camera-month, for bitrate *B* in Mbps:

```text
KVS(B)    = 2.754·B  (ingest)  + 0.497·B  (storage)      = 3.251·B
S3(B)     = 0.497·B  (storage) + 0.216 (PUT) + 0.07 (idx) = 0.497·B + 0.286
Delta(B)  = KVS(B) − S3(B)                                = 2.754·B − 0.286
```

These reproduce v1.3's headline figures ($2.93 ingest-side and $2.19 Delta exactly; S3
comes to $0.73 against v1.3's printed $0.74, a rounding difference), and
they make the structure visible: **KVS is purely proportional to bitrate; S3 has a fixed
floor that bitrate cannot reduce.** Every conclusion in §6–§8 is a consequence of that
one asymmetry.

---

## 3. What actually drives bitrate

This section is new in 1.4 and is where the measurements land.

### 3.1 Illumination — the dominant term

Same camera, same framing, empty scene in both cases, only the light differs:

| Stream | Daylight | Dark | **Ratio** |
|---|---|---|---|
| `cam-01` 720p15 | 0.241 | 1.005 | **4.2×** |
| `cam-02` main 4MP | 0.615 / 0.785 | 1.472 / 1.969 | **~2.5×** |
| `cam-02` main 4MP (09-09 re-test) | 0.700 avg | **1.703 / 1.765** | **2.5×** |
| `cam-02` sub 640×360 | 0.056 | 0.049 | 0.9× (flat) |

**The 09-09 re-test is an independent confirmation**, taken 3 days later on a different
night with the lens obstruction cleared and the room lights verified off. It reproduces
the 2.5× ratio almost exactly, from two samples that agree with each other within 3.6 %.
The dark rows on this camera are therefore not one lucky draw.

It also disposes of a hypothesis raised mid-measurement and worth recording as closed:
that *true IR-illuminated night* might be the **cheapest** state (monochrome, no chroma to
encode, illuminator restoring exposure and dropping gain), which would have inverted this
whole section into a three-regime model. It was based on the glare-corrupted 04:42 samples.
With the obstruction cleared, night sits at 1.70–1.77 — squarely in the same expensive band
as the earlier lights-on night samples (1.47–1.97), not below daylight. **Two-regime
day/night stands; the three-regime rewrite was withdrawn before it was made.**

The mechanism is sensor noise. In low light the sensor's gain rises, and the resulting
grain is high-entropy, temporally uncorrelated detail: it defeats motion estimation, so
P-frames that would collapse to almost nothing in good light instead carry real residual
data. The encoder spends its bits describing noise.

`cam-01` is the cleanest demonstration available: it is pointed at a **static photograph
on a wall**. There is no motion in the scene at all. Its 4.2× swing is therefore *purely*
a noise effect, with the motion variable held at zero by construction.

**This is larger than any other lever in this document** — larger than the H.265 saving
(§7.4, 40–50 %), larger than the retention tier, and comparable to the entire span of
v1.3's bitrate sensitivity table, which swept 0.9→2.0 Mbps as hypothetical scenarios. A
single real camera traverses that whole range on its own, twice a day.

### 3.2 Resolution buys stability, not just savings

The sub-stream barely responds to lighting (+14 %) while the 4MP stream moves 2.5×.
Downscaling to 640×360 spatially averages sensor noise away *before* it reaches the
encoder — eight source pixels collapse into one, and uncorrelated noise partially cancels
in the process.

The practical consequence is stronger than "small streams are cheap", which was already
obvious: **small streams are predictable.** For fleet-scale budgeting, variance matters
as much as the mean — a tier whose cost is flat across the diurnal cycle can be committed
to, while one that triples every night cannot. This is an argument for the archive/preview
split (§4.2, §5.2) beyond the egress saving that already motivated it.

### 3.3 Motion — smaller than expected

A domestic cat walking into frame and settling moved the 4MP stream ~34 % (1.472 →
1.969, subject to §1.3's attribution caveat). v1.3's §1 guidance — "add 10–15 % for a
scene with intermittent activity" — is the right order of magnitude for incidental
motion, and survives.

What it lacked was any equivalent multiplier for light. Ranked by measured effect:
**illumination (140–320 %) > motion (~34 %) > codec (40–50 %, §7.4)**.

### 3.4 Rate caps are ceilings, not floors

v1.3 carried `cam-01` at "1.0 Mbps — our encoder target", never measured. The night
capture returned 1.005 Mbps, which looks like confirmation. It is not: the daylight
capture returned **0.241 Mbps**.

`video_bitrate=1000000` on `v4l2h264enc` sets a rate-control *ceiling*. The encoder runs
VBR beneath it and only approaches the cap when the content demands it — which, at night,
noise does. The 1.005 figure was a saturated encoder, not a constant-bitrate one.

> **A correction to an interim analysis.** Between the two capture runs, the night figure
> was read as evidence that `cam-01` was effectively CBR and therefore gave *predictable*
> cloud cost, with an architectural conclusion drawn from it: that the transcode path,
> otherwise the inferior option on CPU grounds, at least bought cost stability. The
> daylight measurement refutes that — `cam-01` has the **widest** relative swing of any
> stream measured here. The claim was inferred from a single sample landing exactly on a
> configured cap; it is withdrawn. Stability comes from *resolution* (§3.2), not from
> rate capping.

### 3.5 Revised operating figures

**All rows are at 15 fps** — our cameras' running configuration. This matters for any
comparison against the reference product, which runs 10 fps (§3.6).

| Stream | fps | Day | Night | 24/7 est. (12/12) | GB/month @ 24/7 |
|---|---|---|---|---|---|
| `cam-01` 720p transcode | 15 | 0.241 | 1.005 | **0.623** | 202 |
| `cam-02` sub 640×360 (current) | 15 | 0.056 | 0.049 | **0.052** | 17 |
| `cam-02` main 2560×1440 | 15 | 0.700 | 1.721 | **1.211** | 392 |

Against v1.3's figures, the two errors run in **opposite directions**:

| v1.3 figure | What it actually was | 24/7 estimate | Error |
|---|---|---|---|
| `cam-01` 1.0 Mbps "target" | night-saturated worst case | 0.623 | overstated ~60 % |
| `cam-02` main 0.937 "measured" | a moderate-light sample | 1.211 | understated ~29 % |

Neither was wrong as a reading; both were wrong as a *representative* figure, in
different directions, which is exactly what §1.3 warns about.

### 3.6 Frame rate — and why it is not a free comparison

**The reference product runs 10 fps; every one of our streams runs 15.** Any table that
puts 0.9 Mbps next to 1.211 Mbps without saying so is comparing a 2 MP 10 fps workload
against a 4 MP 15 fps one and attributing the whole difference to efficiency. v1.3 did
this in several places; 1.4 labels frame rate everywhere and normalises before comparing.

**The scaling factor is measured, not assumed.** v1.3 captured `cam-02`'s main stream at
both frame rates on the same day under the same conditions — the only reason a normalised
comparison is possible at all:

```text
15 fps: 0.937 Mbps        10 fps: 0.6475 Mbps
factor = 0.6475 ÷ 0.937  = 0.691      (naive linear 10/15 = 0.667)
```

So this camera loses **30.9 %** going 15 → 10 fps, against 33.3 % predicted by naive
proportionality — **near-linear, slightly sublinear.** The gap is small but the direction
is structural and worth stating: bitrate does not scale with frame rate one-for-one,
because I-frames recur on a *time* interval rather than a frame interval (so their cost
per second is unchanged), and because halving the frame rate doubles the temporal distance
each P-frame must span, making each one individually more expensive. Only the *surplus*
P-frames are saved.

`cam-02` normalised to the reference product's 10 fps:

| Condition | Measured @15 fps | **@10 fps equivalent** |
|---|---|---|
| Daylight | 0.700 | **0.484** |
| 09-01 sample (moderate light) | 0.937 | 0.647 *(measured directly)* |
| Dark | 1.721 | **1.189** |
| 24/7 estimate | 1.211 | **0.837** |

**At matched frame rate, `cam-02`'s 24/7 average (0.837 Mbps) sits just below Videoloft's
0.9 Mbps — while carrying 1.78× the pixel count.** That is the like-for-like comparison
v1.3's tables implied but never actually performed.

> **The factor is `cam-02`'s, not a constant.** It was measured on that camera's own SoC
> encoder. Applying it to `cam-01` is an assumption, and a poor one — see below.

**The rate-cap interaction, which breaks the factor entirely.** `cam-01` is capped at
1 Mbps (§3.4). At night it *saturates* that cap. For a saturated encoder, dropping the
frame rate frees no bandwidth at all — the rate controller simply spends the same budget
on fewer, better frames. Frame rate is a bandwidth lever only while the encoder is
running below its ceiling:

| `cam-01` condition | Bitrate | At cap? | Effect of 15 → 10 fps |
|---|---|---|---|
| Daylight | 0.241 | no (24 % of cap) | reduces bitrate, ~linearly |
| Dark | 1.005 | **yes** | reduces *nothing*; buys quality instead |

This is the practical form of the §3.4 correction: a cap converts a bandwidth saving into
a quality improvement without telling you it has done so.

---

## 4. The reference tariff (Videoloft)

Nothing in §3 touches this section — it is about *their* camera, measured from their own
playback API. It is reproduced from v1.3 with one correction (§4.3).

Price calculator, captured August 2026 — **two resolution tiers**, which turn out to be
informative in their own right (§4.4):

> **1 × 2 MP camera, 24/7 continuous, 2-day cloud retention — €5.39 / month**
> **1 × 4 MP camera, same terms — €5.89 / month**

`cam-02`'s main stream is 2560×1440 = 3.7 Mpx, so **€5.89 (≈$6.47) is the correct retail
line to measure our own camera against** — not the €5.39 figure v1.3 and earlier drafts of
this document used throughout.

### 4.1 Measured workload

Seven consecutive 90-second media responses measured 10.01–10.10 MB (one 13.83 MB outlier
during vehicle motion):

```text
10.05 MB × 8 ÷ 90 s = 0.893 Mbps
10.10 MB × 8 ÷ 90 s = 0.898 Mbps       →  ~0.9 Mbps

ingested   292 GB/month     stored  19.4 GB steady state (2 days)
```

Unknowns: whether €5.39 includes VAT, and the EUR/USD rate (~$1 ≈ €0.91).

### 4.2 Three profiles, not one

| Profile | Bitrate | Used for | Cost driver |
|---|---|---|---|
| Archive | ~0.9 Mbps (measured) | 24/7 recording | ingest + storage, 720 h/month |
| Live preview | below archive, unquantified | multi-camera grid | egress, only while watched |
| Live full | unmeasured | single-camera focus | egress, briefly |

> **Honesty note (carried from v1.3).** An earlier revision quoted the preview tier at
> ~0.05 Mbps and derived a "20× cheaper" figure from it, by dividing a session's total
> bytes across the full camera list when only a few tiles were streaming. Withdrawn. The
> direction is not in doubt — grid tiles are ~200 px, and observed part sizes span
> 0.06–0.56 Mbps.

§3.2 now supplies an independent reason to want this split: the preview tier is not just
cheaper but diurnally stable.

### 4.3 Why 0.9 Mbps and not 1.5

The original 1.5 Mbps assumption reused our own bits-per-pixel figure:

```text
bits per pixel per frame = bitrate ÷ (width × height × fps)

measured (theirs)   900,000 ÷ (1920 × 1080 × 10) = 0.0434 bpp
our MVP target    1,000,000 ÷ (1280 ×  720 × 15) = 0.0723 bpp
old assumption    1,500,000 ÷ (1920 × 1080 × 10) = 0.0723 bpp   ← the error
```

Theirs is 40 % lower, for reasons mostly *not* about encoder skill:

1. **The bitrate is a camera setting, not their achievement.** `x-vl-transcoded: false`
   proves passthrough — whatever the installer configured is what gets stored.
2. **Camera-side noise reduction.** Sensor noise at night is high-entropy and defeats
   motion estimation, so surveillance cameras apply aggressive temporal 3DNR specifically
   to stop bitrate exploding. This filtering happens before the encoder.
3. **Encoding tools.** A camera SoC encoder typically runs High profile with CABAC;
   Baseline/CAVLC costs 10–20 % more bits for equal quality.
4. **No transcode generation loss.** Our MJPG → decode → H.264 path spends bits
   reproducing JPEG artefacts.
5. **Hardware encoder efficiency.** `v4l2h264enc` has limited rate control and no
   B-frames; hardware encoders commonly need 20–40 % more bits than `x264` at equal
   quality.

> **Correction in 1.4.** v1.3 listed a sixth reason: *"a static night scene compresses
> extremely well — an empty car park at 10 fps is almost entirely skip macroblocks."*
> Our own measurements contradict this as a general claim: darkness **raised** bitrate
> 2.4–4.2× on both our cameras (§3.1). The two are reconcilable, and item 2 is the
> reconciliation — a static dark scene compresses well *only if* noise reduction erases
> the grain first. Where it doesn't, darkness is a large penalty, not a saving. The claim
> is therefore conditional on the camera, not a property of night scenes, and cannot be
> assumed for an arbitrary installed device.

**`cam-02` at matched frame rate.** Measured for this comparison only (ONVIF
`FrameRateLimit` forced to 10 for one 60-second capture, then reverted), and now extended
across the lighting range using §3.6's measured 0.691 factor. Because bpp already divides
by fps, these rows are directly comparable to theirs:

| Source | Mbps @10 fps | Resolution | **bpp** | vs. Videoloft |
|---|---|---|---|---|
| Videoloft archive | 0.900 | 1920×1080 | **0.0434** | — |
| `cam-02`, daylight (scaled) | 0.484 | 2560×1440 | **0.0131** | 3.3× better |
| `cam-02`, 09-01 (measured) | 0.6475 | 2560×1440 | **0.0176** | 2.5× better |
| `cam-02`, dark (scaled) | 1.189 | 2560×1440 | **0.0323** | 1.35× better |

**v1.3's flat "2.5× more efficient" turns out to be the moderate-light case.** The true
figure spans **1.35×–3.3×** depending on illumination alone. The direction is robust —
`cam-02` beats the reference camera's bits-per-pixel in every lighting condition measured,
while carrying 1.78× the pixels — but any single multiplier quoted without its lighting
condition is meaningless. Only the middle row is a direct measurement; the outer two apply
§3.6's factor to §3.1's day and night captures.

**`cam-01`'s bpp, and a circularity worth naming.** At 15 fps:

```text
cam-01 daylight   241,000 ÷ (1280 × 720 × 15) = 0.0174 bpp
cam-01 dark     1,005,000 ÷ (1280 × 720 × 15) = 0.0727 bpp
v1.3 "MVP target"                             = 0.0723 bpp
```

The night figure reproduces the design assumption to three decimal places — necessarily,
because the 1 Mbps cap *was* derived from that bpp figure (0.0723 × 1280 × 720 × 15 =
0.999 Mbps). **At saturation, `cam-01` measures the cap we chose, not what the scene
needs.** Its daylight bpp (0.0174) is what the scene actually costs, and it is 4.2× lower.

One caution against over-reading that: `cam-01` points at a **static photograph**, an
unusually easy source with literally zero motion. Its low daylight bpp is not evidence
that the transcode path is efficient — items 4 and 5 above still apply. It is a
lower bound produced by an easy scene.

### 4.4 What the resolution tiers reveal

The two tiers are only €0.50 apart:

| Tier | Price | Pixels | vs. 2 MP |
|---|---|---|---|
| 2 MP (1920×1080) | €5.39 (≈$5.92) | 2.07 Mpx | — |
| 4 MP (2560×1440) | €5.89 (≈$6.47) | 3.69 Mpx | **+78 % pixels for +9.3 % price** |

**This is a strong hint about their cost structure, and it is derived purely from the
published price list** — no headers, no packet captures, no measurements of ours.

Ask what the $0.55 uplift has to *cover*. If a 4 MP camera runs at a higher bitrate than a
2 MP one, the uplift must absorb that extra bitrate for the tier to be as profitable as
the tier below it. Using §2.1's marginal costs:

```text
uplift $0.55 buys, in additional sustained bitrate:
  on S3 economics   ($0.497 per Mbps-month, storage only)  →  +1.106 Mbps
  on KVS economics  ($3.251 per Mbps-month, ingest-dominated) →  +0.169 Mbps
```

**S3 economics absorb 6.5× more bitrate per tier-dollar than KVS economics.** Concretely,
the 4 MP tier stays margin-neutral against the 2 MP tier up to:

| | Tolerable 4 MP bitrate | as bpp @10 fps |
|---|---|---|
| On S3 | **2.01 Mbps** | 0.0544 |
| On KVS | **1.07 Mbps** | 0.0290 |

Now put our own measured 4 MP camera against those thresholds, normalised to their 10 fps
(§3.6):

| `cam-02` main @10 fps equiv | Mbps | vs. KVS threshold (1.07) | vs. S3 threshold (2.01) |
|---|---|---|---|
| Daylight | 0.484 | under | under |
| 09-01 sample | 0.647 | under | under |
| 24/7 estimate | 0.837 | under | under |
| **Dark** | **1.189** | **over** | under (59 % of headroom) |

**A real 4 MP camera in ordinary night conditions already exceeds what the 4 MP tier's
upcharge would cover on KVS economics, while sitting comfortably inside it on S3
economics.** A provider on KVS would find its 4 MP tier eroding margin against its own
2 MP tier every night, on an entirely unremarkable camera. A provider on S3 would not
notice.

Two further observations:

- **Resolution-based pricing is a proxy that does not track the cost driver.** Their cost
  moves with *bitrate*; their price moves with *pixels*. §4.3 item 1 already established
  that bitrate is an installer's camera setting, not the provider's choice — so the two
  decouple completely. Our own 4 MP camera at 0.837 Mbps (24/7, matched fps) would cost
  them **less** than the 2 MP reference camera at 0.9 Mbps, while paying €0.50 more. A
  conservatively-configured 4 MP customer subsidises an aggressively-configured one.
- **The tier spacing is only rational with a fixed-cost-dominated architecture.** Charging
  9 % more for 78 % more pixels is a comfortable trade when your marginal cost is storage
  (which is what S3 makes it) and an uncomfortable one when it is per-GB ingest (which is
  what KVS makes it). The pricing structure is itself evidence for the §6.1 conclusion.

---

## 5. Recording vs. viewing cost

**Recording cost is architecture-dependent; viewing cost is very nearly
architecture-independent**, because internet egress dominates it and egress is priced the
same whatever produced the bytes.

### 5.1 Recording — per camera-month, 2-day retention

Using §2.1's functions across the measured range:

| Stream / condition | fps | B (Mbps) | KVS | S3 | Delta |
|---|---|---|---|---|---|
| Videoloft archive (their camera, 2 MP) | **10** | 0.900 | $2.93 | $0.74 | $2.19 |
| `cam-01`, daylight | 15 | 0.241 | $0.78 | $0.41 | $0.38 |
| `cam-01`, dark | 15 | 1.005 | $3.27 | $0.79 | $2.48 |
| `cam-01`, 24/7 est. | 15 | 0.623 | $2.03 | $0.60 | $1.43 |
| `cam-02` sub, 24/7 est. | 15 | 0.052 | $0.17 | $0.31 | **−$0.14** |
| `cam-02` main, daylight | 15 | 0.700 | $2.28 | $0.63 | $1.64 |
| `cam-02` main, dark | 15 | 1.721 | $5.59 | $1.14 | $4.45 |
| `cam-02` main, 24/7 est. | 15 | 1.211 | $3.94 | $0.89 | $3.05 |
| *`cam-02` main, 24/7, normalised to 10 fps* | *10* | *0.837* | *$2.72* | *$0.70* | *$2.02* |

**The last row is the only one comparable to the first.** Run at the reference product's
own frame rate, `cam-02`'s 4 MP main stream would cost **$2.72/camera-month on KVS against
their 2 MP workload's $2.93** — cheaper, despite 1.78× the pixels. Read against the 15 fps
row above it ($3.94), the naive comparison overstates our cost by 45 % purely through the
frame-rate mismatch.

**The `cam-02` main row moves from v1.3's $3.05 to a $2.28–5.59 band** — the single
largest numerical change in this revision, and it is entirely a lighting artefact.

Note also that the Delta for a single camera **swings 2.7× between day and night**
($1.64 → $4.45). Every fleet-scale figure in §8 inherits that spread.

### 5.2 Viewing — per GB actually watched

| | KVS HLS | S3 + CloudFront |
|---|---|---|
| Service charge | $0.0119 | ~$0.0000 (GET) |
| Internet egress | $0.09 | $0.085 |
| **Per GB watched** | **$0.102** | **$0.085** |

Both dominated by transfer. CloudFront also caches, so a second operator watching the
same footage is nearly free; S3-direct and KVS both charge again.

**The break-even that makes this concrete.** A measured 53-minute session on the
reference product transferred 373 MB — ~$0.034 of egress at $0.09/GB. Against $0.74/month
to record that camera on S3:

```text
$0.74 ÷ ($0.034 / 0.89 h) ≈ 19 hours
```

**Roughly 20 hours of viewing per month costs as much as recording that camera 24/7 for
the whole month.** A security desk with a video wall inverts the economics entirely —
continuous viewing of one camera would cost ~36× its recording cost.

Three consequences, all unchanged from v1.3 and all reinforced by §3.2:

- **Sub-stream for live monitoring, main stream only for evidence.** A cost decision an
  order of magnitude more significant than any storage optimisation.
- **CDN caching is not a nicety.** With several operators on the same footage, cache hits
  are the difference between paying once and paying N times. The reference product forgoes
  this (no `via:`/`age:`/`x-cache:`) because arbitrary-range serving is near-uncacheable.
- **Playback pulls at roughly real time** (0.94 Mbps average fetch across 53 minutes), so
  viewing cost accrues linearly with watch time and forecasts cleanly per seat.

### 5.3 Total against the reference tariff

> **Label correction.** v1.3, and 1.4's own first draft, carried a row labelled **"MVP as
> built (KVS)"** against $2.93 — a figure derived from *Videoloft's* 0.9 Mbps camera. Our
> MVP has never produced that bitrate. What it actually runs is `cam-02`'s **sub-stream at
> 0.052 Mbps**, roughly 17× lower. The label attributed someone else's workload to our
> system. Now that our own cameras are measured in our own pipeline (§1.2), the tables
> below are built from those measurements, and the reference product's workload is priced
> separately and labelled as theirs.

**Viewing basis, also corrected.** The old tables held viewing at a flat 5 GB/month across
streams whose bitrates differ by 20×. That silently assumes wildly different watch times —
5 GB is ~12 hours of their 0.9 Mbps stream but **214 hours** of our sub-stream. The tables
below instead hold *watch time* constant at **12.3 h/month** (the same light-viewing
assumption, expressed the way it was originally derived) and let viewing bytes scale with
each stream's own bitrate. At 0.9 Mbps this reproduces the original 5.0 GB exactly.

**(a) What our MVP actually costs, as built.** `cam-02` as configured today — the
sub-stream, 24/7:

| | Recording | Viewing (12.3 h) | Total | vs. €5.89 (≈$6.47) |
|---|---|---|---|---|
| KVS (as deployed) | $0.17 | $0.03 | **$0.20** | **3 % of revenue** |
| S3 archive | $0.31 | $0.02 | $0.34 | 5 % of revenue |

Two things stand out. **KVS is the cheaper architecture here** — the sub-stream sits below
§6.2's crossover, so the managed service wins. And the absolute figure is so far under the
tariff that the revenue-share column is close to meaningless: we are archiving 640×360
from a 4 MP camera, which is not the product the €5.89 tier prices. Read it as "what we
actually spend", not as a competitive comparison.

**(b) What our 4 MP camera would cost at main-stream quality** — the comparison that is
genuinely like-for-like against the 4 MP tier, using our own measured bitrates (§3.5)
normalised for frame rate (§3.6):

| | fps | Recording | Viewing | Total | vs. €5.89 (≈$6.47) |
|---|---|---|---|---|---|
| KVS, as we would run it | 15 | $3.94 | $0.69 | **$4.62** | **71 % of revenue** |
| KVS, normalised to their 10 fps | 10 | $2.72 | $0.47 | **$3.20** | **49 % of revenue** |
| S3, as we would run it | 15 | $0.89 | $0.57 | $1.46 | 23 % of revenue |
| S3, normalised to their 10 fps | 10 | $0.70 | $0.40 | **$1.10** | **17 % of revenue** |

**(c) Their 2 MP workload, priced on each architecture** — no number in this table comes
from our system; it is their measured camera (§4.1) against their 2 MP tier, and it exists
only as the input to §6.1's argument about *them*:

| | Recording | Viewing | Total | vs. €5.39 (≈$5.92) |
|---|---|---|---|---|
| KVS | $2.93 | $0.51 | **$3.44 ≈ €3.13** | **58 % of revenue** |
| S3 | $0.73 | $0.43 | **$1.16 ≈ €1.06** | 20 % of revenue |

A SaaS business needs infrastructure COGS below roughly 20–25 %. **Only the S3 rows reach
it, in every comparison and at either frame rate.** The KVS rows land at 49–71 % — before
support, engineering, the viewing application, payment processing or sales.

Two readings worth separating:

- **Frame rate moves this more than resolution does.** Our 4 MP camera at their frame rate
  (49 %) is *better* than their own 2 MP workload on KVS (58 %), despite 1.78× the pixels.
  Run at our 15 fps it is worse (71 %). The resolution difference is doing less work here
  than the frame-rate difference.
- **An independent corroboration of the 0.9 Mbps figure.** Our 4 MP camera normalised to
  10 fps runs 0.837 Mbps against their 2 MP camera's 0.900 — two unrelated 24/7
  surveillance cameras, different vendors, different scenes, landing within 7 % of each
  other at matched frame rate. That is weak evidence, but it is evidence, and it points
  the same way as §4.3: archive bitrate is set by installer convention far more than by
  sensor resolution.

---

## 6. The conclusion this forces

### 6.1 Videoloft cannot be using KVS for bulk archive

At their published price and measured bitrate, KVS raw infrastructure consumes ~58 % of
the retail line before a single non-infrastructure cost is counted.

Independent confirmation: their playback API serves **arbitrary time ranges**
(`x-vl-seek: 1.144` on an unaligned request), remuxes containers on the fly
(`x-vl-transcoded: false`, `/stream/mpegts/` path segment), and returns none of the
headers KVS emits. That is a custom packager over object storage.

**What they must therefore have built:** the time index, the HLS packager, and clip
export — precisely the three things KVS sells.

**A third line of evidence, new in 1.4: their own tier spacing.** §4.4 shows the 4 MP
tier's €0.50 uplift covers +1.106 Mbps on S3 economics but only +0.169 Mbps on KVS. Our
own 4 MP camera at night, normalised to their frame rate, already exceeds the KVS
threshold. **A provider on KVS would be selling a 4 MP tier that erodes margin against its
own 2 MP tier on any ordinary camera after dark.** The price list is therefore consistent
with storage-dominated marginal cost and inconsistent with ingest-dominated marginal cost
— an argument that needs no packet capture at all.

**This conclusion is untouched by everything in §3.** It rests on their measured bitrate
and their retail price, neither of which our cameras inform. It also survives the H.265
stress test (§7.4): even a best-case HEVC cut on an already well-tuned stream leaves a
KVS design at 29–35 % of revenue, above the ceiling.

### 6.2 …but the conclusion has a lower bound, which v1.3 never stated

`Delta(B) = 2.754·B − 0.286` is **negative below B ≈ 0.104 Mbps.**

Below roughly 0.1 Mbps, the S3 architecture costs *more* than KVS, because S3's fixed
PUT + index floor ($0.286/camera-month at 60-second segments) exceeds the ingest saving
that free ingress buys. v1.3 printed both numbers for the sub-stream — "KVS recording
≈$0.15, S3 ≈$0.31" — and drew no conclusion from the fact that one is **twice** the
other.

Our own `cam-02` sits at 0.052 Mbps today, i.e. **squarely in the region where the
managed service is the cheaper architecture** — by $0.14/camera-month, or about 2×.

This does not overturn §6.1, which concerns 0.9 Mbps archive workloads an order of
magnitude above the crossover. It bounds it:

> **Build-your-own wins on bulk archive. The managed service wins on low-bitrate
> preview/thumbnail tiers.** A fleet running both should not assume one architecture is
> correct for both, and the crossover is computable: **B\* ≈ 0.104 Mbps** at 60-second
> segments, moving with segment length (§7.5) since that sets the fixed floor.

---

## 7. Sensitivity — ordered by measured magnitude

v1.3 led with bitrate as "dominant uncertainty". Bitrate is the *mechanism*; 1.4 orders
by what actually moves it.

### 7.1 Illumination — 2.4×–4.2× (largest measured)

| Condition | `cam-02` main | KVS recording | Delta |
|---|---|---|---|
| Daylight | 0.700 | $2.28 | $1.64 |
| Dark | 1.721 | $5.59 | $4.45 |

Unmodelled in v1.3 entirely. Mitigations, in order of effectiveness: better scene
lighting (cheapest — it is a bitrate intervention disguised as an electrical one),
camera-side 3DNR tuning (§4.3 item 2 — the mechanism by which some cameras avoid this
penalty), lower resolution at night, or duty-cycle control (§7.2), which sidesteps it.

### 7.2 Duty cycle — up to ~20× (largest available)

Not a sensitivity so much as an architectural choice, and it dominates every other entry
here. All figures above assume 24/7 continuous recording, 720 h/month. Recording only
when something happens scales the *entire* recording bill linearly with duty cycle.

`cam-02` performs human-shape and motion detection **onboard** and publishes both as
ONVIF events (`tns1:UserAlarm/IVA/HumanShapeDetect`,
`tns1:RuleEngine/CellMotionDetector/Motion`), verified firing live. There is no inference
cost to pay — no Rekognition, no local model, no CPU on the Pi.

At a 5 % duty cycle (a plausible figure for an indoor hallway), `cam-02` main-stream
recording falls from $3.94 to ~$0.20/camera-month. That is a larger saving than the codec
change (§7.4), the retention tier (§7.6) and the entire day/night span (§7.1) combined,
and it is the one lever that makes the others' variance irrelevant.

**The 5 % is no longer an assumption — it has been measured, and it is conservative.** A
9.5-hour continuous ONVIF event capture (`adapter/observe_events.py`, log in
`measurements/`, method and full results in `Camera-Features.md` §9) recorded every
detection this camera emitted overnight, with a heartbeat every 5 minutes proving the
observation was continuous rather than merely quiet. Duty cycle, using `clip_to_s3`'s
45-second clip window and a 60-second cooldown:

| Window | Clips/h | **Duty cycle** |
|---|---|---|
| Overnight, 22:00–07:00 | 0.22 | **0.28 %** |
| Whole 9.5 h run (incl. evening activity) | 1.6 | **2.0 %** |
| Busy afternoon, separate 4.5 h sample | 10.2 | **12.8 %** |

Two things follow. First, **the diurnal spread in duty cycle is ~45×** — far wider than
the 2.5× spread in bitrate (§3.1), which makes duty cycle not merely the largest lever but
the largest *source of variance* in the whole model. Second, a 24/7 average is the wrong
basis for it: the hours that cost money are the busy ones, and they are precisely the
hours a viewer cares about.

Supporting observations from the same capture, both of which bear on whether event-gating
is trustworthy enough to bill against:

- **Zero false positives in 9.01 of 9.5 hours of heartbeat-backed observed silence.** The
  longest unbroken quiet stretch was 5.41 hours. Only three genuine incidents occurred (a
  fourth was the camera being physically serviced and is excluded).
- **The dawn day/night switch produced no spurious events.** Illumination-mode changes
  invert the entire image and are a classic false-trigger source; here the transition at
  ~06:50 fell inside a 2.22-hour silence. That risk is measured and absent, not assumed
  away.

**It also changes what the storage architecture must do:** event-gated recording produces
short discontinuous clips, not a continuous timeline — which suits object storage and
undermines KVS's continuous-timeline model further still.

### 7.3 Bitrate and resolution

| Bitrate | fps | GB/mo | KVS | S3 | Ratio |
|---|---|---|---|---|---|
| 0.052 (`cam-02` sub) | 15 | 17 | $0.17 | $0.31 | **0.5×** ← S3 loses |
| 0.104 (crossover) | — | 34 | $0.34 | $0.34 | 1.0× |
| 0.837 (`cam-02` main, 10 fps equiv) | 10 | 271 | $2.72 | $0.70 | 3.9× |
| 0.9 (Videoloft) | 10 | 292 | $2.93 | $0.74 | 4.0× |
| 1.211 (`cam-02` main 24/7) | 15 | 392 | $3.94 | $0.89 | 4.4× |
| 2.0 | — | 648 | $6.50 | $1.28 | 5.1× |

The architectures diverge as quality rises, and **converge — then cross — as it falls.**

### 7.4 Codec — H.265 on `cam-02`

Viable only on `cam-02`; `cam-01` is hardware-locked to H.264 (the Pi's VideoCore VI has
no HEVC encode block, `NETWORK.md` §4).

Re-based onto the 24/7 estimate (1.211 Mbps) rather than v1.3's 0.937:

| | H.264 | H.265 @ 40 % | H.265 @ 50 % |
|---|---|---|---|
| Bitrate | 1.211 | 0.727 | 0.606 |
| KVS recording | $3.94 | $2.36 | $1.97 |
| S3 recording | $0.89 | $0.65 | $0.59 |
| Delta | $3.05 | $1.72 | $1.38 |

Because KVS recording is linear in bitrate, **the dollar saving equals the bitrate saving
exactly.** Not so on S3, where only the storage line moves — the same cut yields ~27–34 %
there. H.265 pays off hardest on the architecture this document otherwise argues against.

**The complication:** browser HEVC playback isn't guaranteed. `hls.js` in
Chrome/Firefox/most Android can't reliably decode it; Safari/iOS can. Closing that needs
either on-demand transcode at playback (real per-minute compute, unquantified) or dual
continuous streams (paying KVS ingest twice — the codec saving and the dual-stream cost
must be netted, not assumed additive).

### 7.5 Segment length

At $0.005/1,000 PUTs, continuous recording generates a surprising request bill — and this
sets the fixed floor that produces §6.2's crossover:

| Segment | PUTs/camera/month | PUT cost | Crossover B\* |
|---|---|---|---|
| 6 s (HLS convention) | 432,000 | $2.16 | 0.810 Mbps |
| 10 s | 259,200 | $1.30 | 0.497 Mbps |
| 30 s | 86,400 | $0.43 | 0.182 Mbps |
| 60 s | 43,200 | $0.22 | **0.104 Mbps** |
| 300 s | 8,640 | $0.04 | 0.040 Mbps |

At the HLS-conventional 6 seconds, request charges are 3× storage charges **and the
crossover rises to 0.81 Mbps** — i.e. S3 would lose to KVS for almost every stream in
this document. Segment length is not a tuning detail; it decides which architecture wins.

> **Derived, not observed.** The reference product's read path is decoupled from its
> storage cadence (`x-vl-seek: 1.144` shows arbitrary cutting), so no segment length is
> visible from outside. 60 s follows from the arithmetic; it is not evidence of what they
> store.

### 7.6 Retention

Storage is the smallest line item at 2-day retention (17 % of the KVS bill, 60 % of S3's).
Longer tiers move S3's column proportionally more, since storage is most of what it
charges for — but KVS's warm tier (§9) changes that comparison and is not modelled.

---

## 8. Fleet scaling and break-even

Assume 3 engineer-months at a loaded €10 k/month ≈ **$33 k** to build segmenting, upload,
index, packager and clip export.

v1.3 gave a single figure (~1,250 camera-years). Since Delta swings 2.7× with light
alone, 1.4 gives the range:

| Basis | fps | Delta/camera-month | Camera-months | **Camera-years** |
|---|---|---|---|---|
| `cam-02` main, dark | 15 | $4.45 | 7,400 | **617** |
| `cam-02` main, 24/7 est. | 15 | $3.05 | 10,800 | **903** |
| Videoloft 0.9 Mbps (v1.3's basis) | 10 | $2.19 | 15,100 | 1,256 |
| `cam-02` main, 24/7, **10 fps equiv** | 10 | $2.02 | 16,300 | **1,362** |
| `cam-02` main, daylight | 15 | $1.64 | 20,100 | **1,675** |
| `cam-01` 24/7 est. | 15 | $1.43 | 23,100 | **1,923** |
| `cam-02` main, H.265 @ 50 %, 24/7 | 15 | $1.38 | 23,900 | 1,993 |
| `cam-02` sub (current config) | 15 | −$0.14 | never | **never** |

**Frame rate belongs on this list of levers, but ranks below the codec.** Dropping
15 → 10 fps moves the break-even from ~900 to ~1,360 camera-years (1.5×), against H.265's
~900 → ~1,990 (2.2×). Frame rate is nonetheless the cheaper intervention — one ONVIF call,
no codec migration and no browser-compatibility problem (§7.4) — so it is the first thing
to try, not the most powerful.

**Reading:** below a few hundred cameras, KVS is the rational choice under any
assumption. Above ~2,000, building is correct under any assumption. **Between ~600 and
~2,000 the answer depends on camera mix, lighting, frame rate and codec** — and no single
number is honest in that band.

Note the last two rows carry the same message from different directions: a low-bitrate
channel never repays a custom pipeline, and H.265 delays repayment by making the managed
service rational for longer. That remains v1.3's counter-intuitive finding, and it
survives re-basing: **a cheaper codec makes building your own pay off later, not sooner**,
because it attacks KVS's per-GB ingest far harder than S3's fixed floor.

---

## 9. What the MVP actually costs to run

The prototype is not operated 24/7, so demo cost is trivial either way. At 24 h retention:

| Scenario | `cam-01` (0.623 est.) | `cam-02` sub (0.052) | `cam-02` main (1.211 est.) |
|---|---|---|---|
| Weekend testing, 6 h | ~$0.016 | ~$0.001 | ~$0.030 |
| One camera, 1 month | ~$1.87 | ~$0.16 | ~$3.64 |
| Ten channels, 1 month | ~$18.70 | ~$1.56 | ~$36.36 |

`cam-02` as currently configured costs less than the rounding error on `cam-01`. **Cost
is not the reason to implement the S3 path in the prototype** — at one camera the
difference is cents, and per §6.2 the sub-stream is actually cheaper on KVS. The reason is
to demonstrate the trade-off is understood, and to have built the capability from
primitives as well as from a managed service.

Keep the $10 monthly budget alarm (`Demo-AWS-Video-revCosts4.md` §1.1). The realistic
failure mode is a `gst-launch` left running for a week, not a design error.

---

## 10. Excluded from this model

- **KVS warm tier** — priced per 1,000 fragments persisted rather than per GB, 30-day
  minimum. Worth modelling for long retention; doesn't help the 2-day case.
- **KMS** — KVS rotates its data key ~every 45 minutes; dollars per account, not camera.
- **Lambda, API Gateway, Cognito, IoT Core** — cents per camera at this scale.
- **CloudFront request charges and minimum commitments.**
- **Audio.** Both `cam-02` profiles carry G711 (~64 kbps, ~20.7 GB/camera-month). The
  pipeline discards it. Enabling it would add ~$0.21/camera-month on KVS — negligible
  against video, but not zero, and it would **more than double** the sub-stream's total.
- **The packaging tier.** `x-vl-transcoded: false` shows container remuxing, not
  re-encoding — real, but far cheaper than transcode compute.
- **CDN absence** on the reference product's side; our pre-generated playlists over static
  objects *are* cacheable, a genuine advantage not modelled.
- **Support plans, replication, backup; adapter hardware; enterprise discounts** (which
  reduce both columns roughly proportionally, so the ratio survives).

---

## 11. How to verify against reality

v1.3 listed three checks and noted one was done. Status now:

- ✅ **Bitrate on the wire** — done for both cameras, both lighting states (§1.2).
- ⬜ **Diurnal curve** — the open item this revision creates. Two states is not a curve;
  sample hourly across ≥24 h before treating any 24/7 figure as more than a bracket.
- ⬜ **AWS-side ingest cross-check** — confirm the arithmetic against what AWS counted:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/KinesisVideo \
  --metric-name PutMedia.IncomingBytes --dimensions Name=StreamName,Value=cam-02 \
  --start-time 2026-09-06T00:00:00Z --end-time 2026-09-07T00:00:00Z \
  --period 3600 --statistics Sum
```

  Hourly granularity here would deliver the diurnal curve above **and** the AWS
  cross-check from one run — the cheapest way to close both.

- ⬜ **Billed cost by service** — `aws ce get-cost-and-usage`, tagged `project=vms-demo`.

Publishing measured-versus-modelled is worth more than either figure alone.

---

## 12. Summary

1. Cloud video cost is driven by **ingest** and **egress**, not storage.
2. KVS charges per GB ingested; **S3 ingress is free.** At archive bitrates that single
   line is the whole argument — a ~4× recording-cost ratio.
3. **But S3 carries a fixed floor** (PUT + index) that bitrate cannot reduce, so below
   **B\* ≈ 0.104 Mbps** the managed service is cheaper. Build-your-own wins on archive;
   KVS wins on preview tiers. (§6.2)
4. **Illumination is the largest uncontrolled variable measured** — 2.4–4.2× on the same
   camera, same scene, twelve hours apart. It exceeds codec choice and was entirely
   absent from v1.3. (§3.1)
5. **Resolution buys predictability as well as savings:** the 640×360 tier is diurnally
   flat where the 4MP tier triples. (§3.2)
6. **Duty-cycle control dominates everything else.** `cam-02` detects humans onboard and
   reports it over ONVIF for free; gating on that beats every codec, retention and
   resolution decision in this document combined. (§7.2)
7. Videoloft cannot be running KVS for bulk archive at either tier (€5.39 / €5.89), and
   §4.4 adds a third independent reason from the price list alone — the conclusion is
   unaffected by any of the above, and survives an H.265 stress test. (§6.1)
8. The build-vs-buy break-even is **a range, 617–1,923 camera-years**, not a number; the
   band from ~600 to ~2,000 cameras cannot be resolved without knowing camera mix,
   lighting and codec. (§8)
9. **Match the resolution tier as well as the frame rate, and price your own measured
   camera rather than someone else's.** `cam-02` is 4 MP, so its comparator is €5.89. At
   matched frame rate KVS lands at **49 %** of revenue and S3 at **17 %**. Our MVP *as
   actually deployed* (sub-stream) costs **$0.20/camera-month** — and KVS is the cheaper
   architecture at that bitrate, per point 3. (§5.3)
10. **Their tier spacing is itself evidence.** +78 % pixels for +9.3 % price only works if
   marginal cost is storage: the €0.50 uplift covers +1.1 Mbps on S3 economics but just
   +0.17 Mbps on KVS — and our own 4 MP camera exceeds the KVS threshold every night.
   Derived from the published price list alone. (§4.4)
11. **Frame rate must be stated before any cross-product comparison.** Videoloft runs
   10 fps; all our streams run 15. Normalised to 10 fps, `cam-02`'s 4 MP main stream costs
   **$2.72/camera-month against their 2 MP workload's $2.93** — cheaper, with 1.78× the
   pixels. The naive 15-vs-10 comparison overstates our cost by 45 %. Measured scaling
   factor 0.691 (near-linear, slightly sublinear), and it collapses to zero saving
   whenever a rate cap is already saturated. (§3.6)
12. A VBR bitrate is a distribution. **Quote the range, the lighting and the frame rate, or
   the number is not reusable** — the lesson v1.3 paid for twice, in opposite
   directions. (§1.3)
