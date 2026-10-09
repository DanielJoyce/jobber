#!/usr/bin/env bash
# Install the git hooks (secret scanning + personal-data guards) for this clone.
#
# gitleaks is built from source by pre-commit, which needs Go. If Go isn't on PATH,
# pre-commit downloads a Go toolchain into ~/.cache/pre-commit (~335 MB with the build).
# This script says so and asks first, instead of letting it happen silently.
#
# Usage: scripts/setup-hooks.sh [--yes]
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

assume_yes=false
[[ "${1:-}" == "--yes" ]] && assume_yes=true

command -v uv >/dev/null || { echo "uv not found: install it first (https://docs.astral.sh/uv/)"; exit 1; }

cache="${PRE_COMMIT_HOME:-$HOME/.cache/pre-commit}"
if compgen -G "$cache/repo*/golangenv-*" >/dev/null; then
  echo "gitleaks environment already built in $cache; nothing to download."
elif command -v go >/dev/null; then
  echo "Found $(go version); gitleaks will be built with it into $cache."
else
  cat <<EOF
Go not found. To build gitleaks, pre-commit will download a Go toolchain
into $cache (about 335 MB including the build). Nothing is installed system-wide.

Alternatives:
  - install Go yourself first (brew install go, or sudo dnf install golang), then rerun
  - answer N and the hooks are not installed
EOF
  if ! $assume_yes; then
    read -r -p "Download Go into $cache and continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted; hooks not installed."; exit 1; }
  fi
  echo "Go not found, downloading via pre-commit..."
fi

uv run pre-commit install
uv run pre-commit install-hooks
echo
echo "Hooks installed. Every commit now runs gitleaks, private-key detection, and the"
echo "personal-path guard. Remove the downloaded toolchain any time: uv run pre-commit clean"
