#!/usr/bin/env bash
# Re-vendor the Metaphor Web SDK bundle from a GitHub Release of team-METAPHOR/Metaphor-sdk.
# Usage: scripts/vendor-sdk.sh v0.1.0
# Writes vendor/sdk/metaphor.js and vendor/sdk/VERSION, then prints the SRI hash.
set -euo pipefail

tag="${1:?usage: scripts/vendor-sdk.sh <tag, e.g. v0.1.0>}"
repo="team-METAPHOR/Metaphor-sdk"
dest="$(cd "$(dirname "$0")/.." && pwd)/vendor/sdk"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh release download "$tag" -R "$repo" -p metaphor.js -D "$tmp"
commit="$(gh api "repos/$repo/commits/$tag" --jq .sha)"

mkdir -p "$dest"
install -m 0644 "$tmp/metaphor.js" "$dest/metaphor.js"
printf '%s\n%s\n' "${tag#v}" "$commit" > "$dest/VERSION"

echo "version:   ${tag#v}"
echo "commit:    $commit"
echo "integrity: sha384-$(openssl dgst -sha384 -binary "$dest/metaphor.js" | openssl base64 -A)"
echo "Update vendor/README.md, then: git add -f vendor/sdk && uv run pytest -q tests/test_sdk_serving.py"
