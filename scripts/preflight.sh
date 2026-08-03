#!/bin/bash
# Everything that must be green before a tag goes out.
#
# This exists because releases were reaching the one person using Spotted with
# regressions in them. Each individual check already existed; what did not was
# one command that runs all of them, so "I forgot to run the frontend build"
# stopped being possible. Run it before every `git tag`.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAILED=()
run() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    printf '   PASS: %s\n' "$name"
  else
    printf '   FAIL: %s\n' "$name"
    FAILED+=("$name")
  fi
}

run "python tests"        .venv/bin/python -m pytest tests/ -q
run "IPC contract"        .venv/bin/python scripts/check_ipc_contract.py
run "frontend typecheck+build" npm --prefix app run build
# The sidecar binary only exists in CI; an empty file satisfies Tauri's
# bundling resource check so cargo can type-check the Rust locally.
touch app/src-tauri/binaries/spotted-sidecar-aarch64-apple-darwin 2>/dev/null
run "rust check"          cargo check --manifest-path app/src-tauri/Cargo.toml

printf '\n=== version files agree ===\n'
TAURI=$(grep -m1 '"version"' app/src-tauri/tauri.conf.json | sed -E 's/.*"([0-9][^"]*)".*/\1/')
PKG=$(grep -m1 '"version"' app/package.json | sed -E 's/.*"([0-9][^"]*)".*/\1/')
CARGO=$(grep -m1 '^version' app/src-tauri/Cargo.toml | sed -E 's/.*"([0-9][^"]*)".*/\1/')
echo "  tauri.conf.json=$TAURI  package.json=$PKG  Cargo.toml=$CARGO"
if [ "$TAURI" = "$PKG" ] && [ "$PKG" = "$CARGO" ]; then
  echo "   PASS: version files agree (tag must be v$TAURI)"
else
  echo "   FAIL: version files disagree"
  FAILED+=("version files agree")
fi

printf '\n========================================\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "ALL GREEN. Safe to tag v$TAURI"
  exit 0
fi
printf 'DO NOT TAG. Failed: %s\n' "${FAILED[*]}"
exit 1
