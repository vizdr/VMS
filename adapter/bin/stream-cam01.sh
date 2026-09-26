#!/usr/bin/env bash
set -e
# $VMS_HOME if exported (interactive shells, via ~/.bashrc); otherwise the repo root
# this script lives in (systemd units do not read ~/.bashrc).
VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"

source "${VMS_HOME}/adapter/bin/adapter-config.sh"   # AWS_REGION, THING_NAME, IOT_* (/etc/adapter/adapter.env)
CERTS="${VMS_HOME}/certs"

# The AAC encode happens HERE, not in publish-cam01.sh, and the reason is not taste.
# Publishing AAC into MediaMTX works and looks correct -- but rtspclientsink payloads it
# as MPEG-4 LATM, and the LATM round-trip re-wraps the AudioSpecificConfig: it comes back
# out as the 4-byte `14081fe0` (channelConfiguration=0 plus trailing config bits) instead
# of the canonical 2-byte form. kvssink ingests that happily and KVS then refuses to
# play it back: "InvalidCodecPrivateDataException: AAC CPD must be of length 2 or 5, but
# was 4" on GetHLSStreamingSessionURL. rtspclientsink's payloader is a per-pad property
# and cannot be forced from gst-launch syntax, so the fix is to not send AAC over RTSP at
# all: publish-cam01.sh sends LPCM, and voaacenc's own codec_data reaches kvssink
# untouched -- the same path cam-02 already proves works.
#
# The audio therefore rides the same RTSP session as the video rather than being captured
# separately here, which is what keeps A/V sync honest: both tracks inherit one timeline
# from rtspsrc instead of racing an independent ALSA clock against rtspsrc's latency.
KVSSINK="kvssink name=kvs stream-name=cam-01 aws-region=${AWS_REGION} \
  iot-certificate=iot-certificate,endpoint=${IOT_CRED_ENDPOINT},cert-path=${CERTS}/adapter.cert.pem,key-path=${CERTS}/adapter.private.key,ca-path=${CERTS}/cacert.pem,role-aliases=${IOT_ROLE_ALIAS},iot-thing-name=${THING_NAME}"

AUDIO_ENV="$(${VMS_HOME}/venv-adapter/bin/python3 \
             ${VMS_HOME}/adapter/bin/camera-audio.py cam-01 || true)"
eval "${AUDIO_ENV}"

if [ "${AUDIO:-off}" = "on" ]; then
  echo "stream-cam01: audio ENABLED (LPCM from MediaMTX -> AAC 16kHz)"
  # The pads must be split by `application/x-rtp,media=...`; linking them bare lets audio
  # mislink into the video depayloader. kvssink's audio pad wants stream-format=raw, not
  # aacparse's default adts.
  #
  # 16 kHz is chosen against the video frame interval, not for fidelity: voaacenc emits
  # 1024-sample frames, so 16 kHz gives 64ms frames against cam-01's 66.7ms (15fps) video
  # -- see stream-cam02.sh for why that ratio decides whether KVS accepts the frames at
  # all. Measured 0 rejects; if the video framerate changes, recount them.
  exec gst-launch-1.0 -v \
    rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 name=src \
    src. ! application/x-rtp,media=video ! queue \
    ! rtph264depay ! h264parse config-interval=-1 \
    ! video/x-h264,stream-format=avc,alignment=au ! queue ! kvs.video_0 \
    src. ! application/x-rtp,media=audio ! queue \
    ! rtpL16depay ! audioconvert \
    ! voaacenc bitrate=32000 ! aacparse \
    ! audio/mpeg,mpegversion=4,stream-format=raw ! queue ! kvs.audio_0 \
    ${KVSSINK}
fi

# Video-only: byte-for-byte the pipeline that ran before audio existed.
echo "stream-cam01: audio disabled (video only)"
exec gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! ${KVSSINK}
