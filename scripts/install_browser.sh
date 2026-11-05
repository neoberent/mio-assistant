#!/usr/bin/env bash
# Registers Mio's native messaging host with Zen and with Chromium / Chrome (per user, no sudo).
# Zen: install browser/extension as an add-on with the id below. Chromium: load browser/extension unpacked.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID="hmjgoilonpblpoamhakkijgoedphnaim"   # fixed by the "key" in browser/extension/manifest.json
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
  "allowed_extensions": ["mio@assistant.local"]
}
JSON
  echo "registered for $dir"
done
echo "Extension folder to load: $ROOT/browser/extension"
