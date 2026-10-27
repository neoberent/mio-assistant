#!/usr/bin/env bash
# Installs the assistant for the current user. Safe to re-run.
# Needs the system packages first (see docs/README.md, step 1).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
UNITS="$CFG/systemd/user"

need=(uv gtk4-layer-shell webkitgtk-6.0 python-gobject python-cairo wtype wl-clipboard playerctl grim libnotify portaudio)
missing=()
for p in "${need[@]}"; do pacman -Q "$p" &>/dev/null || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "Missing packages: ${missing[*]}"
  echo "  sudo pacman -S --needed ${missing[*]} ollama-cuda"
  exit 1
fi

echo "==> Python 3.12 venv"
cd "$ROOT"
uv python install 3.12
uv sync --python 3.12 --extra piper

echo "==> Assets"
"$ROOT/scripts/fetch_assets.sh"

echo "==> Config"
mkdir -p "$CFG/assistant"
[ -f "$CFG/assistant/config.toml" ] || cp "$ROOT/config/config.toml" "$CFG/assistant/config.toml"
if [ ! -f "$CFG/assistant/env" ]; then
  cp "$ROOT/config/env.example" "$CFG/assistant/env"
fi
chmod 600 "$CFG/assistant/env"

echo "==> Speech model (first run downloads ~1.6 GB)"
LD_LIBRARY_PATH="$("$ROOT/.venv/bin/python" -c 'import nvidia, os; d=list(nvidia.__path__)[0]; print(":".join(os.path.join(d,x,"lib") for x in os.listdir(d)))')" \
  "$ROOT/.venv/bin/python" -c "
from assistant import config; from assistant.stt import STT
STT(config.load().stt).load()"

echo "==> Local model"
if command -v ollama >/dev/null; then
  model="$("$ROOT/.venv/bin/python" -c 'from assistant import config; print(config.load().brain["ollama_model"])')"
  ollama pull "$model" || echo "  (ollama pull failed — is ollama.service running?)"
fi

echo "==> Command + services"
mkdir -p "$HOME/.local/bin" "$UNITS"
ln -sf "$ROOT/bin/assistant" "$HOME/.local/bin/assistant"
for unit in assistantd.service assistant-avatar.service; do
  sed "s|%h/mio-assistant|$ROOT|" "$ROOT/systemd/$unit" > "$UNITS/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now assistantd.service assistant-avatar.service

echo "==> niri keybinds"
cp "$ROOT/niri/assistant.kdl" "$CFG/niri/assistant.kdl"
if ! grep -q 'include "assistant.kdl"' "$CFG/niri/config.kdl"; then
  printf '\n// Desktop assistant keybinds (Mod+A talk, Mod+Shift+A show/hide, Mod+Ctrl+A stop)\ninclude "assistant.kdl"\n' >> "$CFG/niri/config.kdl"
fi
niri validate && niri msg action load-config-file || echo "  niri config did not validate — check $CFG/niri/assistant.kdl"

echo
echo "Done. Press Mod+A and talk. Status: assistant status · Logs: journalctl --user -fu assistantd"
