#!/usr/bin/env bash
# Resolve the image tags in the Containerfile to current digests and rewrite the @sha256: pins.
#
#   scripts/pin-image-digests.sh           rewrite Containerfile in place (review the diff)
#   scripts/pin-image-digests.sh --check   exit 1 if any digest is still the all-zero placeholder
#
# Needs network and skopeo (preferred) or podman. Run by hand; commit the result as a chore: PR.
set -euo pipefail

file="${CONTAINERFILE:-Containerfile}"
placeholder=0000000000000000000000000000000000000000000000000000000000000000

if [ "${1:-}" = "--check" ]; then
    if grep -q "@sha256:${placeholder}" "$file"; then
        echo "placeholder digests remain in $file: run scripts/pin-image-digests.sh" >&2
        exit 1
    fi
    exit 0
fi

resolve() {
    local ref=$1
    if command -v skopeo >/dev/null 2>&1; then
        # The digest of the raw manifest (the index for multi-arch images) is what a pin names.
        skopeo inspect --raw "docker://${ref}" | sha256sum | cut -d' ' -f1
    else
        podman pull --quiet "${ref}" >/dev/null
        podman image inspect --format '{{.Digest}}' "${ref}" | sed 's/^sha256://'
    fi
}

grep -E '^FROM ' "$file" | awk '{print $2}' | sort -u | while read -r from; do
    ref=${from%@*}
    digest=$(resolve "$ref")
    if ! [[ $digest =~ ^[0-9a-f]{64}$ ]]; then
        echo "could not resolve $ref" >&2
        exit 1
    fi
    sed -i "s|^FROM ${ref}@sha256:[0-9a-f]\{64\}|FROM ${ref}@sha256:${digest}|" "$file"
    echo "${ref} -> sha256:${digest}"
done
