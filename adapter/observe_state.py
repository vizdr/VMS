import datetime
from awscrt import mqtt, auth, io
from awsiot import mqtt_connection_builder

STATE_T = f"{config.TOPIC_PREFIX}/state"

def on_message(topic, payload, **kwargs):
    ts = datetime.datetime.now().isoformat()
    print(f"{ts}  {topic}  {payload.decode()}", flush=True)

credentials_provider = auth.AwsCredentialsProvider.new_default_chain()

conn = mqtt_connection_builder.websockets_with_default_aws_signing(
    endpoint=config.IOT_DATA_ENDPOINT,
    region=config.AWS_REGION,
    credentials_provider=credentials_provider,
    client_id="observer-admin",
    clean_session=True,
    keep_alive_secs=30,
)
conn.connect().result()
conn.subscribe(topic=STATE_T, qos=mqtt.QoS.AT_LEAST_ONCE, callback=on_message)[0].result()
print("subscribed, watching for state changes...", flush=True)

import threading

import config
threading.Event().wait()
