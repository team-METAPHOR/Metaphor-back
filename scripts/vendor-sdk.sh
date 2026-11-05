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
# 버전 고정 설치 코드와 SRI가 계속 동작하도록 기존 번들을 보관합니다.
if [[ -f "$dest/VERSION" && -f "$dest/metaphor.js" ]]; then
  read -r previous < "$dest/VERSION"
  if [[ "$previous" == "${tag#v}" ]] && ! cmp -s "$dest/metaphor.js" "$tmp/metaphor.js"; then
    echo "이미 고정된 버전의 번들은 바꿀 수 없습니다. SDK 버전을 올리세요." >&2
    exit 1
  fi
  if [[ "$previous" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$previous" != "${tag#v}" ]]; then
    mkdir -p "$dest/versions/$previous"
    install -m 0644 "$dest/metaphor.js" "$dest/versions/$previous/metaphor.js"
    install -m 0644 "$dest/VERSION" "$dest/versions/$previous/VERSION"
  fi
fi
install -m 0644 "$tmp/metaphor.js" "$dest/metaphor.js"
printf '%s\n%s\n' "${tag#v}" "$commit" > "$dest/VERSION"

echo "version:   ${tag#v}"
echo "commit:    $commit"
echo "integrity: sha384-$(openssl dgst -sha384 -binary "$dest/metaphor.js" | openssl base64 -A)"
echo "Update vendor/README.md, then: git add -f vendor/sdk && uv run pytest -q tests/test_sdk_serving.py"
