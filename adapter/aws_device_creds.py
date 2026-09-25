"""Temporary AWS credentials for this device, via its existing IoT X.509 identity.

This is the same mTLS credentials-endpoint call kvssink already makes for KVS uploads
(see stream-cam01.sh's iot-certificate provider) -- reused here so any Python running on
this Pi that needs AWS API access (the ONVIF admin GUI, agent.py) borrows the device's
existing certificate-backed identity instead of holding a separate static credential.
Keeping this as one shared helper, rather than a second role/mechanism, is deliberate:
§6 of the guide replaced static keys with X.509 specifically because a long-lived key
sitting on an unattended embedded device is the actual liability -- adding a second
static credential for the admin tooling would reopen exactly that.
"""
import os
import json
import ssl
import urllib.request

import boto3

CRED_ENDPOINT = "c38gt2us7mrsmf.credentials.iot.eu-central-1.amazonaws.com"
# $VMS_HOME if set, otherwise the repo root (this file lives in adapter/).
VMS_HOME = os.environ.get("VMS_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERTS = os.path.join(VMS_HOME, "certs")
ROLE_ALIAS = "KVSAdapterRoleAlias"
THING_NAME = "adapter-01"


def get_session(region: str = "eu-central-1") -> boto3.Session:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=f"{CERTS}/adapter.cert.pem", keyfile=f"{CERTS}/adapter.private.key")
    ctx.load_verify_locations(cafile=f"{CERTS}/cacert.pem")

    url = f"https://{CRED_ENDPOINT}/role-aliases/{ROLE_ALIAS}/credentials"
    req = urllib.request.Request(url, headers={"x-amzn-iot-thingname": THING_NAME})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        creds = json.load(resp)["credentials"]

    return boto3.Session(
        aws_access_key_id=creds["accessKeyId"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
        region_name=region,
    )
