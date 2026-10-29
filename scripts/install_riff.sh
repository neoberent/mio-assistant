#!/usr/bin/env bash
# Installs the Mio plugin into the Riff music player and turns it on. Restart Riff afterwards.
# The plugin gets the bridge token from ~/.local/state/assistant/riff-token (made here if missing).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${RIFF_DATA_DIR:-$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.config/devsize/config.json"))).get("dataDir",""))' 2>/dev/null || true)}"
DATA="${DATA:-$HOME/.cache/devsize}"
DIR="$DATA/plugins"
mkdir -p "$DIR"
read -r TOKEN PORT < <(cd "$ROOT" && .venv/bin/python -c 'from assistant import riff; print(riff.token(), riff.PORT)')
sed -e "s/__TOKEN__/$TOKEN/" -e "s/__PORT__/$PORT/" "$ROOT/riff/mio.js" > "$DIR/mio.js"
[ -f "$DIR/plugins.json" ] && cp "$DIR/plugins.json" "$DIR/plugins.json.bak-$(date +%Y%m%d%H%M%S)"
python3 - "$DIR/plugins.json" <<'PY'
import json, sys
p = sys.argv[1]
try:
    state = json.load(open(p))
except (OSError, ValueError):
    state = {}
state.setdefault("enabled", {})["mio.js"] = True
json.dump(state, open(p, "w"))
PY
echo "installed $DIR/mio.js and turned it on; restart Riff to load it"
