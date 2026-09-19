import json, threading, time
from datetime import datetime, timezone

from awscrt import mqtt
from awsiot import mqtt_connection_builder

import aws_state
import camera_control
from aws_device_creds import get_session

THING   = "adapter-01"
CMD_T   = f"adapter/{THING}/cmd"
STATE_T = f"adapter/{THING}/state"

# Camera existence/capabilities come from the "cameras" DynamoDB registry, not a
# hardcoded set/dict -- the same registry the local ONVIF admin GUI (adapter/onvif-admin)
# writes to when a camera is discovered and registered, and the same one the cloud
# Lambdas already read. A camera added through the GUI works over this MQTT control path
# immediately, with no code edit here.
#
# A fresh session is fetched per message rather than cached at module scope: the IoT
# role-alias credentials this borrows are only valid for 3600s (see aws_device_creds.py),
# and this process is a long-running daemon -- caching them once at import time would
# work fine in every manual test and then start failing with ExpiredTokenException on
# every command after an hour of real uptime. The extra round trip is cheap next to how
# infrequently this fires (a human pressing a button), so there's no reason to build a
# refreshing-credential cache for it.
def get_cameras_table():
    return get_session().resource("dynamodb").Table("cameras")

def on_message(topic, payload, **kwargs):
    msg = json.loads(payload)
    cmd = msg.get("action")
    camera = msg.get("camera", "cam-01")   # older clients/messages omit this -- default cam-01

    cam_item = get_cameras_table().get_item(Key={"cameraId": camera}).get("Item")
    if not cam_item:
        return

    if cmd in ("start", "stop"):
        camera_control.set_stream(camera, cmd == "start")
        conn.publish(topic=STATE_T,
                     payload=json.dumps({"camera": camera, "streaming": cmd == "start"}),
                     qos=mqtt.QoS.AT_LEAST_ONCE)
    elif cmd == "ir" and cam_item.get("hasIrControl"):
        mode = msg.get("mode")
        try:
            camera_control.set_ir_mode(cam_item, mode)
            conn.publish(topic=STATE_T,
                         payload=json.dumps({"camera": camera, "irMode": mode}),
                         qos=mqtt.QoS.AT_LEAST_ONCE)
        except Exception as e:
            conn.publish(topic=STATE_T,
                         payload=json.dumps({"camera": camera, "irError": str(e)}),
                         qos=mqtt.QoS.AT_LEAST_ONCE)

# --- AWS reachability, published for the outage buffer (adapter/outage_buffer.py) ------
#
# This connection is the only MQTT session the adapter is permitted (the IoT policy grants
# iot:Connect on one client id), so this process is the only place the SDK's
# interrupted/resumed callbacks can be observed. It publishes them to a file rather than
# acting on them: recording during an outage is a separate unit's job, and wedging that
# logic in here would put Start/Stop control behind it.
#
# The callbacks set the flag; a heartbeat thread rewrites the file regardless. Without the
# heartbeat, this process dying would leave a file that says "online" forever -- see
# aws_state.py.
_aws = {"online": True, "since": datetime.now(timezone.utc).isoformat(), "seq": 0}
_aws_lock = threading.Lock()


def _publish_aws_state():
    with _aws_lock:
        aws_state.write_state(_aws["online"], _aws["since"], _aws["seq"])


def _set_aws_online(online: bool, why: str):
    with _aws_lock:
        if _aws["online"] == online:
            return
        _aws["online"] = online
        _aws["since"] = datetime.now(timezone.utc).isoformat()
        _aws["seq"] += 1
    print(f"agent: AWS {'RESUMED' if online else 'INTERRUPTED'} ({why})", flush=True)
    _publish_aws_state()


def on_connection_interrupted(connection, error, **kwargs):
    # Fires on a keepalive miss, a TLS failure, throttling, or another client taking this
    # client id. It is a proxy for AWS reachability and not proof about KVS specifically,
    # which is why outage_buffer.py corroborates it with its own probe.
    _set_aws_online(False, str(error))


def on_connection_resumed(connection, return_code, session_present, **kwargs):
    _set_aws_online(True, f"rc={return_code}")


def _heartbeat_loop():
    while True:
        try:
            _publish_aws_state()
        except Exception as e:
            print(f"agent: heartbeat write failed: {e}", flush=True)
        time.sleep(aws_state.HEARTBEAT_SEC)


conn = mqtt_connection_builder.mtls_from_path(
    endpoint="a3dp4umq4qv6ul-ats.iot.eu-central-1.amazonaws.com",
    port=443,                       # ALPN x-amzn-mqtt-ca -- traverses HTTPS-only firewalls
    cert_filepath="/home/vladimir/MyProjects/VMS/certs/adapter.cert.pem",
    pri_key_filepath="/home/vladimir/MyProjects/VMS/certs/adapter.private.key",
    ca_filepath="/home/vladimir/MyProjects/VMS/certs/AmazonRootCA1.pem",
    client_id=THING,
    keep_alive_secs=30,
    clean_session=False,
    will=mqtt.Will(topic=STATE_T,
                   qos=mqtt.QoS.AT_LEAST_ONCE,
                   payload=json.dumps({"online": False}).encode(),
                   retain=False),
    on_connection_interrupted=on_connection_interrupted,
    on_connection_resumed=on_connection_resumed,
)
conn.connect().result()
_publish_aws_state()
threading.Thread(target=_heartbeat_loop, daemon=True).start()
conn.subscribe(topic=CMD_T, qos=mqtt.QoS.AT_LEAST_ONCE, callback=on_message)[0].result()
conn.publish(topic=STATE_T, payload=json.dumps({"online": True}),
             qos=mqtt.QoS.AT_LEAST_ONCE, retain=False)[0].result()
threading.Event().wait()
