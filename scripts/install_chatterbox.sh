#!/usr/bin/env bash
# Installs Chatterbox Turbo (Resemble AI, MIT) for Mio's voice into its own venv, so torch never touches Mio's.
# ~4 GB of packages + ~1.5 GB of model weights (Hugging Face cache, fetched on first start). Then set
# [tts] engine = "chatterbox" and restart assistantd. Kokoro stays the fallback.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/assistant/chatterbox"
mkdir -p "$DIR"
cd "$DIR"
[ -x .venv/bin/python ] || uv venv -p 3.12 -q
# setuptools<81: the Perth watermarker still imports pkg_resources
uv pip install -q "chatterbox-tts==0.1.7" "setuptools<81"
echo "warming up (downloads the model the first time)..."
printf '{"text": "Hi, I am Mio."}\n' | .venv/bin/python -I "$ROOT/assistant/tts_worker.py" \
  | head -c 300 | head -2 | grep -a '^{' || { echo "worker test failed"; exit 1; }
echo "chatterbox installed in $DIR"
