#!/usr/bin/env bash
# Deploy this repository's Lambda code -- to pir-* functions ONLY (PIR-MQTT-VMS-PI4.md §3.9).
#
# The AWS account is shared with the successor (VideoSafeZone), which owns every unprefixed
# function. A deploy from here to one of those silently replaced the successor's code once
# (FoundAndFixed.md #48); this script makes that impossible by refusing any other name.
#
#   cloud/deploy-pir.sh pir-control pir-list-clips     # named functions
#   cloud/deploy-pir.sh all                            # every pir-* function in pir-stack.json
#   cloud/deploy-pir.sh client                         # the page, to this project's bucket only
#
# pir-<name> deploys cloud/lambda/<name with _>.py; pir-control deploys pir_control.py.
# The zip is built in a temporary directory, so nothing lands in the repo.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
[ $# -ge 1 ] || { echo "usage: $0 pir-<function>... | all | client" >&2; exit 2; }
stack() { python3 -c "import json,sys; print(json.load(open('$HERE/pir-stack.json'))[sys.argv[1]])" "$1"; }

if [ "$1" = client ]; then
  # The page goes to pir-client-<account> (Phase 11), never to the shared client bucket, whose
  # index.html is the successor's and unversioned: an overwrite there can't be undone (#48).
  bucket="$(stack clientBucket)"; page="$HERE/../client/index.html"
  case "$bucket" in pir-client-*) ;; *) echo "REFUSED: '$bucket' is not this project's page bucket (#48)" >&2; exit 2;; esac
  # ...and only a page that talks to this project's API and app client.
  for want in "const API = \"$(stack invokeUrl)\"" "const COGNITO_CLIENT_ID = \"$(stack appClientId)\""; do
    grep -qF "$want" "$page" || { echo "REFUSED: client/index.html lacks: $want" >&2; exit 2; }
  done
  aws s3 cp "$page" "s3://$bucket/index.html" --only-show-errors \
    --content-type "text/html; charset=utf-8" --cache-control "no-cache, must-revalidate"
  echo "deployed client/index.html -> s3://$bucket/index.html; live at $(stack pageUrl)"
  exit 0
fi
if [ "$1" = all ]; then
  set -- $(python3 -c "import json; print(' '.join(json.load(open('$HERE/pir-stack.json'))['functions']))")
fi
for fn in "$@"; do
  case "$fn" in pir-*) ;; *) echo "REFUSED: '$fn' is not a pir- function -- unprefixed functions belong to the successor (#48)" >&2; exit 2;; esac
done
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for fn in "$@"; do
  if [ "$fn" = pir-control ]; then src="$HERE/lambda/pir_control.py"; else n="${fn#pir-}"; src="$HERE/lambda/${n//-/_}.py"; fi
  [ -f "$src" ] || { echo "no source $src for $fn" >&2; exit 1; }
  (cd "$(dirname "$src")" && zip -qj "$tmp/$fn.zip" "$(basename "$src")")
  sha=$(aws lambda update-function-code --function-name "$fn" --zip-file "fileb://$tmp/$fn.zip" --query CodeSha256 --output text)
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo "deployed $fn <- ${src#$HERE/} ($sha)"
done
