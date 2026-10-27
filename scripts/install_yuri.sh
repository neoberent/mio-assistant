#!/usr/bin/env bash
# Installs Mio's current look, "yuri" by creape (free, https://booth.pm/ja/items/4798949), from the Booth zip.
# Booth downloads need a login, so download live2d_yuri.zip yourself, then: scripts/install_yuri.sh [ZIP]
# Renames the files to Yuri.*, fills the empty EyeBlink/LipSync groups and halves the 8192 px texture.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ZIP="${1:-$HOME/Downloads/live2d_yuri.zip}"
DEST="$ROOT/avatar/web/models/Yuri"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

unzip -q -O cp932 "$ZIP" -d "$tmp"
src="$(dirname "$(find "$tmp" -name '*.model3.json' | head -1)")"
mkdir -p "$DEST/textures" "$DEST/expressions"
cp "$src"/*.moc3 "$DEST/Yuri.moc3"
cp "$src"/*.physics3.json "$DEST/Yuri.physics3.json"
cp "$src"/*.cdi3.json "$DEST/Yuri.cdi3.json"
cp "$src/FaceRed.exp3.json" "$DEST/expressions/blush.exp3.json"
cp "$src/FaceBlue.exp3.json" "$DEST/expressions/pale.exp3.json"
magick "$src"/*.8192/texture_00.png -filter Lanczos -resize 4096x4096 "$DEST/textures/texture_00.png"
cat > "$DEST/Yuri.model3.json" <<'EOF'
{
 "Version": 3,
 "FileReferences": {
  "Moc": "Yuri.moc3",
  "Textures": ["textures/texture_00.png"],
  "Physics": "Yuri.physics3.json",
  "DisplayInfo": "Yuri.cdi3.json",
  "Expressions": [
   {"Name": "blush", "File": "expressions/blush.exp3.json"},
   {"Name": "pale", "File": "expressions/pale.exp3.json"}
  ]
 },
 "Groups": [
  {"Target": "Parameter", "Name": "EyeBlink", "Ids": ["ParamEyeLOpen", "ParamEyeROpen"]},
  {"Target": "Parameter", "Name": "LipSync", "Ids": ["ParamMouthOpenY"]}
 ]
}
EOF
echo "installed to $DEST — set [avatar] model = \"Yuri/Yuri.model3.json\" and restart assistant-avatar"
