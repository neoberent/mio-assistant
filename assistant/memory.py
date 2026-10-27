"""Short conversation memory + a small long-term fact list, persisted as JSON."""
from __future__ import annotations

import datetime as dt
import json
import re

from .config import MEMORY_FILE, USAGE_FILE

MAX_FACTS = 30


class Memory:
    """Conversation memory for one *session* (while Mio is on screen) + persistent facts.

    A session holds the turns and compact notes of what tools returned (incl. links), so
    follow-ups like "open their GitHub" work. It is wiped when she hides, which keeps every
    request small and cheap. Only `remember`-ed facts survive between sessions.
    """

    NOTE_CHARS = 350
    CONTEXT_CHARS = 6000

    def __init__(self, keep_turns: int):
        self.keep = keep_turns
        self.turns: list[dict] = []   # {"user", "assistant", "notes": [str]}
        self.notes: list[str] = []    # tool notes of the turn in progress
        self.facts: list[str] = []
        self._load()

    def _load(self):
        try:
            self.facts = json.loads(MEMORY_FILE.read_text()).get("facts", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        MEMORY_FILE.write_text(json.dumps({"facts": self.facts}, indent=1))

    def note(self, tool: str, args: dict, result: str):
        """Keep a short trace of a tool result; links are always kept in full."""
        links = list(dict.fromkeys(re.findall(r"https?://[^\s)\]\"'<>]+", result)))[:8]
        short = re.sub(r"\s+", " ", result)[: self.NOTE_CHARS]
        arg = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
        self.notes.append(f"{tool}({arg}) → {short}" + (f" | links: {' '.join(links)}" if links else ""))

    def add(self, user: str, assistant: str):
        self.turns.append({"user": user, "assistant": assistant, "notes": self.notes})
        self.notes = []
        self.turns = self.turns[-self.keep:]

    def tools_used(self) -> list[str]:
        """Tool names used in the last two turns (offered again for follow-ups)."""
        names = [n.split("(", 1)[0] for t in self.turns[-2:] for n in t["notes"]]
        return list(dict.fromkeys(reversed(names)))

    def end_session(self):
        self.turns, self.notes = [], []

    def active(self) -> bool:
        return bool(self.turns)

    def remember(self, fact: str):
        fact = fact.strip()
        if fact and fact not in self.facts:
            self.facts = (self.facts + [fact])[-MAX_FACTS:]
            self.save()

    def clear(self):
        self.end_session()

    def messages(self) -> list[dict]:
        """This session as chat turns (small models follow real turns far better than a text blob)."""
        out = []
        for t in self.turns:
            out.append({"role": "user", "content": t["user"]})
            out.append({"role": "assistant", "content": t["assistant"]})
        while out and sum(len(m["content"]) for m in out) > self.CONTEXT_CHARS:
            out = out[2:]
        return out

    def facts_line(self) -> str:
        """Persistent facts + what tools found earlier this session (links etc.), for the system prompt."""
        lines = ["Known about the user: " + "; ".join(self.facts)] if self.facts else []
        notes = [n for t in self.turns for n in t["notes"]]
        while notes and sum(map(len, notes)) > self.CONTEXT_CHARS // 2:
            notes.pop(0)
        if notes:
            lines.append("Found earlier in this conversation (use these for follow-ups):\n- " + "\n- ".join(notes))
        return "\n".join(lines)

    def context_block(self) -> str:
        """Facts + this session's turns with tool notes, newest kept if it gets too long."""
        blocks = []
        for t in self.turns:
            b = f"User: {t['user']}"
            if t["notes"]:
                b += "\n(tools: " + " ; ".join(t["notes"]) + ")"
            blocks.append(b + f"\nYou: {t['assistant']}")
        while blocks and sum(map(len, blocks)) > self.CONTEXT_CHARS:
            blocks.pop(0)
        head = ["Known about the user: " + "; ".join(self.facts)] if self.facts else []
        if blocks:
            head.append("Conversation so far (this session):")
        return "\n".join(head + blocks)


class Usage:
    """Counts Claude requests per day so a hard cap can be enforced."""

    def __init__(self, daily_limit: int):
        self.limit = daily_limit

    def _read(self) -> dict:
        try:
            return json.loads(USAGE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def today(self) -> int:
        return self._read().get(dt.date.today().isoformat(), 0)

    def cost_today(self) -> float:
        return self._read().get(f"{dt.date.today().isoformat()}:usd", 0.0)

    def month(self) -> tuple[int, float]:
        """(requests, usd) for the current calendar month."""
        prefix = dt.date.today().strftime("%Y-%m")
        d = self._read()
        reqs = sum(v for k, v in d.items() if k.startswith(prefix) and not k.endswith(":usd"))
        usd = sum(v for k, v in d.items() if k.startswith(prefix) and k.endswith(":usd"))
        return reqs, usd

    def summary(self) -> str:
        n, usd = self.today(), self.cost_today()
        mn, musd = self.month()
        return (f"Today I used Claude {n} {'time' if n == 1 else 'times'} out of {self.limit}, "
                f"for {usd * 100:.2f} cents. This month: {mn} requests, {musd * 100:.1f} cents.")

    def allowed(self) -> bool:
        return self.today() < self.limit

    def bump(self, cost_usd: float = 0.0):
        d = self._read()
        key = dt.date.today().isoformat()
        d[key] = d.get(key, 0) + 1
        d[f"{key}:usd"] = round(d.get(f"{key}:usd", 0.0) + cost_usd, 4)
        d = {k: v for k, v in d.items() if k[:10] >= (dt.date.today() - dt.timedelta(days=40)).isoformat()}
        USAGE_FILE.write_text(json.dumps(d, indent=1))
