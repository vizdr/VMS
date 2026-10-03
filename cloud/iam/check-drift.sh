#!/usr/bin/env bash
# Compare every policy document in this directory against what is actually attached to the
# deployed role, and report semantic differences (statement sets, action and resource sets
# -- not key order or whitespace).
#
# Why this exists: these files are documentation, not a deployment mechanism. Nothing
# applies them; every widening was done with `aws iam put-role-policy` against the live
# role, and three of them drifted silently for weeks -- still naming per-stream ARNs with
# creation timestamps (`stream/cam-01/1787244081283`) long after the deployed policies had
# moved to the `stream/cam-*/*` wildcard that lets a GUI-registered camera work without an
# IAM edit. Reading the repo, you would conclude a third camera needs an IAM change. It
# does not. See FoundAndFixed.md #41 and guide §8.1.
#
# Usage:  ./check-drift.sh          # report only
#         ./check-drift.sh --pull   # overwrite the local files from the deployed policies
set -u
cd "$(dirname "$(readlink -f "$0")")"
PULL=""
[ "${1:-}" = "--pull" ] && PULL=yes

# file  role  inline-policy-name
MAP='
kvs-producer-policy.json              KVSAdapterRole                    KVSProducer
outage-backfill-policy.json           KVSAdapterRole                    OutageBackfill
clip-to-s3-policy.json                ClipToS3LambdaRole                ClipToS3Access
delete-clip-policy.json               DeleteClipLambdaRole              DeleteClipAccess
get-hls-url-policy.json               GetHlsUrlLambdaRole               GetHlsUrlAccess
list-cameras-policy.json              ListCamerasLambdaRole             ListCamerasAccess
list-clips-policy.json                ListClipsLambdaRole               ListClipsAccess
play-clip-policy.json                 PlayClipLambdaRole                PlayClipAccess
publish-cmd-policy.json               PublishCmdLambdaRole              PublishCmdAccess
set-camera-audio-policy.json          SetCameraAudioLambdaRole          cameras-rw
set-camera-mode-policy.json           SetCameraModeLambdaRole           SetCameraModeAccess
set-camera-outage-buffer-policy.json  SetCameraOutageBufferLambdaRole   cameras-rw
set-clip-tier-policy.json             SetClipTierLambdaRole             SetClipTierAccess
cameras-registry-read-policy.json     GetHlsUrlLambdaRole               CamerasRegistryRead
pir-local-device-policy.json          KVSAdapterRole                    PirLocal
pir-control-policy.json               pir-control-role                  PirControlAccess
list-cameras-policy.json              pir-list-cameras-role             ListCamerasAccess
get-hls-url-policy.json               pir-get-hls-url-role              GetHlsUrlAccess
cameras-registry-read-policy.json     pir-get-hls-url-role              CamerasRegistryRead
publish-cmd-policy.json               pir-publish-cmd-role              PublishCmdAccess
cameras-registry-read-policy.json     pir-publish-cmd-role              CamerasRegistryRead
clip-to-s3-policy.json                pir-record-clip-role              ClipToS3Access
cameras-registry-read-policy.json     pir-record-clip-role              CamerasRegistryRead
list-clips-policy.json                pir-list-clips-role               ListClipsAccess
cameras-registry-read-policy.json     pir-list-clips-role               CamerasRegistryRead
play-clip-policy.json                 pir-play-clip-role                PlayClipAccess
set-clip-tier-policy.json             pir-set-clip-tier-role            SetClipTierAccess
delete-clip-policy.json               pir-delete-clip-role              DeleteClipAccess
set-camera-mode-policy.json           pir-set-camera-mode-role          SetCameraModeAccess
set-camera-audio-policy.json          pir-set-camera-audio-role         cameras-rw
set-camera-outage-buffer-policy.json  pir-set-camera-outage-buffer-role cameras-rw
'
# The pir-* roles (PIR-MQTT-VMS-PI4.md §3.9) are this repository's copies of the shared functions'
# roles and use the SAME files on purpose: a copy's permissions must equal the original's.
# cloud/pir_stack.py puts them from these files. Caveat for --pull: a file listed twice is
# rewritten from whichever row comes last, so pull only after checking which side drifted.
# The comparison ignores Condition blocks, so pir-control-policy.json's attribute limits are
# verified with `aws iam simulate-principal-policy` instead (Phase 9).
# cameras-registry-read is attached identically to four roles (GetHlsUrl, ClipToS3,
# ListClips, PublishCmd); one is checked, since they are kept byte-identical on purpose.
# client-bucket-oac-policy.json, kvs-role-trust.json and lambda-trust.json are not inline
# role policies and are not checked here.

drift=0
while read -r file role policy; do
  [ -z "${file:-}" ] && continue
  if ! aws iam get-role-policy --role-name "$role" --policy-name "$policy" \
         --query PolicyDocument --output json > /tmp/.iam-drift.$$ 2>/dev/null; then
    echo "ABSENT  $file -> $role/$policy does not exist"; drift=1; continue
  fi
  if python3 - "$file" /tmp/.iam-drift.$$ <<'PY'
import json, sys
def norm(d):
    st = d["Statement"]
    st = st if isinstance(st, list) else [st]
    out = []
    for x in st:
        a = x.get("Action"); a = [a] if isinstance(a, str) else a
        r = x.get("Resource"); r = [r] if isinstance(r, str) else (r or [])
        out.append((x.get("Effect"), tuple(sorted(a)), tuple(sorted(r))))
    return sorted(out)
a, b = (norm(json.load(open(p))) for p in sys.argv[1:3])
if a == b:
    sys.exit(0)
for s in b:
    if s not in a: print("   deployed only:", s[1], "->", s[2])
for s in a:
    if s not in b: print("   repo only    :", s[1], "->", s[2])
sys.exit(1)
PY
  then
    echo "ok      $file ($role)"
  else
    echo "DRIFT   $file  ($role/$policy)"; drift=1
    [ -n "$PULL" ] && { cp /tmp/.iam-drift.$$ "$file"; echo "        pulled deployed version into $file"; }
  fi
  rm -f /tmp/.iam-drift.$$
done <<< "$MAP"

[ "$drift" -eq 0 ] && echo "no drift" || echo "drift found -- ./check-drift.sh --pull overwrites the local files"
exit "$drift"
