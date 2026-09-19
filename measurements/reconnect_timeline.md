# Reconnect behaviour and gap-fill — measured

The file the repo layout at `Demo-AWS-Video-revCosts4.md:2004` has always listed and which
never existed. §10.2 specified this test and it was never run; §16.3c asked for "the
gap-fill percentage before and after" and never defined it.

Measured 2026-09-19 on `cam-02` (ONVIF sub-stream, 640×360@15, audio on).
Tool: `adapter/bin/gap-fill.py`. Design: `OUTAGE.md`.

---

## 1. What "gap-fill percentage" means

Neither §16.3c nor §10.4 writes the equation down, so:

> **gap-fill % = (seconds of the outage window recoverable from the cloud) ÷ (window seconds)**

There are two sources and they must be **unioned, not added**. Before outage buffering the
only source was KVS fragments. With it there is a second — backfilled clips in S3 — and
they overlap, because kvssink replays its last `replayDuration` on reconnect while the
buffer holds the same span. Adding them reports >100% and looks like a triumph.

## 2. Method

Both runs identical apart from `outageBufferSec`:

- producer (`kvs-cam02.service`) running throughout, ingesting normally before T0
- AWS made unreachable with `awsblock.sh` — **both address families** (§4)
- outage held ~5 minutes, well past kvssink's 120 s buffer duration
- block removed, then ~90 s for kvssink to reconnect and replay before measuring
- window = [T0, T_recover], measured with
  `gap-fill.py --stream cam-02 --start … --end …`

## 3. Result

| | BEFORE (`outageBufferSec = 0`) | AFTER (`= 3600`) |
|---|---|---|
| Window | 304 s | 321 s |
| KVS fragments | 29 → 83.2 s (**27.38 %**) | 40 → 115.5 s (35.98 %) |
| Backfilled clips | 0 → 0.0 s | 1 → 289.8 s (90.29 %) |
| **Gap-fill (union)** | **27.38 %** | **99.78 %** |
| **Lost** | **220.8 s — 72.62 %** | **0.7 s — 0.22 %** |

> **A five-minute WAN outage lost 72.6 % of its footage before; it loses 0.2 % now.**

Raw output:

```
=== BEFORE (outage buffering OFF) ===
window            2026-09-19T14:44:45+02:00  ->  2026-09-19T14:49:49+02:00
                  304s
KVS fragments       29   covering    83.2s    27.38%
backfilled clips     0   covering     0.0s     0.00%
GAP-FILL (union)                  83.2s    27.38%
lost                             220.8s    72.62%

=== AFTER (outage buffering ON) ===
window            2026-09-19T14:57:04+02:00  ->  2026-09-19T15:02:25+02:00
                  321s
KVS fragments       40   covering   115.5s    35.98%
backfilled clips     1   covering   289.8s    90.29%
GAP-FILL (union)                 320.3s    99.78%
```

### Reading the numbers honestly

- **The residual 0.22 % is measurement edge, not lost footage.** The window end is the
  moment the firewall rules came out, which is a few seconds after connectivity actually
  returned; the last fractional segment falls outside the merged clip.
- **KVS alone improved too** (27.4 % → 36.0 %), which is *not* an effect of the feature.
  Fragment recovery depends on where kvssink's 120 s ring sat relative to T_recover, and
  the two runs differed. Only the union line is the claim.
- **The clip covers 90.3 %, not 100 %**, because a clip is built from *completed* 30 s
  segments: the segment still being written at recovery is excluded by design. KVS's own
  replay covers that tail, which is exactly why the union matters.
- Single paired run. Enough to establish the order of magnitude, not a distribution.

## 4. §10.2 is wrong, in three ways, and fails silently

The guide's snippet cannot simulate an outage on this network:

```bash
sudo iptables -A OUTPUT -p tcp --dport 443  -j DROP   # IPv4 only
```

1. **IPv4-only.** This LAN has working IPv6; AWS resolves to `2a05:d014:…`, so every
   "blocked" connection went over IPv6 and returned HTTP 200. The rules appear applied and
   nothing is blocked. **`ip6tables` rules are required too.**
2. **Blocking a resolved IP is useless.** The IoT endpoint rotated across
   `18.196.251.80`, `3.69.141.146`, `18.185.210.34`, `18.153.244.214` within minutes.
   Block ranges, not addresses.
3. **Blocking port 443 wholesale cuts your own tooling.** Anthropic's API is on
   `160.79.104.10` / `2607:6bc0::`, AWS on `3/8, 18/8, 35/8, 52/8, 54/8` and `2a05::/16` —
   disjoint, so range-blocking AWS leaves a developer session alive.

`iptables` here is `v1.8.11 (nf_tables)`, the nft-backed shim — present and working,
despite the guide's warning that it can be absent on Debian trixie.

**Always confirm the block landed** by curling an AWS endpoint and seeing it fail. A
"still works" result against an ineffective block is the trap, and it is silent.

Working version: `adapter/bin/awsblock.sh` (on|off).

## 5. Detection latency

Measured separately, since it bounds how much pre-roll is needed:

| | detection |
|---|---|
| First implementation | **86 s** |
| After the probe fix | **4 s** |

The first version used `socket.create_connection`, whose timeout applies **per resolved
address** — with both A and AAAA records a 4 s timeout cost 8 s per probe, and two probes
are needed. It worked, but on a third of the margin the 120 s pre-roll is supposed to
provide. Replaced with an explicit resolve-then-try loop under a total 4 s budget.

**The invariant to preserve: pre-roll > worst-case detection latency.** At 4 s against
120 s the margin is now 30×.

## 6. Reproducing

```bash
# 1. baseline
aws dynamodb update-item --table-name cameras --key '{"cameraId":{"S":"cam-02"}}' \
  --update-expression "SET outageBufferSec = :s" \
  --expression-attribute-values '{":s":{"N":"0"}}'
sudo systemctl start kvs-cam02.service && sleep 90

adapter/bin/awsblock.sh on   && T0=$(date -Iseconds)   # CHECK it says UNREACHABLE
sleep 300
adapter/bin/awsblock.sh off  && T1=$(date -Iseconds)
sleep 110                                              # let kvssink replay

adapter/bin/gap-fill.py --stream cam-02 --start "$T0" --end "$T1"

# 2. repeat with outageBufferSec = 3600, and wait for
#    `journalctl --user -u kvs-outage-uploader` to log "complete" before measuring.

sudo systemctl stop kvs-cam02.service                  # PutMedia costs money
```
