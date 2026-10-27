"""Timers and reminders. Kept in a small JSON file so they survive restarts and reboots.

The daemon runs `Timers.run()`, which sleeps until the next one is due and then calls
`on_due(item, late)`; `late` is set for ones that came due while Mio wasn't running.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import uuid
from pathlib import Path

log = logging.getLogger("timers")

MISSED_GRACE = dt.timedelta(hours=12)  # older missed reminders are dropped silently

WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
         "fifty": 50, "sixty": 60, "ninety": 90}
UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
         "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|[smh])\b")


def _digits(text: str) -> str:
    """'twenty five minutes' → '25 minutes', 'half an hour' → '30 minutes'."""
    t = text.lower()
    t = re.sub(r"\b(an?|one) hour and a half\b", "90 minutes", t)
    t = re.sub(r"\bhalf an? hour\b", "30 minutes", t)
    t = re.sub(r"\ba quarter of an hour\b|\bquarter hour\b", "15 minutes", t)
    t = re.sub(r"\b(\w+) and a half (hours?|minutes?)\b",
               lambda m: f"{WORDS.get(m[1], m[1])}.5 {m[2]}" if str(WORDS.get(m[1], m[1])).isdigit() else m[0], t)
    words = "|".join(sorted(WORDS, key=len, reverse=True))
    t = re.sub(rf"\b(twenty|thirty|forty|fifty)[ -](one|two|three|four|five|six|seven|eight|nine)\b",
               lambda m: str(WORDS[m[1]] + WORDS[m[2]]), t)
    return re.sub(rf"\b({words})\b(?=\s+(?:seconds?|minutes?|hours?|mins?|secs?)\b)", lambda m: str(WORDS[m[1]]), t)


def parse_duration(text: str) -> int | None:
    """Seconds in '5 minutes', '1 hour and 20 minutes', 'an hour', '90 sec'; None if there is none."""
    found = DURATION.findall(_digits(text))
    if not found:
        return None
    return int(sum(float(n) * UNITS[u] for n, u in found)) or None


def parse_clock(text: str, now: dt.datetime) -> dt.datetime | None:
    """'at 6 pm', 'at 18:30', 'tomorrow at 9', 'tonight at 8', 'at noon' → the next such moment."""
    t = text.lower().replace("a.m.", "am").replace("p.m.", "pm")
    day = now.date() + dt.timedelta(days=1) if re.search(r"\btomorrow\b", t) else now.date()
    if re.search(r"\bat noon\b|\bat midday\b", t):
        hour, minute, ampm = 12, 0, "pm"
    elif re.search(r"\bat midnight\b", t):
        hour, minute, ampm = 0, 0, "am"
    elif m := re.search(r"\bat (\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|o'?clock)?(?!\s*(?:seconds?|minutes?|hours?|mins?))\b", t):
        hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3] or ""
    else:
        return None
    if hour > 23 or minute > 59:
        return None
    evening = re.search(r"\b(tonight|this evening|in the evening|this afternoon|in the afternoon)\b", t)
    if ampm == "pm" and hour < 12 or (evening and ampm not in ("am", "pm") and hour < 12):
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    when = dt.datetime.combine(day, dt.time(hour, minute))
    if when <= now and day == now.date():
        if ampm not in ("am", "pm") and hour < 12 and when + dt.timedelta(hours=12) > now:
            when += dt.timedelta(hours=12)  # "at 6" said at 14:00 means 18:00
        else:
            when += dt.timedelta(days=1)
    return when


def say_duration(seconds: float, adjective: bool = False, exact: bool = True) -> str:
    """'5 minutes'; adjective=True gives '5 minute' (as in 'a 5 minute timer'); exact=False drops seconds past 2 min."""
    s = max(0, round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if not exact and s >= 120:
        m, sec = m + (sec >= 30), 0
    plural = lambda n: "s" if n != 1 and not adjective else ""
    parts = [f"{h} hour{plural(h)}" if h else "", f"{m} minute{plural(m)}" if m else "",
             f"{sec} second{plural(sec)}" if sec and not h else ""]
    parts = [p for p in parts if p]
    return " and ".join(parts) or "0 seconds"


def you(text: str) -> str:
    """'call my mom' → 'call your mom', for repeating a reminder back."""
    swap = {"my": "your", "me": "you", "i": "you", "myself": "yourself", "i'm": "you're", "mine": "yours"}
    return re.sub(r"\b(my|me|i|myself|i'm|mine)\b", lambda m: swap[m[1]], text)


def say_clock(when: dt.datetime, now: dt.datetime) -> str:
    day = {0: "", 1: "tomorrow "}.get((when.date() - now.date()).days, when.strftime("%A "))
    return f"{day}at {when.strftime('%-H:%M')}"


class Timers:
    def __init__(self, path: Path, on_due):
        self.path = path
        self.on_due = on_due  # async callable(item: dict, late: bool)
        self.items: list[dict] = self._load()
        self._changed = asyncio.Event()

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self):
        self.path.write_text(json.dumps(self.items, indent=1))
        self._changed.set()

    def add(self, due: dt.datetime, label: str = "", kind: str = "timer", seconds: int | None = None) -> dict:
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "due": due.isoformat(timespec="seconds"),
                "label": label.strip(), "seconds": seconds}
        self.items.append(item)
        self.items.sort(key=lambda i: i["due"])
        self._save()
        log.info("added %s %s due %s", kind, label or "", item["due"])
        return item

    def cancel(self, query: str = "", kind: str = "") -> list[dict]:
        """Removes the ones whose label matches `query`; 'all' removes everything (of `kind`, if given),
        '' the next one due (of `kind`)."""
        q = query.lower().strip()
        pool = [i for i in self.items if not kind or i["kind"] == kind]
        if q in ("all", "every", "everything"):
            gone = pool
        elif q:
            gone = [i for i in pool if q in i["label"].lower() or (i["seconds"] and parse_duration(q) == i["seconds"])]
        else:
            gone = pool[:1]
        self.items = [i for i in self.items if i not in gone]
        self._save()
        return gone

    def describe(self, item: dict, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now()
        due = dt.datetime.fromisoformat(item["due"])
        label = you(item["label"])
        if item["kind"] == "timer":
            name = f"a {say_duration(item['seconds'], adjective=True)} timer" if item.get("seconds") else "a timer"
            name += f" for the {label}" if label else ""
            return f"{name} with {say_duration((due - now).total_seconds(), exact=False)} left"
        if not label:
            return f"an alarm {say_clock(due, now)}"
        return f"a reminder {say_clock(due, now)} to {label}"

    def summary(self) -> str:
        if not self.items:
            return "You don't have any timers or reminders."
        now = dt.datetime.now()
        lines = [self.describe(i, now) for i in self.items]
        if len(lines) == 1:
            return f"You have {lines[0]}."
        return f"You have {len(lines)}: " + ", ".join(lines[:-1]) + f", and {lines[-1]}."

    async def run(self):
        while True:
            self._changed.clear()
            now = dt.datetime.now()
            due = [i for i in self.items if dt.datetime.fromisoformat(i["due"]) <= now]
            if due:
                self.items = [i for i in self.items if i not in due]
                self._save()
                for item in due:
                    late = now - dt.datetime.fromisoformat(item["due"])
                    if late > MISSED_GRACE:
                        log.info("dropping stale %s %s", item["kind"], item["label"])
                        continue
                    log.info("%s due: %s%s", item["kind"], item["label"], " (missed while off)" if late > dt.timedelta(minutes=2) else "")
                    try:
                        await self.on_due(item, late > dt.timedelta(minutes=2))
                    except Exception:
                        log.exception("announcing %s failed", item["kind"])
                continue
            wait = (dt.datetime.fromisoformat(self.items[0]["due"]) - now).total_seconds() if self.items else 3600
            try:  # wake early when one is added or cancelled; cap so a suspend/resume can't oversleep
                await asyncio.wait_for(self._changed.wait(), min(wait, 30))
            except asyncio.TimeoutError:
                pass
