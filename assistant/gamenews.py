"""Game patch notes and Steam prices from free public endpoints (no API key, no LLM).

Steam: ISteamNews for patch notes, store search to find a game by name, IStoreBrowseService for
prices and discounts, IWishlistService for the user's wishlist (it must be public). Roblox has no
public patch notes, so "did Roblox update" reads Roblox's deploy history plus Sober's flatpak.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import re

import httpx

from .games import STEAM_ROOTS, steam_games

UA = {"User-Agent": "desktop-assistant/0.1 (personal use)"}
COUNTRY = "DE"  # prices in euros
ROBLOX = re.compile(r"\b(roblox|sober|froststrap)\b", re.I)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=12, follow_redirects=True, headers=UA)


def _day(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    ago = (dt.date.today() - d.date()).days
    rel = "today" if ago == 0 else "yesterday" if ago == 1 else f"{ago} days ago"
    return f"{d.strftime('%B %-d')} ({rel})"


def _strip(text: str) -> str:
    """Steam announcements are BBCode or HTML; keep the words and list structure."""
    t = re.sub(r"\[/\*\]", "", text.replace("\\[", "[").replace("\\]", "]"))
    t = re.sub(r"\[\*\]|<li>", "\n- ", t)
    t = re.sub(r"\[/?[a-z0-9]+(=[^\]]*)?\]|<[^>]+>|\{STEAM_CLAN_IMAGE\}\S*", " ", t)
    t = html.unescape(t)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def steam_id() -> str | None:
    """SteamID64 of the account last logged in on this PC."""
    for root in STEAM_ROOTS:
        f = root / "config/loginusers.vdf"
        if f.exists():
            text = f.read_text(errors="replace")
            users = re.findall(r'"(\d{17})"\s*\{([^}]*)\}', text)
            recent = [u for u, body in users if re.search(r'"MostRecent"\s+"1"', body)]
            return (recent or [u for u, _ in users] or [None])[0]
    return None


async def find_app(c: httpx.AsyncClient, name: str) -> tuple[int, str] | None:
    """(appid, title): installed games first, then the Steam store search."""
    from .games import find_game

    hit = find_game(name, {})
    if hit and hit[1][0] == "steam":
        return int(hit[1][1].rsplit("/", 1)[1]), hit[0]
    r = await c.get("https://store.steampowered.com/api/storesearch/",
                    params={"term": name, "cc": COUNTRY, "l": "english"})
    items = [i for i in r.json().get("items", []) if i.get("type") == "app"]
    return (items[0]["id"], items[0]["name"]) if items else None


# ── patch notes ─────────────────────────────────────────────────────────────
async def roblox_update() -> str:
    async with _client() as c:
        r = await c.get("https://setup.rbxcdn.com/DeployHistory.txt", headers={"Range": "bytes=-30000"})
    lines = [l for l in r.text.splitlines() if "WindowsPlayer" in l]
    out = []
    if lines and (m := re.search(r" at (\d+/\d+/\d{4} \d+:\d+:\d+ [AP]M).*?git hash: ([\d.]+)", lines[-1])):
        when = dt.datetime.strptime(m[1], "%m/%d/%Y %I:%M:%S %p")
        out.append(f"Roblox last updated on {_day(when.timestamp())}, version {m[2]}. Roblox doesn't publish "
                   "patch notes for client updates.")
    proc = await asyncio.create_subprocess_exec("flatpak", "remote-ls", "--updates", "--columns=application",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    pending, _ = await proc.communicate()
    if b"org.vinegarhq.Sober" in pending:
        out.append("There's a Sober update waiting (flatpak update).")
    elif proc.returncode == 0:
        out.append("Sober is up to date.")
    return " ".join(out) or "I couldn't get Roblox's update history."


async def patch_notes(game: str, count: int = 2) -> str:
    """Latest official announcements (patch notes first) for a Steam game, or Roblox's update status."""
    if ROBLOX.search(game):
        return await roblox_update()
    async with _client() as c:
        app = await find_app(c, game)
        if not app:
            return f"I couldn't find a game called {game} on Steam."
        appid, title = app
        r = await c.get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                        params={"appid": appid, "count": 15, "maxlength": 0, "feeds": "steam_community_announcements"})
    news = r.json().get("appnews", {}).get("newsitems", [])
    if not news:
        return f"{title} has no official announcements on Steam."
    patches = [n for n in news if "patchnotes" in (n.get("tags") or [])
               or re.search(r"\b(patch|update|hotfix|fix(es)?)\b", n["title"], re.I)]
    picked = (patches or news)[:max(1, min(count, 4))]
    out = [f"{title} — latest {'updates' if patches else 'announcements'} (newest first):"]
    for n in picked:
        body = _strip(n.get("contents", ""))
        out.append(f"* {n['title']} — {_day(n['date'])}\n{body[:900]}{'…' if len(body) > 900 else ''}\n{n['url']}")
    return "\n".join(out)


# ── prices ──────────────────────────────────────────────────────────────────
async def _prices(c: httpx.AsyncClient, appids: list[int]) -> list[dict]:
    items = []
    for i in range(0, len(appids), 50):
        req = {"ids": [{"appid": a} for a in appids[i:i + 50]],
               "context": {"language": "english", "country_code": COUNTRY},
               "data_request": {"include_all_purchase_options": True}}
        r = await c.get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                        params={"input_json": json.dumps(req)})
        items += r.json().get("response", {}).get("store_items", [])
    return items


def _price_line(it: dict) -> str:
    b = it.get("best_purchase_option") or {}
    if it.get("is_free"):
        return f"{it['name']} is free to play"
    if not b.get("formatted_final_price"):
        return f"{it['name']} isn't for sale on Steam right now"
    if b.get("discount_pct"):
        end = (b.get("active_discounts") or [{}])[0].get("discount_end_date")
        until = f", until {dt.datetime.fromtimestamp(end).strftime('%B %-d')}" if end else ""
        return (f"{it['name']} is {b['discount_pct']}% off: {b['formatted_final_price']} instead of "
                f"{b['formatted_original_price']}{until}")
    return f"{it['name']} costs {b['formatted_final_price']}, not on sale"


async def price(game: str) -> str:
    async with _client() as c:
        app = await find_app(c, game)
        if not app:
            return f"I couldn't find a game called {game} on Steam."
        items = await _prices(c, [app[0]])
    return (_price_line(items[0]) + ".") if items else f"I couldn't get the price of {app[1]}."


async def wishlist_sales() -> str:
    sid = steam_id()
    if not sid:
        return "I couldn't find your Steam account on this PC."
    async with _client() as c:
        r = await c.get("https://api.steampowered.com/IWishlistService/GetWishlist/v1/", params={"steamid": sid})
        appids = [i["appid"] for i in r.json().get("response", {}).get("items", [])]
        if not appids:
            return ("Your Steam wishlist looks empty, or it's private. To let me check it, set Game details to "
                    "Public in Steam's privacy settings.")
        items = await _prices(c, appids)
    sale = sorted((it for it in items if (it.get("best_purchase_option") or {}).get("discount_pct")),
                  key=lambda it: -it["best_purchase_option"]["discount_pct"])
    if not sale:
        return f"Nothing on your wishlist is on sale right now ({len(appids)} games checked)."
    lines = [_price_line(it) for it in sale[:6]]
    more = f" And {len(sale) - 6} more." if len(sale) > 6 else ""
    return f"{len(sale)} of your {len(appids)} wishlist games are on sale. " + ". ".join(lines) + "." + more


async def top_deals(count: int = 5) -> str:
    async with _client() as c:
        r = await c.get("https://store.steampowered.com/api/featuredcategories/",
                        params={"cc": COUNTRY.lower(), "l": "english"})
    items = r.json().get("specials", {}).get("items", [])[:max(1, min(count, 10))]
    if not items:
        return "I couldn't load Steam's specials."
    eur = lambda cents: f"{cents / 100:.2f}€".replace(".", ",")
    return "Top Steam deals right now: " + "; ".join(
        f"{i['name']} {i['discount_percent']}% off, {eur(i['final_price'])}" for i in items) + "."
