#!/usr/bin/env bash
# Provisions ONE new passthrough camera's local systemd wiring: an env file for
# kvs-cam@.service's templated unit (§16.6), then daemon-reload + enable --now.
# Invoked from the ONVIF admin GUI (adapter/onvif-admin/app.py), which runs as the same
# unprivileged user and shells out to this script via sudo rather than writing under
# /etc itself -- this keeps "what root-level file gets written and how" as one
# reviewable, input-validated script instead of scattered across the Flask app. Note:
# on this Pi, `vladimir` already has unrestricted passwordless sudo (Raspberry Pi OS's
# default), so this script is about input hygiene and a single source of truth for the
# templating, not an actual privilege boundary -- that boundary doesn't exist on this
# box regardless of what invokes sudo.
set -euo pipefail

CAMERA_ID="$1"       # e.g. cam-03
MEDIAMTX_PATH="$2"   # e.g. cam03 -- same id, hyphen stripped

if [[ ! "$CAMERA_ID" =~ ^cam-[0-9]{2}$ ]]; then
  echo "invalid camera id: $CAMERA_ID" >&2
  exit 1
fi
if [[ ! "$MEDIAMTX_PATH" =~ ^cam[0-9]{2}$ ]]; then
  echo "invalid mediamtx path: $MEDIAMTX_PATH" >&2
  exit 1
fi

ENV_FILE="/etc/adapter/channels/${MEDIAMTX_PATH}.env"
if [[ -e "$ENV_FILE" ]]; then
  echo "already provisioned: $ENV_FILE exists" >&2
  exit 1
fi

mkdir -p /etc/adapter/channels
cat > "$ENV_FILE" <<EOF
CAMERA_ID=${CAMERA_ID}
MEDIAMTX_PATH=${MEDIAMTX_PATH}
EOF

systemctl daemon-reload
systemctl enable --now "kvs-cam@${MEDIAMTX_PATH}.service"
