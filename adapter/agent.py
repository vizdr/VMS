import asyncio, json, subprocess, threading
from awscrt import mqtt
from awsiot import mqtt_connection_builder
from onvif import ONVIFCamera

THING   = "adapter-01"
CMD_T   = f"adapter/{THING}/cmd"
STATE_T = f"adapter/{THING}/state"

ALLOWED_CAMERAS = {"cam-01", "cam-02"}  # matches each Lambda's own allow-list

# Only cam-02 (the real ONVIF IPC) has IR-illumination control -- cam-01 is a plain USB
# webcam with no ONVIF/IR-cut hardware. Standard ONVIF only exposes IrCutFilter
# (ON/OFF/AUTO) -- this camera's Hikvision-OEM firmware would normally also offer a
# separate ISAPI supplementLight endpoint for finer IR-LED-only control, but that path
# 404s on this particular rebrand, so IrCutFilter is the only lever actually available.
ONVIF_CONFIG = {
    "cam-02": {
        "host": "192.168.178.67", "port": 80, "user": "admin", "password": "153456",
        "wsdl_dir": "/home/vladimir/MyProjects/VMS/venv-adapter/lib/python3.13/site-packages/onvif/wsdl",
        "video_source_token": "VideoSourceMain",
    },
}
ALLOWED_IR_MODES = {"AUTO", "ON", "OFF"}

def set_ir_mode(camera: str, mode: str):
    cfg = ONVIF_CONFIG[camera]

    async def _set():
        cam = ONVIFCamera(cfg["host"], cfg["port"], cfg["user"], cfg["password"],
                           wsdl_dir=cfg["wsdl_dir"])
        await cam.update_xaddrs()
        imaging = await cam.create_imaging_service()
        await imaging.SetImagingSettings({
            "VideoSourceToken": cfg["video_source_token"],
            "ImagingSettings": {"IrCutFilter": mode},
        })
        await cam.close()

    asyncio.run(_set())

def set_stream(on: bool, camera: str):
    action = "start" if on else "stop"
    # Naming mismatch: the "camera" identifier is "cam-02" (matching the KVS stream name,
    # DynamoDB cameraId, S3 key prefix -- with a hyphen), but the systemd units are
    # kvs-cam01.service / kvs-cam02.service (no hyphen before the digits, per §7.1's
    # original naming). Naively f-stringing "kvs-{camera}.service" builds
    # "kvs-cam-02.service", which doesn't exist -- systemctl silently no-ops against an
    # unknown unit (subprocess.run's check=False swallows the failure), so the command
    # appears to succeed while doing nothing. Strip the hyphen to match the real unit name.
    unit = f"kvs-{camera.replace('-', '')}.service"
    subprocess.run(["sudo", "systemctl", action, unit], check=False)

def on_message(topic, payload, **kwargs):
    msg = json.loads(payload)
    cmd = msg.get("action")
    camera = msg.get("camera", "cam-01")   # older clients/messages omit this -- default cam-01
    if cmd in ("start", "stop") and camera in ALLOWED_CAMERAS:
        set_stream(cmd == "start", camera)
        conn.publish(topic=STATE_T,
                     payload=json.dumps({"camera": camera, "streaming": cmd == "start"}),
                     qos=mqtt.QoS.AT_LEAST_ONCE)
    elif cmd == "ir":
        mode = msg.get("mode")
        if camera in ONVIF_CONFIG and mode in ALLOWED_IR_MODES:
            try:
                set_ir_mode(camera, mode)
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
