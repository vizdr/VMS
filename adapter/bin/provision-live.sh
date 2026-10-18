#!/usr/bin/env bash
# Adds or removes the live-view producer of one ONVIF camera: the instance of kvs-cam@.service
# that pushes the camera's sub stream (MediaMTX path <path>_live) to the KVS stream
# <cameraId>-live, for the cloud page's live view while the main stream is recorded
# (adapter/live_view.py, guide §21.7).
#
#   provision-live.sh add    cam-03 cam03 kvs-cam@cam03.service
#   provision-live.sh remove cam-03 cam03 kvs-cam@cam03.service
#
# The live producer is tied to the recording producer (the 4th argument) rather than started on
# its own: Wants= from the recording unit starts it with every Start, including at boot, and
# PartOf= stops it with every Stop. So nothing that controls a camera -- agent.py, both GUIs,
# Unregister -- has to know it exists, and it can never run unattended without the recording
# producer, which is the cost rule (guide §1.2).
#
# Like provision-camera.sh, run via sudo from the admin GUI so that what is written under /etc
# is one reviewable, input-validated script.
set -euo pipefail

ACTION="${1:-}"; CAMERA_ID="${2:-}"; MEDIAMTX_PATH="${3:-}"; REC_UNIT="${4:-}"

[[ "$ACTION" =~ ^(add|remove)$ ]] || { echo "usage: $0 add|remove <cam-NN> <camNN> <recording unit>" >&2; exit 1; }
[[ "$CAMERA_ID" =~ ^cam-[0-9]{2}$ ]] || { echo "invalid camera id: $CAMERA_ID" >&2; exit 1; }
[[ "$MEDIAMTX_PATH" =~ ^cam[0-9]{2}$ && "$MEDIAMTX_PATH" == "${CAMERA_ID//-/}" ]] \
  || { echo "invalid mediamtx path for $CAMERA_ID: $MEDIAMTX_PATH" >&2; exit 1; }
[[ "$REC_UNIT" == "kvs-${MEDIAMTX_PATH}.service" || "$REC_UNIT" == "kvs-cam@${MEDIAMTX_PATH}.service" ]] \
  || { echo "invalid recording unit for $CAMERA_ID: $REC_UNIT" >&2; exit 1; }

LIVE_PATH="${MEDIAMTX_PATH}_live"
UNIT="kvs-cam@${LIVE_PATH}.service"
ENV_FILE="/etc/adapter/channels/${LIVE_PATH}.env"
DROPIN_DIR="/etc/systemd/system/${UNIT}.d"
WANTS_LINK="/etc/systemd/system/${REC_UNIT}.wants/${UNIT}"

if [ "$ACTION" = remove ]; then
  systemctl stop "$UNIT" 2>/dev/null || true
  rm -f "$WANTS_LINK"
  rmdir --ignore-fail-on-non-empty "$(dirname "$WANTS_LINK")" 2>/dev/null || true
  rm -rf "$DROPIN_DIR"
  rm -f "$ENV_FILE"
  systemctl daemon-reload
  echo "removed ${UNIT}"
  exit 0
fi

mkdir -p /etc/adapter/channels "$DROPIN_DIR"
# KVS_STREAM and LIVE are read by stream-channel.sh: the stream to push to, and "don't record
# this path's codec as the camera's" (stream-codec.py --live).
cat > "${ENV_FILE}.tmp" <<EOF
CAMERA_ID=${CAMERA_ID}
MEDIAMTX_PATH=${LIVE_PATH}
KVS_STREAM=${CAMERA_ID}-live
LIVE=1
EOF
mv "${ENV_FILE}.tmp" "$ENV_FILE"

cat > "${DROPIN_DIR}/live.conf" <<EOF
# Written by provision-live.sh: the live view of ${CAMERA_ID} runs only alongside its recording producer.
[Unit]
Description=KVS live-view producer for ${CAMERA_ID} (sub stream, ${CAMERA_ID}-live)
PartOf=${REC_UNIT}
EOF

systemctl daemon-reload
systemctl add-wants "$REC_UNIT" "$UNIT"
# Recording already running: start the live view now rather than at the next Start.
if systemctl is-active --quiet "$REC_UNIT"; then
  systemctl start "$UNIT"
fi
echo "added ${UNIT} (with ${REC_UNIT})"
