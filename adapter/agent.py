import json, subprocess, threading
from awscrt import mqtt
from awsiot import mqtt_connection_builder

THING   = "adapter-01"
CMD_T   = f"adapter/{THING}/cmd"
STATE_T = f"adapter/{THING}/state"

def set_stream(on: bool):
    action = "start" if on else "stop"
    subprocess.run(["sudo", "systemctl", action, "kvs-cam01.service"], check=False)

def on_message(topic, payload, **kwargs):
    cmd = json.loads(payload).get("action")
    if cmd in ("start", "stop"):
        set_stream(cmd == "start")
        conn.publish(topic=STATE_T,
                     payload=json.dumps({"streaming": cmd == "start"}),
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
