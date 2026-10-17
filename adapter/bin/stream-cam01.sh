#!/usr/bin/env bash
set -e
# $VMS_HOME if exported (interactive shells, via ~/.bashrc); otherwise the repo root
# this script lives in (systemd units do not read ~/.bashrc).
VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"

source "${VMS_HOME}/adapter/bin/adapter-config.sh"   # AWS_REGION, THING_NAME, IOT_* (/etc/adapter/adapter.env)
source "${VMS_HOME}/adapter/bin/producer-lib.sh"     # producer_run
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

# cam-01's microphone reaches MediaMTX only through the publisher (publish-cam01.sh), a
# long-running user unit that reads the same flag once, at *its* start. Start restarts this
# producer, not the publisher, so a flag switched since then left the two disagreeing: audio
# on here with no audio track in MediaMTX made this pipeline wait on an audio pad forever,
# and kvssink, collecting across its pads, stalled the video with it (FoundAndFixed.md #61).
# So Start brings the publisher in line first. Same user (the unit's User=), lingering is on,
# so its user manager is reachable. Off is reconciled too: off must stop the capture, which
# the outage and PIR rings would otherwise record.
cam01_audio() {   # "down" while the path isn't ready, else the audio codec MediaMTX receives or "none"
  "${VMS_HOME}/venv-adapter/bin/python3" - <<EOF 2>/dev/null || echo down
import sys; sys.path.insert(0, "${VMS_HOME}/adapter")
import mediamtx_api as m
s = m.get_path_state("cam01") or {}
a = m.path_audio_track("cam01")
print("down" if not s.get("ready") else (a["codec"] if a else "none"))
EOF
}
want=$([ "${AUDIO:-off}" = "on" ] && echo mic || echo none)
have="$(cam01_audio)"
mismatch=no
if [ "${have}" != "down" ]; then
  if [ "${want}" = mic ] && [ "${have}" = none ]; then mismatch=yes; fi
  if [ "${want}" = none ] && [ "${have}" != none ]; then mismatch=yes; fi
fi
if [ "${mismatch}" = yes ]; then
  echo "stream-cam01: audio is ${AUDIO:-off} but MediaMTX receives audio '${have}' -- restarting kvs-camera-publish to match"
  XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user restart kvs-camera-publish.service \
    || echo "stream-cam01: could not restart kvs-camera-publish" >&2
  for _ in $(seq 1 30); do
    sleep 1
    have="$(cam01_audio)"
    if [ "${have}" = "down" ]; then continue; fi
    if [ "${want}" = none ] || [ "${have}" != none ]; then break; fi
  done
fi
if [ "${AUDIO:-off}" = "on" ] && { [ "${have}" = none ] || [ "${have}" = down ]; }; then
  echo "stream-cam01: audio is on, but MediaMTX receives no audio from the publisher -- video only" >&2
  AUDIO=off
fi

# With audio, the video gets a real DTS: h264parse leaves the Pi encoder's frames at `dts: none`,
# kvssink then invents last_dts + 40 ms per video frame, and two video frames in a row (66.7 ms
# apart) overtook the next 64 ms audio frame, which KVS refused (FoundAndFixed.md #62).
# h264timestamper computes DTS from PTS; identity undoes its 1000 h offset, as in the H.265 chain.
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
  producer_run stream-cam01 -v \
    rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 name=src \
    src. ! application/x-rtp,media=video ! queue \
    ! rtph264depay ! h264parse config-interval=-1 \
    ! video/x-h264,stream-format=avc,alignment=au ! h264timestamper ! identity single-segment=true \
    ! queue ! kvs.video_0 \
    src. ! application/x-rtp,media=audio ! queue \
    ! rtpL16depay ! audioconvert \
    ! voaacenc bitrate=32000 ! aacparse \
    ! audio/mpeg,mpegversion=4,stream-format=raw ! queue ! kvs.audio_0 \
    ${KVSSINK}
fi

# Video-only. The audio pad is sunk into a fakesink rather than left unlinked -- an
# unlinked rtspsrc pad can abort the whole pipeline with "not-linked (-1)" depending on pad
# ordering (FoundAndFixed.md #43). cam-01 has no audio track when audio is off, so this
# branch is inert here; it is kept identical to stream-cam02.sh's on purpose, because the
# same script shape is what publish-cam01.sh's LPCM leg can start feeding at any time.
echo "stream-cam01: audio disabled (video only)"
producer_run stream-cam01 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam01" protocols=tcp latency=200 name=src \
  src. ! application/x-rtp,media=video ! queue \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! ${KVSSINK} \
  src. ! application/x-rtp,media=audio ! queue ! fakesink sync=false async=false
