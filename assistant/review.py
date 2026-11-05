"""Her own report card: every finished request is logged (locally, 30 days), and once a night she goes over the
day's requests and writes down the ones that went badly — slow, lost, wrong tool result, asked back, sent to Claude,
or that the user had to repeat or correct right after. That list is the to-do list for the next "smarter" round.

No model calls: it's plain rules over the log, so it costs nothing and runs any time.
Read it with `assistant review [days]`; files land in ~/.local/state/assistant/reviews/.
Stdlib only (the `assistant` CLI imports it).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
import logging
import os
import re

from .config import STATE_DIR

log = logging.getLogger("review")

REQUESTS = STATE_DIR / "requests.jsonl"
REVIEWS = STATE_DIR / "reviews"
KEEP_DAYS = 30
SLOW_S = 8.0
LOST = re.compile(r"got a bit lost|say it again|something went wrong|couldn'?t reach claude|can'?t do that one|"
                  r"what should i do exactly|i'?m not sure (what|how)|no usable results|web search failed", re.I)
# "Failed system services: none" is system_health's normal first line, not a failure
TOOL_FAILED = re.compile(r"^\W*(error|failed(?![^\n:]*:\s*none\b)|couldn'?t|could not|can'?t|not found|no such|"
                         r"unknown|invalid|nothing (?:from|to|called)|the \w+ is empty|no \w+ (is )?open)", re.I)
# a greeting said twice isn't a repeat because she got it wrong, and "stop"/"shut up" are meant to get silence
GREETING = re.compile(r"^\W*(hi|hello|hey|yo|heya|what'?s up|sup|how are you|how'?s it going|good (morning|"
                      r"evening|night))\b", re.I)
WANTS_SILENCE = re.compile(r"^\W*(stop|shut up|be quiet|quiet)\W*$", re.I)
# said right after a reply: she got the last one wrong
CORRECTED = re.compile(r"^\W*(no+\b|nope|wrong|not that|that'?s (wrong|not)|i said|i meant|i didn'?t (say|mean|ask)|"
                       r"you (misheard|got it wrong)|stop)", re.I)


def log_request(text: str, reply: str, src: str, secs: float, voice: bool, trace: list[dict]):
    """One line per finished request. Never raises. Test runs of the daemon set MIO_TEST=1 so they stay out."""
    if os.environ.get("MIO_TEST"):
        return
    try:
        REQUESTS.parent.mkdir(parents=True, exist_ok=True)
        row = {"t": dt.datetime.now().isoformat(timespec="seconds"), "text": text[:300], "reply": (reply or "")[:300],
               "src": src, "secs": round(secs, 1), "voice": voice,
               "tools": [{"name": c.get("name"), "args": {k: str(v)[:60] for k, v in (c.get("args") or {}).items()},
                          "result": str(c.get("result", ""))[:160]} for c in trace[:8]]}
        with REQUESTS.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - logging must never break a request
        log.debug("request log failed: %s", e)


def _rows() -> list[dict]:
    try:
        lines = REQUESTS.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _prune(rows: list[dict]):
    cut = (dt.datetime.now() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    keep = [r for r in rows if r["t"] >= cut]
    if len(keep) < len(rows):
        REQUESTS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))


def problems(rows: list[dict]) -> list[tuple[dict, list[str]]]:
    """(request, [what went wrong]) for the ones worth a look, in order."""
    out = []
    for i, r in enumerate(rows):
        why = []
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        close = nxt and (dt.datetime.fromisoformat(nxt["t"]) - dt.datetime.fromisoformat(r["t"])).total_seconds() < 120
        if r.get("src") == "cancelled":
            continue  # they stopped it on purpose
        if not r.get("reply") and not WANTS_SILENCE.match(r["text"]):
            why.append("said nothing")
        elif LOST.search(r["reply"]):
            why.append("got lost")
        if r.get("src") == "claude":
            why.append("needed Claude")
        if r.get("src") == "ask":
            why.append("had to ask back")
        if r.get("secs", 0) >= SLOW_S:
            why.append(f"slow ({r['secs']:.0f} s)")
        if failed := [c["name"] for c in r.get("tools", []) if TOOL_FAILED.match(c.get("result", ""))]:
            why.append("tool failed: " + ", ".join(dict.fromkeys(failed)))
        if close and CORRECTED.search(nxt["text"]):
            why.append(f"you corrected her: “{nxt['text'][:60]}”")
        elif close and r.get("src") != "ask" and not GREETING.match(r["text"]) and difflib.SequenceMatcher(
                None, r["text"].lower(), nxt["text"].lower()).ratio() > 0.75 and len(r["text"]) > 8:
            why.append("you had to say it again")
        if why:
            out.append((r, why))
    return out


def report(days: int = 1, until: dt.date | None = None) -> str:
    """Markdown report for the `days` days ending with `until` (default today)."""
    until = until or dt.date.today()
    since = until - dt.timedelta(days=days - 1)
    rows = [r for r in _rows() if since.isoformat() <= r["t"][:10] <= until.isoformat()]
    span = until.isoformat() if days == 1 else f"{since.isoformat()} – {until.isoformat()}"
    if not rows:
        return f"# Mio's review, {span}\n\nNo requests.\n"
    bad = problems(rows)
    by_src: dict[str, int] = {}
    for r in rows:
        by_src[r.get("src", "?")] = by_src.get(r.get("src", "?"), 0) + 1
    avg = sum(r.get("secs", 0) for r in rows) / len(rows)
    good = len(rows) - len(bad)
    lines = [f"# Mio's review, {span}", "",
             f"{len(rows)} requests, {good} went smoothly ({100 * good // len(rows)}%), avg {avg:.1f} s. "
             "Answered by: " + ", ".join(f"{k} {v}" for k, v in sorted(by_src.items(), key=lambda kv: -kv[1])) + ".",
             ""]
    if bad:
        lines += ["## Worth a look", ""]
        for r, why in bad:
            tools = ", ".join(c["name"] for c in r.get("tools", [])) or "no tools"
            lines.append(f"- {r['t'][11:16]} “{r['text'][:90]}” → {'; '.join(why)}  \n"
                         f"  she said: “{r.get('reply', '')[:120]}” ({r.get('src')}, {tools})")
        lines.append("")
        kinds: dict[str, int] = {}
        for _, why in bad:
            for w in why:
                k = re.sub(r"[:(].*", "", w).strip()
                kinds[k] = kinds.get(k, 0) + 1
        lines.append("Most common: " + ", ".join(f"{k} ×{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    slow = sorted(rows, key=lambda r: -r.get("secs", 0))[:3]
    lines += ["", "Slowest: " + "; ".join(f"“{r['text'][:50]}” {r.get('secs', 0):.1f} s" for r in slow), ""]
    return "\n".join(lines)


def write(day: dt.date) -> str:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    path = REVIEWS / f"{day.isoformat()}.md"
    path.write_text(report(1, day))
    return str(path)


async def nightly():
    """From 4 am on, review yesterday once (and prune the log). Checks every 30 minutes; ~ms of work."""
    while True:
        try:
            now = dt.datetime.now()
            day = now.date() - dt.timedelta(days=1)
            if now.hour >= 4 and not (REVIEWS / f"{day.isoformat()}.md").exists():
                rows = _rows()
                _prune(rows)
                if any(r["t"][:10] == day.isoformat() for r in rows):
                    path = write(day)
                    log.info("reviewed %s: %s", day, path)
        except Exception as e:  # noqa: BLE001
            log.warning("nightly review failed: %s", e)
        await asyncio.sleep(1800)
