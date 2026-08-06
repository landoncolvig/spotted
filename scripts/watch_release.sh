#!/bin/bash
# Watch a tag all the way to actually-downloadable, and say plainly if it didn't.
#
# A release is not shipped when the tag is pushed. It is shipped when the
# workflow succeeds, the release leaves draft, the tarball is uploaded, and
# the updater endpoint serves the new version. A tag was once pushed, believed
# shipped, and never published; the tester sat on a broken build for two days.
# Nobody gets told a fix is available until this prints SHIPPED.
#
# Usage: bash scripts/watch_release.sh 0.0.92
set -uo pipefail
REPO="landoncolvig/spotted"
V="${1:?usage: watch_release.sh <version, e.g. 0.0.92>}"
TAG="v$V"

echo "waiting for the release workflow for $TAG..."
sleep 15
RUN=$(gh run list --repo "$REPO" --workflow Release --limit 1 --json databaseId --jq '.[0].databaseId')
until [ "$(gh run view "$RUN" --repo "$REPO" --json status --jq .status 2>/dev/null)" = "completed" ]; do
  sleep 30
done
CONC=$(gh run view "$RUN" --repo "$REPO" --json conclusion --jq .conclusion)
if [ "$CONC" != "success" ]; then
  echo "NOT SHIPPED: workflow $CONC"
  gh run view "$RUN" --repo "$REPO" 2>&1 | grep -E "^  X" | head -5
  exit 1
fi

echo "waiting for assets to publish..."
for _ in $(seq 1 60); do
  ASSETS=$(gh release view "$TAG" --repo "$REPO" --json isDraft,assets \
    --jq 'select(.isDraft==false)|[.assets[].name]|join(",")' 2>/dev/null)
  case "$ASSETS" in *Spotted.app.tar.gz,*) break;; esac
  sleep 20
done
case "${ASSETS:-}" in
  *Spotted.app.tar.gz,*) ;;
  *) echo "NOT SHIPPED: release still draft or missing the tarball"; exit 1;;
esac

SERVED=$(curl -sL -H 'Cache-Control: no-cache' \
  "https://github.com/$REPO/releases/latest/download/latest.json?cb=$RANDOM" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null)
[ "$SERVED" = "$V" ] || { echo "NOT SHIPPED: updater still serving $SERVED"; exit 1; }

CODE=$(curl -sL -r 0-1023 -o /tmp/_rel_probe.bin -w '%{http_code}' \
  "https://github.com/$REPO/releases/download/$TAG/Spotted.app.tar.gz")
case "$CODE" in 200|206) ;; *) echo "NOT SHIPPED: tarball HTTP $CODE"; exit 1;; esac
file /tmp/_rel_probe.bin | grep -q gzip || { echo "NOT SHIPPED: tarball is not gzip"; exit 1; }

echo "SHIPPED: $TAG published, updater serving $SERVED, tarball downloadable"
