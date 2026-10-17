# PTS and DTS — what they are, and where they bite in this project

Two timestamps travel with every encoded frame. Getting either one wrong is not loud: in
this project it has twice produced a pipeline that looked healthy while KVS discarded
frames (`FoundAndFixed.md` #15, #57). This note explains the concepts and points to those
records. It is reference material, not a runbook.

## The short version

- **PTS (Presentation Time Stamp)**: when a frame should be *shown*.
- **DTS (Decode Time Stamp)**: when the frame must be *handed to the decoder*. In practice
  this means the order the decoder needs frames in.

When the two orders are the same, PTS = DTS. They only differ when the encoder uses
**B-frames**.

## Why the orders can differ

A B-frame is predicted from a frame before it *and* a frame after it. The decoder can't
rebuild it until it already has the later frame, so the encoder sends frames out of display
order:

| Stream (decode) order | I₀ | P₃ | B₁ | B₂ | P₆ | B₄ | B₅ |
|---|---|---|---|---|---|---|---|
| **DTS** | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
| **PTS** | 1 | 4 | 2 | 3 | 7 | 5 | 6 |

Three rules follow, and every muxer and decoder relies on them:

1. **DTS only ever goes up** in stream order.
2. **DTS ≤ PTS** for every frame, because a frame can't be shown before it is decoded. That
   is why PTS is shifted by one slot in the table.
3. **PTS goes up only when there are no B-frames.**

None of the cameras on this Pi use B-frames. Measured 2026-10-17, ffprobe reports
`has_b_frames=0` for cam-01 (the Pi's own H.264 encoder) and cam-03 (H.265); cam-02 was not
publishing at the time. A packet dump from cam-01 shows the two stamps equal:

```
packet|pts_time=0.310111|dts_time=0.310111|flags=___
packet|pts_time=0.374089|dts_time=0.374089|flags=___
```

## Cameras over RTP carry only one timestamp

An IP camera sends video over RTSP/RTP. The RTP header has **a single 32-bit timestamp on a
90 kHz clock**, and that timestamp is the capture instant, effectively the **PTS**. **RTP
has no DTS field.** The receiver has to work out DTS from the bitstream itself. The SPS (the
stream's sequence parameter set) declares how many frames can be reordered, which tells the
receiver how far DTS must lag behind PTS.

In GStreamer that job falls to the depayloader and parser (`rtph265depay ! h265parse`).
Whether they actually fill in DTS depends on the camera's bitstream. That gap is what
stopped cam-03 from ever reaching the cloud (`FoundAndFixed.md` #57):

- `h265parse` produced buffers with `dts: none`.
- kvssink drops any buffer flagged HEADER that lacks a valid DTS. Every keyframe carries the
  in-band VPS/SPS/PPS, so every keyframe was dropped.
- With no keyframes, no fragment could start, and PutMedia starved even though the unit
  showed `active`.
- The fix is `h265timestamper` at the end of the H.265 chain (`video_depay_chain` in
  [`adapter/bin/producer-lib.sh`](adapter/bin/producer-lib.sh)). It computes DTS from PTS
  plus the SPS reorder depth, so it stays correct for a future camera that does use
  B-frames.

## Audio: no reordering, so PTS only

Audio frames never reorder, so GStreamer's convention is to give audio buffers **only a
PTS**. kvssink needs a DTS for every frame, so it makes one up: `last_dts + 40 ms`. But it
uses **one `last_dts` counter shared with the video track**. If two audio frames land
between consecutive video frames, the made-up DTS runs past the next real video DTS, the
sequence goes backwards, and KVS rejects the frames with `0x30000005`
(`STATUS_CONTENT_VIEW_INVALID_TIMESTAMP`). Fragments still persist and `ffprobe` is
happy; the only symptom is that about half the audio is missing (`FoundAndFixed.md` #15).

This is why the audio sample rate is chosen against the video frame rate rather than for
sound quality ([guide §18.3](Demo-AWS-Video-revCosts4.md)). `voaacenc` emits 1024-sample
frames, and each audio frame must last longer than one video frame interval: at 15 fps that
means 1024/rate > 66.7 ms, so a rate below ~15.4 kHz. cam-01 runs 16 kHz at its frame
rate and measures zero rejects; recount them after changing any camera's frame rate.

**Correction, 2026-10-17: the explanation above is in doubt; the rule is not.** Printed per
buffer just before kvssink, the producers' audio (`voaacenc ! aacparse`) arrives **with a DTS
of its own**, equal to its PTS, so kvssink's invented `last_dts + 40 ms` doesn't apply to it.
The measured rule (8 kHz: zero rejects; 48 kHz: half the audio lost) stands.

**Resolved the same evening:** the invented DTS is the **video's**. `h264parse` leaves the Pi
encoder's frames at `dts: none`, so kvssink stamps each video frame `last_dts + 40 ms`. When the
video frames come faster than the audio frames, consecutive invented video stamps overtake the
next audio frame. A second cause hit every audio camera: the SDK merges the tracks by PTS rounded
to 1 ms but checks DTS at 100 ns. Both are fixed (`FoundAndFixed.md` #62).

## Two clocks in one stream: the H.265 timestamper

Audio and video must share one clock, because KVS orders a stream's frames across tracks by
DTS. `h265timestamper` (the fix for the missing DTS above) shifts every video timestamp by
1000 h, and shifts its segment by the same amount so *running time* is unchanged. kvssink reads
the raw timestamps, though. With video at `1000:00:03.0` and audio at `0:00:03.0`, KVS refused
audio frames in bursts. `identity single-segment=true` after the timestamper re-stamps video to
running time and fixed it (`FoundAndFixed.md` #58). Printing both tracks' `dts`/`pts` right
before kvssink is how it was found:

```bash
... ! aacparse ! identity name=AUD silent=false ! fakesink \
... ! h265timestamper ! identity name=VID silent=false ! fakesink      # gst-launch-1.0 -v
```

## How the containers store them

- **MKV**, which KVS PutMedia uses: per-frame timestamps, and the KVS SDK enforces rule 1
  (`dts >= lastClusterDts` in `MkvGenerator.c`).
- **Fragmented MP4 / HLS**, the playback side: stores DTS plus a *composition offset*
  (PTS − DTS) per sample, so B-frame streams survive the round trip.

## Inspecting them

```bash
# per-packet PTS/DTS from any local stream
ffprobe -v error -rtsp_transport tcp -select_streams v:0 -read_intervals "%+#20" \
  -show_entries packet=pts_time,dts_time,flags -of compact rtsp://127.0.0.1:8554/cam03

# inside a GStreamer chain: insert before the sink and read "pts:" / "dts:" per buffer
... ! identity silent=false ! fakesink -v

# audio timestamp rejects on a producer (want 0)
journalctl -u kvs-cam01.service --since "-60 s" | grep -c 0x30000005
```

The `identity` trick is how the missing DTS in #57 was found. If a producer is connected to
KVS but sends nothing, check timestamps with it first.
