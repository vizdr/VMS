#!/usr/bin/env bash
CAM=/dev/v4l/by-id/usb-Generic_AVerMedia_PW310_Webcam_200901010001-video-index0

# Audio goes into MediaMTX as LPCM, deliberately uncompressed. Encoding it to AAC here
# instead is the obvious move and it is a trap: rtspclientsink payloads AAC as MPEG-4
# LATM, and the round-trip mangles the codec private data into a form KVS will ingest but
# refuses to play back. stream-cam01.sh carries the full account. LPCM at 16 kHz mono is
# 256 kbps over loopback, which costs nothing here and never leaves the Pi.
#
# Guide 18.2 suggests bypassing MediaMTX entirely for A/V; that advice predates MediaMTX
# becoming the hub, and following it now would cost the local preview, the Start/Stop
# layer, and the single-producer-per-camera model. MediaMTX carries two tracks fine.
#
# Device by persistent name, never hw:N -- card numbers move on reboot (guide 18.1).
# The PW310 capture device is STEREO-ONLY (ALSA reports CHANNELS: 2, a fixed value not a
# range), so `-c 1` is rejected outright; audioconvert does the downmix instead.
#
# 16 kHz, not 48 kHz, and the reason is not audio quality -- see stream-cam02.sh for the
# full account. Short version: kvssink synthesises the DTS that GStreamer audio buffers
# lack, using a counter SHARED with the video track, so an audio frame rate higher than
# the video frame rate makes the synthesised timestamps run backwards and KVS drops the
# frames. voaacenc emits 1024-sample frames, so frame duration is 1024/rate: at 48 kHz
# that is 21ms against a 66.7ms video frame, which fails; 16 kHz gives 64ms. Verified by
# measurement below, not by theory -- if you change the rate, recount the rejects.
AUDIO_DEV=hw:CARD=Webcam,DEV=0

# Read once at startup; see adapter/bin/camera-audio.py for why this is never re-read.
# `|| true` so an unreachable registry degrades to video-only instead of leaving cam-01
# with no feed at all.
AUDIO_ENV="$(/home/vladimir/MyProjects/VMS/venv-adapter/bin/python3 \
             /home/vladimir/MyProjects/VMS/adapter/bin/camera-audio.py cam-01 || true)"
eval "${AUDIO_ENV}"
[ -n "${AUDIO_DEVICE:-}" ] && AUDIO_DEV="${AUDIO_DEVICE}"

VIDEO_CHAIN="v4l2src device=$CAM ! \
  image/jpeg,width=1280,height=720,framerate=30/1 ! \
  v4l2jpegdec ! videorate drop-only=true ! video/x-raw,framerate=15/1 ! \
  v4l2convert ! video/x-raw,format=I420 ! \
  v4l2h264enc extra-controls=controls,video_bitrate=1000000,h264_i_frame_period=30,repeat_sequence_header=1 ! \
  video/x-h264,level=(string)4,profile=(string)high ! \
  h264parse config-interval=-1"

if [ "${AUDIO:-off}" = "on" ]; then
  echo "publish-cam01: audio ENABLED (${AUDIO_DEV} -> LPCM 16kHz mono)"
  exec gst-launch-1.0 -v \
    ${VIDEO_CHAIN} ! queue ! rtsp.sink_0 \
    alsasrc device="${AUDIO_DEV}" provide-clock=false do-timestamp=true \
    ! audioconvert ! audioresample \
    ! audio/x-raw,rate=16000,channels=1,format=S16BE ! queue ! rtsp.sink_1 \
    rtspclientsink name=rtsp location=rtsp://127.0.0.1:8554/cam01 protocols=tcp
fi

# Video-only: byte-for-byte the pipeline that ran before audio existed.
echo "publish-cam01: audio disabled (video only)"
exec gst-launch-1.0 -v \
  v4l2src device="$CAM" ! \
  image/jpeg,width=1280,height=720,framerate=30/1 ! \
  v4l2jpegdec ! videorate drop-only=true ! video/x-raw,framerate=15/1 ! \
  v4l2convert ! video/x-raw,format=I420 ! \
  v4l2h264enc extra-controls="controls,video_bitrate=1000000,h264_i_frame_period=30,repeat_sequence_header=1" ! \
  "video/x-h264,level=(string)4,profile=(string)high" ! \
  h264parse config-interval=-1 ! \
  rtspclientsink location=rtsp://127.0.0.1:8554/cam01 protocols=tcp
