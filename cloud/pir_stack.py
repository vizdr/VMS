#!/usr/bin/env python3
"""Provision this project's own cloud resources -- the `pir-` namespace (PIR-MQTT-VMS-PI4.md §3.9).

The AWS account is shared with the successor (VideoSafeZone, FoundAndFixed.md #48). Everything
created here is NEW and named `pir-…`, so the successor can neither overwrite it nor be
overwritten by it, and nothing shared is modified -- with one deliberate, additive exception:
the inline policy PirLocal on the device role KVSAdapterRole, which both projects' Pis assume
(§4.4). Re-running is safe: every step checks what already exists.

  pir_stack.py            create what is missing, then print what exists
  pir_stack.py --check    only report

It also creates this project's page hosting (Phase 11): the private bucket pir-client-<account>,
readable only by its own CloudFront distribution through an Origin Access Control. The page
itself is uploaded with `cloud/deploy-pir.sh client`.

Run with the operator's AWS CLI credentials (not the device certificate), from the repo root:
  venv-adapter/bin/python3 cloud/pir_stack.py

Lambda CODE is deployed afterwards with cloud/deploy-pir.sh; this tool only creates functions
(with the current code) and keeps their configuration and policies right.
"""
import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
HERE = Path(__file__).resolve().parent
IAM_DIR, LAMBDA_DIR = HERE / "iam", HERE / "lambda"
OUT_FILE = HERE / "pir-stack.json"

POOL_NAME = "kvs-demo-users"          # the shared Cognito pool: same users, own app client
API_NAME, STAGE = "pir-api", "prod"
TABLE = "pir-local"
REQUEST_INDEX = "upload-requests"     # sparse: rows with uploadRequestedAt (§3.11)
CLIENT_NAME = "pir-web"
OAC_NAME = "pir-client-oac"
SITE_COMMENT = "pir-client: this project's cloud page (PIR-MQTT-VMS-PI4.md §3.9)"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"   # AWS managed; the shared page's too
DEVICE_ROLE, DEVICE_POLICY = "KVSAdapterRole", "PirLocal"
BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

# Copies of the shared functions (option (b)): this repository's code, the live original's
# runtime/timeout/memory/environment, and the SAME policy documents as the original's role --
# the files check-drift.sh already compares against the deployed originals.
REG = "cameras-registry-read-policy.json:CamerasRegistryRead"
FUNCTIONS = {
    # pir-name               original                    policies (file:inline-name)
    "pir-list-cameras":      ("list-cameras",            ["list-cameras-policy.json:ListCamerasAccess"]),
    "pir-get-hls-url":       ("get-hls-url",             ["get-hls-url-policy.json:GetHlsUrlAccess", REG]),
    "pir-publish-cmd":       ("publish-cmd",             ["publish-cmd-policy.json:PublishCmdAccess", REG]),
    "pir-record-clip":       ("record-clip",             ["clip-to-s3-policy.json:ClipToS3Access", REG]),
    "pir-list-clips":        ("list-clips",              ["list-clips-policy.json:ListClipsAccess", REG]),
    "pir-play-clip":         ("play-clip",               ["play-clip-policy.json:PlayClipAccess"]),
    "pir-set-clip-tier":     ("set-clip-tier",           ["set-clip-tier-policy.json:SetClipTierAccess"]),
    "pir-delete-clip":       ("delete-clip",             ["delete-clip-policy.json:DeleteClipAccess"]),
    "pir-set-camera-mode":   ("set-camera-mode",         ["set-camera-mode-policy.json:SetCameraModeAccess"]),
    "pir-set-camera-audio":  ("set-camera-audio",        ["set-camera-audio-policy.json:cameras-rw"]),
    "pir-set-camera-outage-buffer": ("set-camera-outage-buffer", ["set-camera-outage-buffer-policy.json:cameras-rw"]),
    "pir-control":           (None,                      ["pir-control-policy.json:PirControlAccess"]),
}
# Routes: the shared API's eleven, unchanged, plus pir-control's four.
ROUTES = [
    ("/cameras", "GET", "pir-list-cameras"), ("/cameras/mode", "POST", "pir-set-camera-mode"),
    ("/cameras/audio", "POST", "pir-set-camera-audio"),
    ("/cameras/outage-buffer", "POST", "pir-set-camera-outage-buffer"),
    ("/cmd", "POST", "pir-publish-cmd"), ("/hls", "GET", "pir-get-hls-url"),
    ("/clips", "GET", "pir-list-clips"), ("/clips/record", "POST", "pir-record-clip"),
    ("/clips/play", "POST", "pir-play-clip"), ("/clips/tier", "POST", "pir-set-clip-tier"),
    ("/clips/delete", "POST", "pir-delete-clip"),
    ("/pir", "GET", "pir-control"), ("/pir/clips", "GET", "pir-control"),
    ("/pir/mode", "POST", "pir-control"), ("/pir/upload", "POST", "pir-control"),
]

s = boto3.session.Session(region_name=REGION)
iam, lam, ddb = s.client("iam"), s.client("lambda"), s.client("dynamodb")
apigw, cognito, sts = s.client("apigateway"), s.client("cognito-idp"), s.client("sts")
s3, cloudfront = s.client("s3"), s.client("cloudfront")
ACCOUNT = sts.get_caller_identity()["Account"]
CLIENT_BUCKET = f"pir-client-{ACCOUNT}"


def say(msg):
    print(msg, flush=True)


def source_file(fn):
    return LAMBDA_DIR / ("pir_control.py" if fn == "pir-control" else fn[4:].replace("-", "_") + ".py")


def zipped(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, path.name)
    return buf.getvalue()


# --- table, device policy, app client ---------------------------------------------------------

def ensure_table(check):
    try:
        ddb.describe_table(TableName=TABLE)
        say(f"ok      table {TABLE}")
    except ddb.exceptions.ResourceNotFoundException:
        if check:
            return say(f"MISSING table {TABLE}")
        ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "cameraId", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "cameraId", "KeyType": "HASH"},
                                    {"AttributeName": "sk", "KeyType": "RANGE"}])
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
        say(f"created table {TABLE} (on-demand; PK cameraId, SK sk)")
    ttl = ddb.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
        if check:
            return say(f"MISSING TTL on {TABLE}")
        ddb.update_time_to_live(TableName=TABLE, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
        say(f"enabled TTL on {TABLE}.ttl")
    ensure_request_index(check)


def ensure_request_index(check):
    """The sparse index the Pi's uploader polls every 30 s (§3.11). Only rows with an
    uploadRequestedAt are in it, and each carries a few hundred bytes. Querying the table itself
    would read every clip row, thumbnails included: at a few thousand rows that is ~30 MB per
    poll, dollars a day, for the handful of rows anyone ever asks for."""
    desc = ddb.describe_table(TableName=TABLE)["Table"]
    gsi = {g["IndexName"]: g for g in desc.get("GlobalSecondaryIndexes", [])}.get(REQUEST_INDEX)
    if gsi:
        return say(f"ok      index {TABLE}/{REQUEST_INDEX} ({gsi['IndexStatus']})")
    if check:
        return say(f"MISSING index {TABLE}/{REQUEST_INDEX}")
    ddb.update_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "cameraId", "AttributeType": "S"},
                              {"AttributeName": "uploadRequestedAt", "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[{"Create": {
            "IndexName": REQUEST_INDEX,
            "KeySchema": [{"AttributeName": "cameraId", "KeyType": "HASH"},
                          {"AttributeName": "uploadRequestedAt", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "INCLUDE",
                           "NonKeyAttributes": ["sessionId", "requestedBy", "uploadedKey", "uploadError"]}}}])
    for _ in range(60):
        time.sleep(5)
        g = {g["IndexName"]: g for g in ddb.describe_table(TableName=TABLE)["Table"]
             .get("GlobalSecondaryIndexes", [])}.get(REQUEST_INDEX, {})
        if g.get("IndexStatus") == "ACTIVE":
            break
    say(f"created index {TABLE}/{REQUEST_INDEX} ({g.get('IndexStatus')})")


def ensure_device_policy(check):
    doc = (IAM_DIR / "pir-local-device-policy.json").read_text()
    try:
        cur = iam.get_role_policy(RoleName=DEVICE_ROLE, PolicyName=DEVICE_POLICY)["PolicyDocument"]
        if cur == json.loads(doc):
            return say(f"ok      {DEVICE_ROLE}/{DEVICE_POLICY}")
    except iam.exceptions.NoSuchEntityException:
        pass
    if check:
        return say(f"MISSING {DEVICE_ROLE}/{DEVICE_POLICY} (or it differs from the file)")
    iam.put_role_policy(RoleName=DEVICE_ROLE, PolicyName=DEVICE_POLICY, PolicyDocument=doc)
    say(f"put     {DEVICE_ROLE}/{DEVICE_POLICY} -- additive; the successor's Pi assumes this role too (§4.4)")


def pool_id():
    for p in cognito.list_user_pools(MaxResults=60)["UserPools"]:
        if p["Name"] == POOL_NAME:
            return p["Id"]
    sys.exit(f"user pool {POOL_NAME} not found")


def ensure_client(pool, check):
    for c in cognito.list_user_pool_clients(UserPoolId=pool, MaxResults=60)["UserPoolClients"]:
        if c["ClientName"] == CLIENT_NAME:
            say(f"ok      app client {CLIENT_NAME} ({c['ClientId']})")
            return c["ClientId"]
    if check:
        return say(f"MISSING app client {CLIENT_NAME}")
    c = cognito.create_user_pool_client(
        UserPoolId=pool, ClientName=CLIENT_NAME, GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        RefreshTokenValidity=30, EnableTokenRevocation=True)["UserPoolClient"]
    say(f"created app client {CLIENT_NAME} ({c['ClientId']}) -- same settings as kvs-demo-web-client")
    return c["ClientId"]


# --- roles and functions ----------------------------------------------------------------------

def ensure_role(fn, policies, check):
    name = f"{fn}-role"
    trust = (IAM_DIR / "lambda-trust.json").read_text()
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        created = False
    except iam.exceptions.NoSuchEntityException:
        if check:
            say(f"MISSING role {name}")
            return None
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust,
                              Description=f"{fn} (VMS, PIR-MQTT-VMS-PI4.md §3.9)")["Role"]["Arn"]
        iam.attach_role_policy(RoleName=name, PolicyArn=BASIC_EXEC)
        created = True
    for spec in policies:
        file, pname = spec.split(":")
        doc = (IAM_DIR / file).read_text()
        try:
            same = iam.get_role_policy(RoleName=name, PolicyName=pname)["PolicyDocument"] == json.loads(doc)
        except iam.exceptions.NoSuchEntityException:
            same = False
        if not same and not check:
            iam.put_role_policy(RoleName=name, PolicyName=pname, PolicyDocument=doc)
        elif not same:
            say(f"DRIFT   role {name}/{pname} differs from {file}")
    say(f"{'created' if created else 'ok     '} role {name} ({', '.join(p.split(':')[1] for p in policies)})")
    return arn


def ensure_function(fn, original, role_arn, check):
    try:
        lam.get_function(FunctionName=fn)
        return say(f"ok      function {fn}")
    except lam.exceptions.ResourceNotFoundException:
        pass
    if check:
        return say(f"MISSING function {fn}")
    if original:
        o = lam.get_function_configuration(FunctionName=original)
        cfg = {"Runtime": o["Runtime"], "Timeout": o["Timeout"], "MemorySize": o["MemorySize"],
               "Architectures": o.get("Architectures", ["arm64"]),
               "Environment": {"Variables": (o.get("Environment") or {}).get("Variables", {})},
               "Handler": o["Handler"]}
    else:
        cfg = {"Runtime": "python3.13", "Timeout": 10, "MemorySize": 128, "Architectures": ["arm64"],
               "Environment": {"Variables": {"PIR_TABLE": TABLE}},
               "Handler": "pir_control.lambda_handler"}
    code = zipped(source_file(fn))
    for attempt in range(12):            # a new role takes a few seconds to become assumable
        try:
            lam.create_function(FunctionName=fn, Role=role_arn, Code={"ZipFile": code},
                                Description=f"VMS copy of {original}" if original else "VMS PIR control",
                                **cfg)
            break
        except lam.exceptions.InvalidParameterValueException as e:
            if "role" not in str(e).lower() or attempt == 11:
                raise
            time.sleep(5)
    lam.get_waiter("function_active_v2").wait(FunctionName=fn)
    say(f"created function {fn}" + (f" (config from {original})" if original else ""))


# --- the API ------------------------------------------------------------------------------------

def find_api():
    for a in apigw.get_rest_apis(limit=500)["items"]:
        if a["name"] == API_NAME:
            return a["id"]
    return None


def ensure_api(pool, check):
    api = find_api()
    if not api:
        if check:
            say(f"MISSING api {API_NAME}")
            return None
        api = apigw.create_rest_api(name=API_NAME, description="VMS-only API (PIR-MQTT-VMS-PI4.md §3.9)",
                                    endpointConfiguration={"types": ["EDGE"]})["id"]
        say(f"created api {API_NAME} ({api})")
    else:
        say(f"ok      api {API_NAME} ({api})")
    if check:
        return api

    auths = apigw.get_authorizers(restApiId=api)["items"]
    auth = next((a["id"] for a in auths if a["name"] == "CognitoAuthorizer"), None) or apigw.create_authorizer(
        restApiId=api, name="CognitoAuthorizer", type="COGNITO_USER_POOLS",
        providerARNs=[f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/{pool}"],
        identitySource="method.request.header.Authorization")["id"]

    # Unlike the shared API: error responses API Gateway generates itself (an expired token's
    # 401, a throttle) carry CORS too, so the page sees the real status, not a CORS failure.
    for rtype in ("DEFAULT_4XX", "DEFAULT_5XX"):
        apigw.put_gateway_response(restApiId=api, responseType=rtype, responseParameters={
            "gatewayresponse.header.Access-Control-Allow-Origin": "'*'",
            "gatewayresponse.header.Access-Control-Allow-Headers": "'Content-Type,Authorization'"})

    resources = {r["path"]: r["id"] for r in apigw.get_resources(restApiId=api, limit=500)["items"]}

    def resource(path):
        if path in resources:
            return resources[path]
        parent, leaf = path.rsplit("/", 1)
        rid = apigw.create_resource(restApiId=api, parentId=resource(parent or "/"), pathPart=leaf)["id"]
        resources[path] = rid
        return rid

    methods_by_path = {}
    for path, method, _ in ROUTES:
        methods_by_path.setdefault(path, []).append(method)
    for path, method, fn in ROUTES:
        rid = resource(path)
        uri = (f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
               f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{fn}/invocations")
        try:
            apigw.get_method(restApiId=api, resourceId=rid, httpMethod=method)
        except apigw.exceptions.NotFoundException:
            apigw.put_method(restApiId=api, resourceId=rid, httpMethod=method,
                             authorizationType="COGNITO_USER_POOLS", authorizerId=auth)
            apigw.put_integration(restApiId=api, resourceId=rid, httpMethod=method, type="AWS_PROXY",
                                  integrationHttpMethod="POST", uri=uri)
        sid = f"pir-api-{method}-{path.strip('/').replace('/', '-')}"
        try:
            lam.add_permission(FunctionName=fn, StatementId=sid, Action="lambda:InvokeFunction",
                               Principal="apigateway.amazonaws.com",
                               SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api}/*/{method}{path}")
        except lam.exceptions.ResourceConflictException:
            pass                          # already granted
    for path, methods in methods_by_path.items():
        rid = resources[path]
        try:
            apigw.get_method(restApiId=api, resourceId=rid, httpMethod="OPTIONS")
            continue
        except apigw.exceptions.NotFoundException:
            pass
        allow = ",".join(sorted(set(methods)) + ["OPTIONS"])
        hdr = "method.response.header.Access-Control-Allow-"
        apigw.put_method(restApiId=api, resourceId=rid, httpMethod="OPTIONS", authorizationType="NONE")
        apigw.put_integration(restApiId=api, resourceId=rid, httpMethod="OPTIONS", type="MOCK",
                              requestTemplates={"application/json": '{"statusCode":200}'})
        apigw.put_method_response(restApiId=api, resourceId=rid, httpMethod="OPTIONS", statusCode="200",
                                  responseParameters={hdr + h: True for h in ("Headers", "Methods", "Origin")})
        apigw.put_integration_response(restApiId=api, resourceId=rid, httpMethod="OPTIONS", statusCode="200",
                                       responseParameters={hdr + "Headers": "'Content-Type,Authorization'",
                                                           hdr + "Methods": f"'{allow}'",
                                                           hdr + "Origin": "'*'"})
    dep = apigw.create_deployment(restApiId=api, stageName=STAGE, description="pir_stack.py")["id"]
    say(f"deployed {API_NAME} to stage {STAGE} ({dep}); {len(ROUTES)} routes, CORS on every path")
    return api


# --- the page's hosting (Phase 11, decision C2) ------------------------------------------------
# Mirrors the shared distribution E1B12167KKII6B (guide §8.5.1): S3 origin through an OAC,
# index.html as root object, HTTPS redirect, the managed CachingOptimized policy -- the page's
# own `Cache-Control: no-cache, must-revalidate` makes CloudFront revalidate on every request,
# so an upload is live at once. Unlike the shared bucket, this one is private from the start:
# Block Public Access on, no website endpoint, readable only by this distribution.

def ensure_client_bucket(check):
    try:
        s3.head_bucket(Bucket=CLIENT_BUCKET)
        say(f"ok      bucket {CLIENT_BUCKET}")
    except s3.exceptions.ClientError:
        if check:
            return say(f"MISSING bucket {CLIENT_BUCKET}")
        s3.create_bucket(Bucket=CLIENT_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        say(f"created bucket {CLIENT_BUCKET}")
    if check:
        return
    s3.put_public_access_block(Bucket=CLIENT_BUCKET, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def ensure_oac(check):
    for o in cloudfront.list_origin_access_controls()["OriginAccessControlList"].get("Items", []):
        if o["Name"] == OAC_NAME:
            say(f"ok      origin access control {OAC_NAME} ({o['Id']})")
            return o["Id"]
    if check:
        return say(f"MISSING origin access control {OAC_NAME}")
    oac = cloudfront.create_origin_access_control(OriginAccessControlConfig={
        "Name": OAC_NAME, "Description": f"OAC for {CLIENT_BUCKET}",
        "SigningProtocol": "sigv4", "SigningBehavior": "always",
        "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]
    say(f"created origin access control {OAC_NAME} ({oac['Id']})")
    return oac["Id"]


def find_distribution():
    origin = f"{CLIENT_BUCKET}.s3.{REGION}.amazonaws.com"
    for page in cloudfront.get_paginator("list_distributions").paginate():
        for d in page["DistributionList"].get("Items", []):
            if any(o["DomainName"] == origin for o in d["Origins"]["Items"]):
                return d
    return None


def ensure_distribution(oac, check):
    d = find_distribution()
    if d:
        say(f"ok      distribution {d['Id']} https://{d['DomainName']} ({d['Status']})")
        return d
    if check or not oac:
        return say("MISSING distribution for " + CLIENT_BUCKET)
    origin = f"{CLIENT_BUCKET}.s3.{REGION}.amazonaws.com"
    d = cloudfront.create_distribution(DistributionConfig={
        "CallerReference": f"{CLIENT_BUCKET}-{int(time.time())}", "Comment": SITE_COMMENT,
        "Enabled": True, "DefaultRootObject": "index.html", "PriceClass": "PriceClass_100",
        "HttpVersion": "http2", "IsIPV6Enabled": True,
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "pir-client-s3", "DomainName": origin, "OriginAccessControlId": oac,
            "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "pir-client-s3", "ViewerProtocolPolicy": "redirect-to-https",
            "CachePolicyId": CACHING_OPTIMIZED, "Compress": True,
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}},
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True}})["Distribution"]
    say(f"created distribution {d['Id']} https://{d['DomainName']} ({d['Status']}; deploying takes minutes)")
    return {"Id": d["Id"], "ARN": d["ARN"], "DomainName": d["DomainName"], "Status": d["Status"]}


def ensure_client_bucket_policy(dist, check):
    want = {"Version": "2012-10-17", "Statement": [{
        "Sid": "PirClientDistributionOnly", "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{CLIENT_BUCKET}/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": dist["ARN"]}}}]}
    try:
        cur = json.loads(s3.get_bucket_policy(Bucket=CLIENT_BUCKET)["Policy"])
    except s3.exceptions.ClientError:
        cur = None
    if cur == want:
        return say(f"ok      bucket policy {CLIENT_BUCKET} (distribution {dist['Id']} only)")
    if check:
        return say(f"MISSING bucket policy {CLIENT_BUCKET} (or it differs)")
    s3.put_bucket_policy(Bucket=CLIENT_BUCKET, Policy=json.dumps(want))
    say(f"put     bucket policy {CLIENT_BUCKET}: GetObject for distribution {dist['Id']} only")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only report what exists")
    check = ap.parse_args().check

    say(f"account {ACCOUNT}, region {REGION}" + (" -- check only" if check else ""))
    ensure_table(check)
    ensure_device_policy(check)
    pool = pool_id()
    client = ensure_client(pool, check)
    for fn, (original, policies) in FUNCTIONS.items():
        role = ensure_role(fn, policies, check)
        if role:
            ensure_function(fn, original, role, check)
    api = ensure_api(pool, check)
    ensure_client_bucket(check)
    oac = ensure_oac(check)
    dist = ensure_distribution(oac, check)
    if dist:
        ensure_client_bucket_policy(dist, check)
    if api and client and dist and not check:
        out = {"region": REGION, "apiId": api, "invokeUrl": f"https://{api}.execute-api.{REGION}.amazonaws.com/{STAGE}",
               "userPoolId": pool, "appClientId": client, "table": TABLE,
               "clientBucket": CLIENT_BUCKET, "distributionId": dist["Id"],
               "pageUrl": f"https://{dist['DomainName']}",
               "functions": sorted(FUNCTIONS), "routes": [f"{m} {p}" for p, m, _ in ROUTES]}
        OUT_FILE.write_text(json.dumps(out, indent=2) + "\n")
        say(f"wrote {OUT_FILE.relative_to(HERE.parent)}: {out['invokeUrl']}")


if __name__ == "__main__":
    main()
