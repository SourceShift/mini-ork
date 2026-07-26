#!/usr/bin/env bash
# Provision the non-Python commands required by the MiniOrk runtime.
# Intended for explicit `make install` use; it never downloads remote scripts.
set -Eeuo pipefail

dry_run=0
case "${1:-}" in
  --dry-run) dry_run=1 ;;
  "") ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

run() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
  [ "$dry_run" = "1" ] || "$@"
}

python_supported() {
  local candidate
  for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" - <<'PY' >/dev/null 2>&1 && return 0
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  done
  return 1
}

bash_supported() {
  command -v bash >/dev/null 2>&1 && [ "$(bash -c 'printf %s "${BASH_VERSINFO[0]:-0}"' 2>/dev/null || echo 0)" -ge 4 ]
}

missing=()
bash_supported || missing+=(bash)
command -v sqlite3 >/dev/null 2>&1 || missing+=(sqlite3)
command -v jq >/dev/null 2>&1 || missing+=(jq)
command -v yq >/dev/null 2>&1 || missing+=(yq)
command -v git >/dev/null 2>&1 || missing+=(git)
command -v curl >/dev/null 2>&1 || missing+=(curl)
python_supported || missing+=(python)

if [ "${#missing[@]}" -eq 0 ]; then
  echo "✓ required system dependencies are already available"
  exit 0
fi

echo "→ missing system dependencies: ${missing[*]}"

case "$(uname -s)" in
  Darwin)
    command -v brew >/dev/null 2>&1 || {
      echo "Homebrew is required to provision missing macOS dependencies: https://brew.sh" >&2
      exit 2
    }
    packages=()
    for dependency in "${missing[@]}"; do
      case "$dependency" in
        bash) packages+=(bash) ;;
        sqlite3) packages+=(sqlite) ;;
        jq|yq|git|curl) packages+=("$dependency") ;;
        python) packages+=(python@3.11) ;;
      esac
    done
    run brew install "${packages[@]}"
    ;;
  Linux)
    if command -v apt-get >/dev/null 2>&1; then
      prefix=()
      [ "$(id -u)" -eq 0 ] || prefix=(sudo)
      packages=()
      for dependency in "${missing[@]}"; do
        case "$dependency" in
          bash) packages+=(bash) ;;
          sqlite3) packages+=(sqlite3) ;;
          jq|yq|git|curl) packages+=("$dependency") ;;
          python) packages+=(python3 python3-venv) ;;
        esac
      done
      run "${prefix[@]}" apt-get update
      run "${prefix[@]}" apt-get install -y "${packages[@]}"
    elif command -v dnf >/dev/null 2>&1; then
      prefix=()
      [ "$(id -u)" -eq 0 ] || prefix=(sudo)
      packages=()
      for dependency in "${missing[@]}"; do
        case "$dependency" in
          sqlite3) packages+=(sqlite) ;;
          python) packages+=(python3) ;;
          *) packages+=("$dependency") ;;
        esac
      done
      run "${prefix[@]}" dnf install -y "${packages[@]}"
    elif command -v pacman >/dev/null 2>&1; then
      prefix=()
      [ "$(id -u)" -eq 0 ] || prefix=(sudo)
      packages=()
      for dependency in "${missing[@]}"; do
        case "$dependency" in
          sqlite3) packages+=(sqlite) ;;
          python) packages+=(python) ;;
          *) packages+=("$dependency") ;;
        esac
      done
      run "${prefix[@]}" pacman -Sy --needed --noconfirm "${packages[@]}"
    else
      echo "No supported Linux package manager found. Install: ${missing[*]}" >&2
      exit 2
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    command -v winget >/dev/null 2>&1 || {
      echo "winget is required to provision missing Windows dependencies: ${missing[*]}" >&2
      exit 2
    }
    for dependency in "${missing[@]}"; do
      case "$dependency" in
        python) package=Python.Python.3.11 ;;
        git) package=Git.Git ;;
        jq) package=jqlang.jq ;;
        yq) package=MikeFarah.yq ;;
        sqlite3) package=SQLite.SQLite ;;
        curl) package=curl.curl ;;
        bash) continue ;; # Git Bash supplies Bash when make is available.
      esac
      run winget install --exact --id "$package" --accept-package-agreements --accept-source-agreements
    done
    ;;
  *)
    echo "Unsupported platform $(uname -s). Install manually: ${missing[*]}" >&2
    exit 2
    ;;
esac

echo "✓ system dependency installation completed; re-run make install if the package manager changed PATH"
