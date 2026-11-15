#!/usr/bin/env bash
# Set up the isolated environment of this calibration job, inside the job folder only.
#
#   bash env/bootstrap.sh
#
# Needs: bash, curl or wget, tar, internet access. Steps:
#   1. download uv 0.13.4 for this machine (uname -sm) into env/tools/ and verify its sha256
#      (pinned below, from the official GitHub release .sha256 files);
#   2. uv python install 3.12 (into env/python/, uv-managed CPython only);
#   3. uv venv .venv and uv pip sync --require-hashes env/requirements.lock;
#   4. python run.py selftest (one tiny restart and a nominal validate, capped at 300 s);
#   5. write env_report.json.
# Safe to rerun: a ready environment (env_report.json with "status": "ok") is kept.
set -euo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JOB_DIR"

UV_VERSION=0.13.4
PYTHON_VERSION=3.12

case "$(uname -sm)" in
  "Linux x86_64")
    UV_PLATFORM=x86_64-unknown-linux-gnu
    UV_SHA256=2b428396f12572d4429cf210ca234dd599a33b3af0958a4d5ed569d63e3f344f ;;
  "Linux aarch64" | "Linux arm64")
    UV_PLATFORM=aarch64-unknown-linux-gnu
    UV_SHA256=1ae72b4718729e2ccc4f1ae2c6d1e531ca8467800e0546e9130b3886023a60ab ;;
  "Darwin x86_64")
    UV_PLATFORM=x86_64-apple-darwin
    UV_SHA256=7be2dcd316122f4b3e5a0a7889f6248266c14425374e105ff7dc9779184603a6 ;;
  "Darwin arm64")
    UV_PLATFORM=aarch64-apple-darwin
    UV_SHA256=e0b1d4743ce64c55d9f0f4a444afa6b81125680070ee04e853fcd0f6c1ebf8f1 ;;
  *)
    echo "bootstrap: unsupported platform '$(uname -sm)' (supported: Linux x86_64/aarch64, macOS x86_64/arm64)" >&2
    exit 2 ;;
esac

VENV_PY="$JOB_DIR/.venv/bin/python"

if [ -x "$VENV_PY" ] && [ -f env_report.json ] && "$VENV_PY" -c \
    'import json, sys; sys.exit(json.load(open("env_report.json")).get("status") != "ok")' 2>/dev/null; then
  echo "bootstrap: environment ready (env_report.json status ok)"
  exit 0
fi

say() { echo "bootstrap: $*"; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    echo "bootstrap: need sha256sum or shasum to verify downloads" >&2
    exit 2
  fi
}

download() {
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --silent --show-error --retry 3 --output "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget --quiet --tries=3 --output-document="$2" "$1"
  else
    echo "bootstrap: need curl or wget to download uv" >&2
    exit 2
  fi
}

# 1. pinned uv, checksum verified
TOOLS="$JOB_DIR/env/tools"
UV_DIR="$TOOLS/uv-$UV_VERSION-$UV_PLATFORM"
UV="$UV_DIR/uv"
if [ ! -x "$UV" ]; then
  mkdir -p "$TOOLS"
  ARCHIVE="$TOOLS/uv-$UV_PLATFORM.tar.gz"
  URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_PLATFORM.tar.gz"
  say "downloading uv $UV_VERSION ($UV_PLATFORM)"
  download "$URL" "$ARCHIVE"
  GOT="$(sha256_of "$ARCHIVE")"
  if [ "$GOT" != "$UV_SHA256" ]; then
    rm -f "$ARCHIVE"
    echo "bootstrap: uv archive sha256 mismatch (expected $UV_SHA256, got $GOT); refusing to run it" >&2
    exit 3
  fi
  rm -rf "$UV_DIR.partial"
  mkdir -p "$UV_DIR.partial"
  tar -xzf "$ARCHIVE" -C "$UV_DIR.partial" --strip-components=1
  rm -f "$ARCHIVE"
  mv "$UV_DIR.partial" "$UV_DIR"
fi
"$UV" --version

# uv: ignore user/system uv.toml, keep Python and caches of this job in the job folder
export UV_NO_CONFIG=1
export UV_PYTHON_INSTALL_DIR="$JOB_DIR/env/python"
export UV_PYTHON_PREFERENCE=only-managed
export UV_CACHE_DIR="${UV_CACHE_DIR:-$JOB_DIR/env/uv-cache}"
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME || true

# 2. pinned Python
say "installing Python $PYTHON_VERSION (uv-managed)"
"$UV" python install "$PYTHON_VERSION"

# 3. virtual environment from the hash-locked lock
say "creating .venv and syncing env/requirements.lock (--require-hashes)"
rm -rf "$JOB_DIR/.venv"
"$UV" venv --python "$PYTHON_VERSION" "$JOB_DIR/.venv"
"$UV" pip sync --python "$VENV_PY" --require-hashes env/requirements.lock

# 4. selftest (run.py caps it at 300 s)
say "running the selftest"
SELFTEST_EXIT=0
"$VENV_PY" run.py selftest || SELFTEST_EXIT=$?

# 5. environment report
"$VENV_PY" run.py env-report --uv-version "$UV_VERSION" --uv-platform "$UV_PLATFORM" \
  --uv-sha256 "$UV_SHA256" --selftest-exit "$SELFTEST_EXIT" || true
if [ "$SELFTEST_EXIT" -ne 0 ]; then
  echo "bootstrap: selftest FAILED; see out/selftest/log.txt and env_report.json" >&2
  exit 1
fi
say "environment ready"
