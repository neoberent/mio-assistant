#!/usr/bin/env bash
# Downloads front-end libraries, the sample Live2D model, and voice models. Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/avatar/web"
MODELS="${XDG_DATA_HOME:-$HOME/.local/share}/assistant/models"
mkdir -p "$WEB/vendor" "$WEB/models" "$MODELS/kokoro" "$MODELS/piper"

get() {  # get URL DEST
  if [ -s "$2" ]; then echo "  have $(basename "$2")"; return; fi
  echo "  fetching $(basename "$2")"
  curl -fsSL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"
}

echo "==> Avatar libraries"
get https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js "$WEB/vendor/pixi.min.js"
get https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js "$WEB/vendor/cubism4.min.js"
# Live2D Cubism Core: proprietary but free for personal use (Live2D Proprietary Software License).
get https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js "$WEB/vendor/live2dcubismcore.min.js"

echo "==> Sample model (Hiyori, Live2D Free Material License)"
if [ ! -d "$WEB/models/Hiyori" ]; then
  tmp="$(mktemp -d)"
  git clone -q --depth 1 --filter=blob:none --sparse https://github.com/Live2D/CubismWebSamples "$tmp/s"
  git -C "$tmp/s" sparse-checkout set Samples/Resources/Hiyori
  cp -r "$tmp/s/Samples/Resources/Hiyori" "$WEB/models/Hiyori"
  rm -rf "$tmp"
else
  echo "  have Hiyori"
fi

echo "==> Kokoro TTS (82M, Apache-2.0)"
get https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx "$MODELS/kokoro/kokoro-v1.0.onnx"
get https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin "$MODELS/kokoro/voices-v1.0.bin"

echo "==> Piper fallback voice"
PV="${PIPER_VOICE:-en_US-amy-medium}"
IFS=_- read -r lang region name quality <<<"$PV"
base="https://huggingface.co/rhasspy/piper-voices/resolve/main/$lang/${lang}_$region/$name/$quality/$PV"
get "$base.onnx" "$MODELS/piper/$PV.onnx"
get "$base.onnx.json" "$MODELS/piper/$PV.onnx.json"
echo "==> Assets done"
