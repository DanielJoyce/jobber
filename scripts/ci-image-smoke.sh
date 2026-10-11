#!/usr/bin/env bash
# Smoke test for the jobhunter image (spec 018, C8). Run by .github/workflows/image.yml; also
# runnable on a host with podman:  scripts/ci-image-smoke.sh localhost/jobhunter:dev <commit>
#
# No job board is contacted and nothing paid is called: the console is idle, no scorer runs,
# and the only secret is a made-up value. Containers other than the published-port check run
# with --network=none. Every check that greps podman output captures that output first, so a
# failing podman command stops the script (set -e) instead of letting an empty grep pass.
set -euo pipefail

image=${1:?usage: ci-image-smoke.sh IMAGE BUILD_COMMIT}
commit=${2:?usage: ci-image-smoke.sh IMAGE BUILD_COMMIT}
tag=$$
vol="jh-smoke-data-${tag}"
secret="jh-smoke-key-${tag}"
console="jh-smoke-console-${tag}"
secret_ctr="jh-smoke-secret-${tag}"
port=${SMOKE_PORT:-18808}

pass() { echo "ok: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
    podman rm -f "$console" "${console}-default" "$secret_ctr" >/dev/null 2>&1 || true
    podman volume rm -f "$vol" >/dev/null 2>&1 || true
    podman secret rm "$secret" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- size budget (spec 018 Image: default <= 450 MB uncompressed) -------------------------------
budget=${SIZE_BUDGET_BYTES:-450000000}
size=$(podman image inspect --format '{{.Size}}' "$image")
[[ $size =~ ^[0-9]+$ ]] || fail "could not read image size (got '$size')"
echo "image size: $((size / 1000000)) MB (budget $((budget / 1000000)) MB)"
((size <= budget)) || fail "image is over the size budget"
pass "size within budget"

# --- OCI labels and user -----------------------------------------------------------------------
revision=$(podman image inspect --format '{{index .Labels "org.opencontainers.image.revision"}}' "$image")
[ "$revision" = "$commit" ] || fail "revision label is '$revision', expected '$commit'"
pass "revision label"

run() { podman run --rm --network=none "$@"; }

# --- basics, no network ------------------------------------------------------------------------
out=$(run "$image" --help)
grep -q "Usage" <<<"$out" || fail "jobhunter --help printed no usage"
pass "--help"

uid=$(run --entrypoint id "$image" -u)
[ "$uid" = "1000" ] || fail "uid is '$uid', expected 1000"
pass "runs as uid 1000"

out=$(run "$image" version)
grep -qF "$commit" <<<"$out" || fail "jobhunter version does not show build commit $commit: $out"
pass "version shows the build commit"

out=$(run "$image" paths)
[ "$(head -n1 <<<"$out")" = "mode: container" ] || fail "paths first line is not 'mode: container': $out"
for want in "/data" "/cache" "/config"; do
    grep -qF "$want" <<<"$out" || fail "paths output lacks $want: $out"
done
pass "paths reports container mode with /data /cache /config"

# --- no personal or secret files in the image --------------------------------------------------
out=$(run --entrypoint sh "$image" -c 'ls -A /app')
[ "$out" = ".venv" ] || fail "/app holds more than .venv: $out"
out=$(run --entrypoint sh "$image" -c \
    'find /app /home /data /cache /config -maxdepth 4 \( -name ".env*" -o -name "*token*.json" -o -name "*client_secret*.json" -o -name config.toml -o -name "*.local.toml" -o -name data -o -name profile -o -name resume \) -not -path "/app/.venv/*" -not -path "/data" -not -path "/cache" -not -path "/config"')
[ -z "$out" ] || fail "personal or secret files in the image: $out"
out=$(run --entrypoint sh "$image" -c 'find /data /cache /config -mindepth 1')
[ -z "$out" ] || fail "mount points are not empty: $out"
pass "no .env, data, profile, resume, token or client_secret files in the image"

# --- init on a fresh volume --------------------------------------------------------------------
podman volume create "$vol" >/dev/null
run -v "$vol":/data "$image" init >/dev/null
out=$(run -v "$vol":/data --entrypoint sh "$image" -c 'ls /data; stat -c %a /data')
grep -q "jobhunter.db" <<<"$out" || fail "init created no database: $out"
pass "init on a fresh volume"

# --- secret by file: resolves, and never appears in inspect or logs ----------------------------
# The value ends with CRLF to exercise the C2 stripping. It is random, so a stray copy anywhere
# is a real leak and the grep cannot match by accident.
fake="ci-fake-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf '%s\r\n' "$fake" | podman secret create "$secret" - >/dev/null
podman run --name "$secret_ctr" --network=none \
    --secret "$secret,type=mount,target=anthropic_key,mode=0400,uid=1000" \
    -e ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_key \
    "$image" secrets status >/dev/null
status=$(podman logs "$secret_ctr")
grep -Eq '^ANTHROPIC_API_KEY +set \(file /run/secrets/anthropic_key\)$' <<<"$status" \
    || fail "secrets status does not show ANTHROPIC_API_KEY as set (file ...): $status"
grep -qF "$fake" <<<"$status" && fail "secret value in secrets status output"
inspect=$(podman inspect "$secret_ctr")
grep -qF "$secret_ctr" <<<"$inspect" || fail "podman inspect output looks empty (no container name)"
grep -qF "$fake" <<<"$inspect" && fail "secret value in podman inspect"
logs_all=$(podman logs "$secret_ctr" 2>&1)
grep -qF "$fake" <<<"$logs_all" && fail "secret value in podman logs"
podman rm "$secret_ctr" >/dev/null
pass "CRLF secret resolves as set (file ...); value absent from inspect, logs and output"

# --- console: /healthz through a published loopback port, and from inside ----------------------
# Bind 0.0.0.0 inside; the interlock variable says the port is published on loopback only.
podman run -d --name "$console" -p "127.0.0.1:${port}:8808" -v "$vol":/data \
    -e JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1 \
    "$image" console --host 0.0.0.0 >/dev/null
code=000
for _ in $(seq 1 "${SMOKE_WAIT:-30}"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/healthz" || true)
    [ "$code" = "200" ] && break
    sleep 1
done
if [ "$code" != "200" ]; then
    podman logs "$console" >&2 || true
    fail "/healthz via the published port returned $code"
fi
pass "/healthz 200 via 127.0.0.1:${port}"

code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "http://127.0.0.1:${port}/")
[ "$code" = "403" ] || fail "a foreign Host header got $code, expected 403"
pass "foreign Host refused with 403"

# The same check as the unit's HealthCmd would do, from inside the container.
podman exec "$console" python -c \
    'import urllib.request as u, sys; sys.exit(0 if u.urlopen("http://127.0.0.1:8808/healthz", timeout=5).status == 200 else 1)'
pass "/healthz 200 from inside the container"

# The image default (CMD console, loopback bind per D11) must start too; the Quadlet unit uses it.
podman run -d --name "${console}-default" --network=none "$image" >/dev/null
code=000
for _ in $(seq 1 "${SMOKE_WAIT:-30}"); do
    code=$(podman exec "${console}-default" python -c \
        'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8808/healthz", timeout=5).status)' \
        2>/dev/null || true)
    [ "$code" = "200" ] && break
    sleep 1
done
if [ "$code" != "200" ]; then
    podman logs "${console}-default" >&2 || true
    fail "the default CMD did not serve /healthz (got '$code')"
fi
pass "default CMD serves /healthz on loopback"

echo "all image smoke checks passed"
