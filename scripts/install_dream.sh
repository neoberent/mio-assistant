#!/usr/bin/env bash
# Mio's dreaming (assistant/dream.py): a separate venv with unsloth/PyTorch, the weights of her local model to
# train on and llama.cpp's LoRA converter + merge/quantize tools, in ~/.local/share/assistant/dream (~25 GB for
# qwen3.5:9b, ~13 GB for qwen3:8b). Remove that folder to uninstall.
#   scripts/install_dream.sh [qwen3.5:9b|qwen3:8b]     (the [brain] ollama_model she runs; default qwen3.5:9b)
# Optional: `sudo pacman -S swayidle` so she only dreams when you're really away (no keyboard/mouse input).
set -euo pipefail
BASE="${1:-qwen3.5:9b}"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/assistant/dream"
mkdir -p "$DIR"
cd "$DIR"
[ -x venv/bin/python ] || uv venv venv --python 3.12
VIRTUAL_ENV="$DIR/venv" uv pip install unsloth gguf
[ -d llama.cpp ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp llama.cpp
# CPU-only tools to merge the LoRA into Ollama's qwen3 file and quantize it back (Ollama dropped LoRA support)
cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_SERVER=OFF \
      -DLLAMA_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build llama.cpp/build --target llama-export-lora llama-quantize -j"$(nproc)"
case "$BASE" in
  qwen3.5:9b)  # no 4-bit upload: the full weights, quantized while loading (the LoRA converter reads them too)
    venv/bin/python -c 'from huggingface_hub import snapshot_download as d; d("unsloth/Qwen3.5-9B", local_dir="base-qwen35")' ;;
  qwen3:8b)
    venv/bin/python - <<'PY'
from huggingface_hub import hf_hub_download, snapshot_download
snapshot_download("unsloth/Qwen3-8B-unsloth-bnb-4bit", local_dir="base")
for f in ["config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"]:
    hf_hub_download("Qwen/Qwen3-8B", f, local_dir="base-config")
PY
    ;;
  *) echo "no dream setup for $BASE (see BASES in assistant/dream.py)"; exit 1 ;;
esac
ollama pull "$BASE"
ollama pull all-minilm
echo "Done. Mio dreams on her own once she has enough wins; 'assistant learned' shows how far she is."
