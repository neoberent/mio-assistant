#!/usr/bin/env python3
"""Pack the Chromium MV3 extension as an add-on for Firefox-based browsers (Zen), without touching its folder."""
import json
import sys
import zipfile
from pathlib import Path

src, addon_id, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
manifest = json.loads((src / "manifest.json").read_text())
for chromium_only in ("key", "minimum_chrome_version"):
    manifest.pop(chromium_only, None)
manifest["background"] = {"scripts": [manifest["background"]["service_worker"]]}  # they run event pages, not workers
manifest["browser_specific_settings"] = {"gecko": {"id": addon_id, "strict_min_version": "128.0",
                                                   "data_collection_permissions": {"required": ["none"]}}}
out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("manifest.json", json.dumps(manifest, indent=2))
    for f in sorted(src.rglob("*")):
        if f.is_file() and f.name != "manifest.json":
            z.write(f, f.relative_to(src))
