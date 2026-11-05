#!/usr/bin/env bash
# Registers Mio's native messaging host with Zen and with Chromium / Chrome (per user, no sudo).
# Zen: installs the add-on this builds (see the end of the output). Chromium: load browser/extension unpacked.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID="hmjgoilonpblpoamhakkijgoedphnaim"   # fixed by the "key" in browser/extension/manifest.json
ADDON_ID="mio@assistant.local"               # the same for Firefox-based browsers, set when the add-on is built
for dir in "$HOME/.config/chromium" "$HOME/.config/google-chrome"; do
  [ -d "$dir" ] || continue
  mkdir -p "$dir/NativeMessagingHosts"
  cat > "$dir/NativeMessagingHosts/com.mio.assistant.json" <<JSON
{
  "name": "com.mio.assistant",
  "description": "Mio desktop assistant bridge",
  "path": "$ROOT/bin/mio-browser-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$ID/"]
}
JSON
  echo "registered for $dir"
done
# Zen (Firefox-based): same host, but these browsers name the extension by its gecko id.
for dir in "$HOME/.zen" "$HOME/.mozilla"; do
  mkdir -p "$dir/native-messaging-hosts"
  cat > "$dir/native-messaging-hosts/com.mio.assistant.json" <<JSON
{
  "name": "com.mio.assistant",
  "description": "Mio desktop assistant bridge",
  "path": "$ROOT/bin/mio-browser-host",
  "type": "stdio",
  "allowed_extensions": ["$ADDON_ID"]
}
JSON
  echo "registered for $dir"
done
XPI="${XDG_CACHE_HOME:-$HOME/.cache}/assistant/mio.xpi"
python3 -I "$ROOT/scripts/build_xpi.py" "$ROOT/browser/extension" "$ADDON_ID" "$XPI"
echo
echo "Zen: in about:config set xpinstall.signatures.required to false, then in about:addons use the gear menu,"
echo "     Install Add-on From File, and pick $XPI"
echo "Chromium / Chrome: chrome://extensions, Developer mode, Load unpacked, and pick $ROOT/browser/extension"
