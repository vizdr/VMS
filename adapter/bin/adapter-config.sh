# Sourced, not executed: loads the adapter's deployment identity (AWS region, IoT Thing,
# endpoints) from /etc/adapter/adapter.env -- the shell twin of adapter/config.py; that
# module's docstring has the why. Environment variables already set win over the file,
# and every key must end up set, or the caller exits with a message naming the file.
#
#   source "${VMS_HOME}/adapter/bin/adapter-config.sh"

source "$(dirname "${BASH_SOURCE[0]}")/env-file.sh"

ADAPTER_CONFIG="${ADAPTER_CONFIG:-/etc/adapter/adapter.env}"
load_env_file "$ADAPTER_CONFIG"

for _key in AWS_REGION THING_NAME IOT_ROLE_ALIAS IOT_CRED_ENDPOINT IOT_DATA_ENDPOINT EVIDENCE_BUCKET; do
  if [ -z "${!_key:-}" ]; then
    echo "adapter-config: $_key is not set: add it to $ADAPTER_CONFIG" \
         "(template: config/adapter.env.example, LAUNCH.md A7)" >&2
    exit 1
  fi
done
unset _key
