import json, threading
from awscrt import mqtt
from awsiot import mqtt_connection_builder

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
)
conn.connect().result()
conn.subscribe(topic=CMD_T, qos=mqtt.QoS.AT_LEAST_ONCE, callback=on_message)[0].result()
conn.publish(topic=STATE_T, payload=json.dumps({"online": True}),
             qos=mqtt.QoS.AT_LEAST_ONCE, retain=False)[0].result()
threading.Event().wait()
