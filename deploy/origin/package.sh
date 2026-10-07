#!/usr/bin/env bash
# Produce an isolated release from a committed tree, never from working files.
set -euo pipefail
if [[ $# != 2 || ! $1 =~ ^[0-9a-f]{40}$ || $2 != /* ]]; then
  echo 'Usage: package.sh FULL_COMMIT_SHA ABSOLUTE_NEW_OUTPUT_DIRECTORY' >&2
  exit 2
fi
revision=$1
output=$2
repo=$(git rev-parse --show-toplevel)
[[ $(git -C "$repo" rev-parse "$revision^{commit}") == "$revision" ]]
mkdir -m 700 "$output"
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
paths=(tradingagents/__init__.py tradingagents/brokers/__init__.py
       tradingagents/brokers/mt5_snapshot.py tradingagents/brokers/demo_telemetry.py
       tradingagents/brokers/mt5_collector.py requirements-origin-service.txt deploy/origin)
git -C "$repo" archive "$revision" "${paths[@]}" | tar -x -C "$staging"
printf '%s\n' "$revision" > "$staging/REVISION"
tar -czf "$output/origin-demo-$revision.tar.gz" -C "$staging" .
(cd "$output" && sha256sum "origin-demo-$revision.tar.gz" > SHA256SUMS)
echo "Release prepared from $revision"
