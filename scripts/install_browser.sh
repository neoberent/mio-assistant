#!/usr/bin/env bash
# Registers Mio's native messaging host with Brave Origin / Brave / Chromium / Chrome (per user, no sudo).
# Then load browser/extension as an unpacked extension (brave://extensions → Developer mode → Load unpacked).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID="hmjgoilonpblpoamhakkijgoedphnaim"   # fixed by the "key" in browser/extension/manifest.json
for dir in "$HOME/.config/BraveSoftware/Brave-Origin" "$HOME/.config/BraveSoftware/Brave-Browser" \
           "$HOME/.config/chromium" "$HOME/.config/google-chrome"; do
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
echo "Extension folder to load: $ROOT/browser/extension"
