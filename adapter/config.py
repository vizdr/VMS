"""Deployment identity for this adapter: which AWS account/region, which IoT Thing, which
endpoints. One machine-wide file, /etc/adapter/adapter.env (KEY=VALUE, the same format
systemd's EnvironmentFile= and adapter/bin/adapter-config.sh read), instead of the same
constants repeated across a dozen files -- moving to another account or Thing is then an
edit to one file, not a grep-and-replace. Template: config/adapter.env.example
(LAUNCH.md A7).

Precedence: environment variable > file. There is deliberately no built-in default: a
missing key raises ConfigError naming the key and the file, rather than silently talking
to whichever account a stale default points at. ADAPTER_CONFIG overrides the file path
(tests, a second adapter on one box).
"""
import os

CONFIG_FILE = os.environ.get("ADAPTER_CONFIG", "/etc/adapter/adapter.env")


class ConfigError(RuntimeError):
    pass


def _read_file(path):
    values = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


_FILE = _read_file(CONFIG_FILE)


def get(key):
    value = os.environ.get(key) or _FILE.get(key)
    if not value:
        raise ConfigError(f"{key} is not set: add it to {CONFIG_FILE} "
                          f"(template: config/adapter.env.example, LAUNCH.md A7)")
    return value


AWS_REGION = get("AWS_REGION")
THING_NAME = get("THING_NAME")
IOT_ROLE_ALIAS = get("IOT_ROLE_ALIAS")
IOT_CRED_ENDPOINT = get("IOT_CRED_ENDPOINT")
IOT_DATA_ENDPOINT = get("IOT_DATA_ENDPOINT")
EVIDENCE_BUCKET = get("EVIDENCE_BUCKET")

# MQTT topics are per-Thing; the IoT policy scopes them to ${iot:Connection.Thing.ThingName}.
TOPIC_PREFIX = f"adapter/{THING_NAME}"
