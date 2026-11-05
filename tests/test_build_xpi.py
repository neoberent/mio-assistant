"""Installing the extension in Zen: the add-on has to carry the id the native host accepts."""
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _install(home: Path) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")} | {"HOME": str(home)}
    return subprocess.run(["bash", str(ROOT / "scripts/install_browser.sh")], env=env, check=True, capture_output=True,
                          text=True).stdout


def _xpi(home: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(home / ".cache/assistant/mio.xpi")


def test_install_builds_an_addon_firefox_browsers_can_run(tmp_path):
    _install(tmp_path)
    with _xpi(tmp_path) as z:
        m = json.loads(z.read("manifest.json"))
        names = z.namelist()
    assert m["background"] == {"scripts": ["background.js"]}  # they don't run service workers
    assert "key" not in m and "minimum_chrome_version" not in m
    assert {"background.js", "popup.html", "popup.js", "icons/icon-128.png"} <= set(names)


def test_the_native_host_accepts_exactly_the_id_the_addon_has(tmp_path):
    _install(tmp_path)
    with _xpi(tmp_path) as z:
        addon_id = json.loads(z.read("manifest.json"))["browser_specific_settings"]["gecko"]["id"]
    for folder in (".zen", ".mozilla"):
        host = json.loads((tmp_path / folder / "native-messaging-hosts/com.mio.assistant.json").read_text())
        assert host["allowed_extensions"] == [addon_id]


def test_install_says_where_the_addon_is_and_how_to_load_it(tmp_path):
    out = _install(tmp_path)
    assert str(tmp_path / ".cache/assistant/mio.xpi") in out
    assert "xpinstall.signatures.required" in out


def test_building_leaves_the_chromium_source_untouched(tmp_path):
    source = ROOT / "browser/extension/manifest.json"
    before = source.read_text()
    _install(tmp_path)
    assert source.read_text() == before
    assert re.search(r'"service_worker"', before)
