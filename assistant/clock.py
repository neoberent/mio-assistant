"""Local time anywhere, offline (system tzdata). No LLM needed."""
from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path
from zoneinfo import ZoneInfo

TZDIR = Path("/usr/share/zoneinfo")
ALIASES = {"usa": "US", "america": "US", "united states": "US", "uk": "GB", "england": "GB", "britain": "GB",
           "great britain": "GB", "holland": "NL", "the netherlands": "NL", "south korea": "KR", "korea": "KR",
           "russia": "RU", "vietnam": "VN", "iran": "IR", "syria": "SY", "taiwan": "TW", "czechia": "CZ",
           "deutschland": "DE"}


@functools.cache
def _tables() -> tuple[dict[str, str], dict[str, str]]:
    """(country name → first zone, city → zone)."""
    names = {}
    for line in (TZDIR / "iso3166.tab").read_text().splitlines():
        if line and not line.startswith("#"):
            code, name = line.split("\t", 1)
            names[code] = name.strip()
    countries, cities = {}, {}
    for line in (TZDIR / "zone1970.tab").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        codes, _, zone = line.split("\t")[:3]
        for code in codes.split(","):
            countries.setdefault(code, zone)
        cities[zone.rsplit("/", 1)[-1].replace("_", " ").lower()] = zone
    by_name = {names[c].lower(): z for c, z in countries.items() if c in names}
    by_name.update({a: countries[c] for a, c in ALIASES.items() if c in countries})
    by_name.update({c.lower(): z for c, z in countries.items()})  # "de", "us"
    return by_name, cities


def zone_for(place: str) -> str | None:
    p = place.lower().strip(" .?!").removeprefix("the ")
    countries, cities = _tables()
    return countries.get(p) or cities.get(p) or next((z for k, z in cities.items() if p and p in k), None)


def now_line() -> str:
    """One line for system prompts so the models always know the date and time."""
    now = dt.datetime.now().astimezone()
    return f"Now: {now.strftime('%A, %d %B %Y, %H:%M')} ({now.tzname()})."


def describe(place: str | None = None, want_time: bool = True, want_date: bool = False) -> str | None:
    tz = None
    if place:
        zone = zone_for(place)
        if not zone:
            return None
        tz = ZoneInfo(zone)
    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    where = f" in {place.strip().title()}" if place else ""
    parts = []
    if want_time:
        parts.append(f"It's {now.strftime('%-H:%M')}{where}")
    if want_date:
        parts.append(f"{'it' if parts else 'It'}'s {now.strftime('%A, %B %-d')}" + ("" if parts else where))
    return " and ".join(parts) + "."
