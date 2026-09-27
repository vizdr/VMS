#!/usr/bin/env bash
# $VMS_HOME if exported (interactive shells, via ~/.bashrc); otherwise the repo root
# this script lives in (systemd units do not read ~/.bashrc).
VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"
source "${VMS_HOME}/adapter/bin/detect-hw.sh"
camera_setup cam01 || exit 1            # sets CAM (detected, or pinned in /etc/adapter/cameras/cam01.env)
# Everything the pipeline is built from, defaulted to the values that ran before this file
# existed (the PW310 tuning) and overridable per camera in /etc/adapter/cameras/cam01.env.
# Nothing about the capture device is hardcoded below this point.
: "${CAPS:=image/jpeg,width=1280,height=720,framerate=30/1}"   # what we ask the camera for
: "${CAM_FPS_OUT:=15}"                                         # after videorate drop-only
: "${VIDEO_BITRATE:=1000000}"
: "${GOP:=30}"                                                 # h264_i_frame_period
: "${H264_PROFILE:=high}"                                      # NOT baseline -- guide 16.1
: "${H264_LEVEL:=4}"
: "${AUDIO_RATE:=16000}"                                       # see the DTS check below

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
# The microphone is the sound card on the same USB device as $CAM, found through sysfs
# (detect-hw.sh), and addressed by name, never hw:N -- card numbers move on reboot
# (guide 18.1). Matching by USB parent rather than by card name matters: ALSA calls many
# webcams' mics plain "Webcam", so a second camera would make the name ambiguous.
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
AUDIO_DEV="$(alsa_card_for_video "$CAM" || true)"

# Read once at startup; see adapter/bin/camera-audio.py for why this is never re-read.
# `|| true` so an unreachable registry degrades to video-only instead of leaving cam-01
# with no feed at all.
AUDIO_ENV="$(${VMS_HOME}/venv-adapter/bin/python3 \
             ${VMS_HOME}/adapter/bin/camera-audio.py cam-01 || true)"
eval "${AUDIO_ENV}"
# A pinned audioDevice is an OVERRIDE, not an instruction to fail. It is honoured only if
# that card is actually present: ALSA card ids are not stable identities -- many webcams'
# mics are called plain "Webcam", and a second one becomes "Webcam_1" in plug-in order --
# so a pin written for one camera can name a card that no longer exists, or worse, a
# different microphone. When it does not resolve, discovery wins and the reason is logged.
#
# Degrading matters more here than it looks: video and audio share ONE pipeline, so
# alsasrc failing to open a stale device ("Could not open audio device for recording")
# takes the *video* down with it -- cam-01 disappears entirely because of an audio setting.
# Same principle as the `|| true` above: a bad setting costs the setting, never the camera.
if [ -n "${AUDIO_DEVICE:-}" ]; then
  pin_card="${AUDIO_DEVICE#hw:CARD=}"; pin_card="${pin_card%%,*}"
  if [ -d "/proc/asound/${pin_card}" ]; then
    AUDIO_DEV="${AUDIO_DEVICE}"
  else
    echo "publish-cam01: registry pins audioDevice=${AUDIO_DEVICE} but ALSA card '${pin_card}'" \
         "is not present -- ignoring the pin and using ${AUDIO_DEV:-no microphone}" >&2
  fi
fi
if [ "${AUDIO:-off}" = "on" ] && [ -z "${AUDIO_DEV}" ]; then
  # A stale audioEnabled on a camera with no microphone must not become a pipeline that
  # waits forever for audio (the same reason camera-audio.py gates on audioCapable).
  echo "publish-cam01: audio requested but $CAM has no microphone -- video only" >&2
  AUDIO=off
fi

# --print-config: report the resolved settings and exit, without building a pipeline.
# The GUI's scan uses this instead of carrying its own copy of the defaults above --
# whoever owns a default should be the one that reports it, or the two drift.
if [ "${1:-}" = "--print-config" ]; then
  for _v in CAPS CAM_FPS_OUT VIDEO_BITRATE GOP H264_PROFILE H264_LEVEL AUDIO_RATE \
            CAM_MATCH CAM_DEVICE; do
    printf '%s=%s\n' "$_v" "${!_v-}"
  done
  exit 0
fi

# The decode stage depends on what the camera sends, so it is chosen from CAPS rather than
# assumed. MJPG is hardware-decoded (v4l2jpegdec); raw formats such as YUYV need no decoder
# at all, because v4l2convert already normalises to I420 for the encoder. Anything else is
# refused loudly rather than building a pipeline that cannot link.
case "${CAPS%%,*}" in
  image/jpeg)  DECODE="v4l2jpegdec ! " ;;
  video/x-raw) DECODE="" ;;
  *) echo "publish-cam01: unsupported capture media type in CAPS='${CAPS}'" \
          "(expected image/jpeg or video/x-raw)" >&2; exit 1 ;;
esac

# Guide 18.3: kvssink synthesises audio DTS from a counter shared with the video track, so
# an audio frame must last at least as long as a video frame, or the timestamps run past
# the next video frame and KVS silently drops half the audio. The downstream encoder emits
# 1024 samples per frame, so the comparison is 1024/AUDIO_RATE against 1/CAM_FPS_OUT.
# cam-01's 16 kHz against 15 fps is 64 ms vs 66.7 ms -- inside the limit but only just, and
# measured at zero rejects, so the check allows a 5% margin before complaining. It warns
# rather than refusing: a marginal rate costs audio, and losing the camera would be worse.
audio_frame_ms=$(( 1024000 / AUDIO_RATE ))
video_frame_ms=$(( 1000 / CAM_FPS_OUT ))
if [ $(( audio_frame_ms * 100 )) -lt $(( video_frame_ms * 95 )) ]; then
  echo "publish-cam01: WARNING AUDIO_RATE=${AUDIO_RATE} gives ${audio_frame_ms}ms audio" \
       "frames against ${video_frame_ms}ms video frames at ${CAM_FPS_OUT}fps -- KVS will" \
       "reject audio (guide 18.3). Use AUDIO_RATE <= $(( 1024 * CAM_FPS_OUT ))." >&2
fi

VIDEO_CHAIN="v4l2src device=$CAM ! \
  ${CAPS} ! \
  ${DECODE}videorate drop-only=true ! video/x-raw,framerate=${CAM_FPS_OUT}/1 ! \
  v4l2convert ! video/x-raw,format=I420 ! \
  v4l2h264enc extra-controls=controls,video_bitrate=${VIDEO_BITRATE},h264_i_frame_period=${GOP},repeat_sequence_header=1 ! \
  video/x-h264,level=(string)${H264_LEVEL},profile=(string)${H264_PROFILE} ! \
  h264parse config-interval=-1"

if [ "${AUDIO:-off}" = "on" ]; then
  echo "publish-cam01: audio ENABLED (${AUDIO_DEV} -> LPCM $((AUDIO_RATE/1000))kHz mono)"
  exec gst-launch-1.0 -v \
    ${VIDEO_CHAIN} ! queue ! rtsp.sink_0 \
    alsasrc device="${AUDIO_DEV}" provide-clock=false do-timestamp=true \
    ! audioconvert ! audioresample \
    ! audio/x-raw,rate=${AUDIO_RATE},channels=1,format=S16BE ! queue ! rtsp.sink_1 \
    rtspclientsink name=rtsp location=rtsp://127.0.0.1:8554/cam01 protocols=tcp
fi

# Video-only. With the shipped defaults this is token-for-token the pipeline that ran
# before audio existed (checked against a stub gst-launch); the values now come from the
# config file, so a different camera changes them by design.
echo "publish-cam01: audio disabled (video only)"
# The same VIDEO_CHAIN as the audio branch, not a second literal copy of it: the two used
# to be written out separately, which is two pipelines free to drift apart.
exec gst-launch-1.0 -v \
  ${VIDEO_CHAIN} ! \
  rtspclientsink location=rtsp://127.0.0.1:8554/cam01 protocols=tcp
