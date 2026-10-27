"""Game launching: custom launchers from the config (e.g. Roblox via Froststrap → Sober) and installed Steam games."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

STEAM_ROOTS = [Path.home() / ".local/share/Steam", Path.home() / ".steam/steam",
               Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
NOT_GAMES = re.compile(r"proton|steam linux runtime|steamworks|redistributable|steamvr|soundtrack", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def steam_games() -> dict[str, str]:
    """name → appid for every installed Steam game."""
    libs: set[Path] = set()
    for root in STEAM_ROOTS:
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            libs.add(root.resolve())
            libs |= {Path(p).resolve() for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))}
    games = {}
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            text = acf.read_text(errors="replace")
            name, appid = re.search(r'"name"\s+"([^"]+)"', text), re.search(r'"appid"\s+"(\d+)"', text)
            if name and appid and not NOT_GAMES.search(name.group(1)):
                games[name.group(1)] = appid.group(1)
    return games


def find_game(query: str, launchers: dict[str, list[str]]) -> tuple[str, list[str]] | None:
    """Returns (display name, argv) or None."""
    q = _norm(query)
    for name, argv in launchers.items():
        if _norm(name) == q or (len(q) >= 4 and q in _norm(name)):
            return name.title(), list(argv)
    games = steam_games()
    by_norm = {_norm(n): n for n in games}
    hit = by_norm.get(q) or next((n for k, n in by_norm.items() if len(q) >= 4 and q in k), None)
    if not hit:
        close = difflib.get_close_matches(q, list(by_norm), n=1, cutoff=0.7)
        hit = by_norm[close[0]] if close else None
    if hit:
        return hit, ["steam", f"steam://rungameid/{games[hit]}"]
    return None
