"""Morning briefing: time, weather, updates and *new* Arch/CachyOS news, built without any LLM."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

from .config import STATE_DIR

STATE = STATE_DIR / "briefing.json"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d: dict):
    STATE.write_text(json.dumps(d, indent=1))


def due_today(start_hour: int, end_hour: int) -> bool:
    """True if it's morning and no automatic briefing was given today yet."""
    now = dt.datetime.now()
    return start_hour <= now.hour < end_hour and _state().get("auto_date") != now.date().isoformat()


def mark_done():
    d = _state()
    d["auto_date"] = dt.date.today().isoformat()
    _save(d)


def updates_line(updates: str) -> str:
    """system_updates result → 'You have 44 updates waiting, including linux-cachyos, …' ('' if unreadable)."""
    m = re.search(r"(\d+) repo updates, (\d+) AUR updates", updates)
    if not m:
        return ""
    total = int(m[1]) + int(m[2])
    if not total:
        return "Your system is fully up to date."
    line = f"You have {total} update{'s' if total != 1 else ''} waiting"
    if int(m[2]):
        line += f" ({m[2]} from the AUR)"
    notable = re.search(r"Notable: (.+)", updates)
    if total <= 3:  # few enough to just say which
        names = [n.split()[0] for kind in ("Repo", "AUR") if (m2 := re.search(rf"^{kind}: (.+)", updates, re.M))
                 for n in m2[1].split(";") if n.strip()]
        if names:
            return line + ": " + ", ".join(names) + "."
    if notable:
        names = sorted({n.split()[0] for n in notable[1].split(";") if n.strip()})
        line += ", including " + ", ".join(names[:3])
    return line + "."


async def build(tools, name: str) -> str:
    now = dt.datetime.now()
    hour = now.hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    parts = [f"{greet}! It's {now.strftime('%A, %B %-d')}, {now.strftime('%-H:%M')}."]

    weather, updates, arch, cachy = await asyncio.gather(
        tools.call("weather", {}), tools.call("system_updates", {}),
        tools.call("arch_news", {"source": "arch", "count": 3}),
        tools.call("arch_news", {"source": "cachyos", "count": 2}), return_exceptions=True)

    if isinstance(weather, str) and (m := re.search(r"Today: ([^,]+), (-?\d+)–(-?\d+)°C, rain chance (\d+)%", weather)):
        desc, lo, hi, rain = m.groups()
        line = f"Today looks {desc.strip().lower()}, {lo} to {hi} degrees"
        line += f", with a {rain} percent chance of rain." if int(rain) >= 30 else "."
        parts.append(line)

    if isinstance(updates, str) and (line := updates_line(updates)):
        parts.append(line)

    # only news that wasn't in yesterday's briefing
    st = _state()
    seen = set(st.get("seen_news", []))
    fresh = []
    for src, text in (("Arch", arch), ("CachyOS", cachy)):
        if not isinstance(text, str):
            continue
        for title in re.findall(r"^- [^:]+:\s*(.+?)(?: — |$)", text, re.M):
            if title not in seen:
                fresh.append((src, title))
    if fresh:
        heads = "; ".join(f"{t}" for _, t in fresh[:2])
        parts.append(f"New from {fresh[0][0]}: {heads}.")
        if any("manual intervention" in t.lower() for _, t in fresh):
            parts.append("One of those needs manual steps, so read it before updating.")
    st["seen_news"] = list(seen | {t for _, t in fresh})[-50:]
    _save(st)

    parts.append(f"Have a lovely day! Just say Hey {name} if you need me.")
    return " ".join(parts)
