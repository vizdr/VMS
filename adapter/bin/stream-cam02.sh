#!/usr/bin/env bash
set -e

CRED_ENDPOINT=c38gt2us7mrsmf.credentials.iot.eu-central-1.amazonaws.com
CERTS=/home/vladimir/MyProjects/VMS/certs

# Video is genuine passthrough -- no jpegdec/videoconvert/v4l2h264enc chain like
# stream-cam01.sh needs for the PW310. The camera already outputs real H.264 (§16.3(a));
# this pipeline just depacketizes RTP and repackages for kvssink, which is why it's
# dramatically lighter on CPU (§16.3(b)'s point, made concrete rather than just argued).
#
# Audio is NOT passthrough, and cannot be. The camera emits G.711 A-law, which kvssink's
# pad template does accept (audio/x-alaw) -- but KVS's *reader* APIs do not: both
# GetHLSStreamingSessionURL and GetClip require "codec private data in the AAC format",
# and reject anything else with UnsupportedStreamMediaTypeException. A-law would
# therefore ingest silently and only fail at playback, so it is transcoded to AAC here.
# Ingest capability and playback capability are separate questions in KVS.
#
# Two details that do not negotiate if you get them wrong:
#   - the RTP pads must be split by `application/x-rtp,media=...`; linking the branches
#     bare lets audio mislink into the video depayloader.
#   - kvssink's audio pad wants stream-format=raw, NOT the adts that aacparse will
#     happily produce by default.
#
# A-law (rtppcmadepay/alawdec), not mu-law: MediaMTX reports muLaw=false and ffprobe
# agrees (pcm_alaw). ONVIF cannot tell you this -- its enum is just "G711".
#
# DO NOT resample the audio up to 48 kHz. It looks harmless and it is not:
# GStreamer audio buffers carry no DTS, and kvssink synthesises one as
# `data->last_dts + 40ms` (gstkvssink.cpp, gst_kvs_sink_handle_buffer) from a last_dts
# field that is SHARED BETWEEN TRACKS. So each audio frame's DTS is derived from the
# most recent *video* frame. voaacenc always emits 1024-sample frames, so the audio
# frame rate follows the sample rate: at 48 kHz that is 21ms frames (~47/s, three per
# video frame), and the synthesised DTS runs past the next real video DTS -- the frames
# are then rejected with 0x30000005 STATUS_CONTENT_VIEW_INVALID_TIMESTAMP. Measured:
# 1.65 rejects/s, and just over half the audio lost (15 kb/s delivered of 32 kb/s sent).
# At the camera's native 8 kHz the frames are 128ms (~8/s, one per two video frames),
# the synthesised +40ms stays inside the 66.7ms video frame interval, and the reject
# rate is exactly zero with 27.8 kb/s delivered.
#
# That margin is 40ms against a 66.7ms video frame interval. If cam-02's video is ever
# raised to 30fps (33ms), the inversions come back -- recheck the reject count.

KVSSINK="kvssink name=kvs stream-name=cam-02 aws-region=eu-central-1 \
  iot-certificate=iot-certificate,endpoint=${CRED_ENDPOINT},cert-path=${CERTS}/adapter.cert.pem,key-path=${CERTS}/adapter.private.key,ca-path=${CERTS}/cacert.pem,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"

# Read once at startup; never polled. See camera-audio.py for why re-reading would be
# actively wrong rather than merely pointless.
# `|| true` because `set -e` is on and this must never be the reason a camera has no
# producer at all -- an unreadable registry degrades to video-only, it does not fail.
AUDIO_ENV="$(/home/vladimir/MyProjects/VMS/venv-adapter/bin/python3 \
             /home/vladimir/MyProjects/VMS/adapter/bin/camera-audio.py cam-02 || true)"
eval "${AUDIO_ENV}"

if [ "${AUDIO:-off}" = "on" ]; then
  echo "stream-cam02: audio ENABLED (${AUDIO_CODEC:-PCMA} -> AAC 8kHz)"
  exec gst-launch-1.0 -v \
    rtspsrc location="rtsp://127.0.0.1:8554/cam02" protocols=tcp latency=200 name=src \
    src. ! application/x-rtp,media=video ! queue \
    ! rtph264depay ! h264parse config-interval=-1 \
    ! video/x-h264,stream-format=avc,alignment=au ! queue ! kvs.video_0 \
    src. ! application/x-rtp,media=audio ! queue \
    ! rtppcmadepay ! alawdec ! audioconvert \
    ! audio/x-raw,rate=8000,channels=1 \
    ! voaacenc bitrate=32000 ! aacparse \
    ! audio/mpeg,mpegversion=4,stream-format=raw ! queue ! kvs.audio_0 \
    ${KVSSINK}
fi

# Video-only: byte-for-byte the pipeline that ran before audio existed, so turning the
# setting off is a true revert and not a second code path that merely resembles one.
echo "stream-cam02: audio disabled (video only)"
exec gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam02" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! ${KVSSINK}
