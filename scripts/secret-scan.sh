#!/usr/bin/env bash
set -euo pipefail

mode="${1:-all}"
repo_root=$(git rev-parse --show-toplevel)
gitleaks_bin="${GITLEAKS_BIN:-gitleaks}"

if [[ "$gitleaks_bin" == */* ]]; then
  if [[ ! -x "$gitleaks_bin" ]]; then
    printf 'error: Gitleaks executable not found at %s\n' "$gitleaks_bin" >&2
    exit 2
  fi
elif ! command -v "$gitleaks_bin" >/dev/null 2>&1; then
  printf 'error: gitleaks is required; install it or set GITLEAKS_BIN\n' >&2
  exit 2
fi

common=(
  --no-banner
  --no-color
  --redact
  --config "$repo_root/.gitleaks.toml"
)

scan_staged() {
  (
    cd "$repo_root"
    "$gitleaks_bin" git "${common[@]}" --pre-commit --staged .
  )
}

scan_tree() {
  (
    cd "$repo_root"
    "$gitleaks_bin" dir "${common[@]}" .
  )
}

scan_history() {
  (
    cd "$repo_root"
    "$gitleaks_bin" git "${common[@]}" --log-opts="--all" .
  )
}

case "$mode" in
  staged)
    scan_staged
    ;;
  tree)
    scan_tree
    ;;
  history)
    scan_history
    ;;
  all)
    scan_tree
    scan_history
    ;;
  *)
    printf 'usage: %s [staged|tree|history|all]\n' "$0" >&2
    exit 2
    ;;
esac
