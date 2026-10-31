"""Short conversation memory + a small long-term fact list, persisted as JSON."""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re

from .config import MEMORY_FILE, USAGE_FILE

MAX_FACTS = 200
FACT_CHARS = 1200  # facts put into one request; the rest wait until a request is about them
SECRET = re.compile(r"password|passwort|token|secret|api.?key|pin code|\bpin\b|credit card|iban", re.I)
STOP = set("the a an and or of to in on at for with is are was be my me i i'm im you your it that this "
           "what who do does did have has like likes".split())


def _ids(text: str) -> set[str]:
    """Names and numbers in a fact (what makes two similar-looking facts different)."""
    return {w.lower() for w in re.findall(r"\b(?:[A-Z][\w'-]+|\d+\w*)", text)} - {"the", "user", "user's"}


def spoken(fact: str) -> str:
    """'The user plays Roblox with Chill' → 'you play Roblox with Chill' (facts are kept third-person)."""
    f = re.sub(r"\bthe user's\b", "your", fact, flags=re.I)
    f = re.sub(r"\bthe user (is|was|has)\b", lambda m: "you " + {"is": "are", "was": "were", "has": "have"}[
        m[1].lower()], f, flags=re.I)
    def base(m: re.Match) -> str:
        v = m[1]
        v = v[:-3] + "y" if v.endswith("ies") else v[:-2] if re.search(r"(ch|sh|ss|x|z|o)es$", v) else v[:-1]
        return "you " + v
    f = re.sub(r"\bthe user (\w+s)\b", base, f, flags=re.I)
    f = re.sub(r"\(said on ([^)]+)\)", r"(you told me on \1)", f)
    return re.sub(r"\bthe user\b", "you", f, flags=re.I)


def _keys(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1}


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

    def remember(self, fact: str) -> bool:
        """Keep a fact; a near-copy of an old one replaces it (newer wording wins). Secrets are never kept."""
        fact = re.sub(r"\s+", " ", fact).strip().rstrip(".")
        if not fact or SECRET.search(fact) or len(fact) > 200:
            return False
        low = fact.lower()
        ids = _ids(fact)  # "Alex is the user's friend" ≠ "Chill is the user's friend": names/numbers must match
        same = [f for f in self.facts if _ids(f) == ids
                and difflib.SequenceMatcher(None, f.lower(), low).ratio() > 0.85]
        if same and fact in self.facts:
            return False
        self.facts = [f for f in self.facts if f not in same] + [fact]
        self.facts = self.facts[-MAX_FACTS:]
        self.save()
        return True

    def forget(self, about: str) -> list[str]:
        """Drop the facts that match `about` best ("forget that I like lofi") → what was dropped."""
        want = _keys(about)
        if not want:
            return []
        scored = [(len(want & _keys(f)) / len(want), f) for f in self.facts]
        best = max((sc for sc, _ in scored), default=0)
        if best < 0.5:
            return []
        gone = [f for sc, f in scored if sc == best]
        self.facts = [f for f in self.facts if f not in gone]
        self.save()
        return gone

    def relevant(self, query: str) -> list[str]:
        """The facts for this request: all of them while they fit, else the ones sharing words with it,
        then the newest."""
        if sum(len(f) + 2 for f in self.facts) <= FACT_CHARS:
            return list(self.facts)
        want = _keys(query)
        order = sorted(range(len(self.facts)), key=lambda i: (len(want & _keys(self.facts[i])), i), reverse=True)
        out, size = [], 0
        for i in order:
            if size + len(self.facts[i]) > FACT_CHARS:
                break
            out.append(i)
            size += len(self.facts[i]) + 2
        return [self.facts[i] for i in sorted(out)]

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

    def facts_line(self, query: str = "") -> str:
        """Persistent facts + what tools found earlier this session (links etc.), for the system prompt."""
        facts = self.relevant(query)
        lines = ["Known about the user: " + "; ".join(facts)] if facts else []
        notes = [n for t in self.turns for n in t["notes"]]
        while notes and sum(map(len, notes)) > self.CONTEXT_CHARS // 2:
            notes.pop(0)
        if notes:
            lines.append("Found earlier in this conversation (use these for follow-ups):\n- " + "\n- ".join(notes))
        return "\n".join(lines)

    def context_block(self, query: str = "") -> str:
        """Facts + this session's turns with tool notes, newest kept if it gets too long."""
        blocks = []
        for t in self.turns:
            b = f"User: {t['user']}"
            if t["notes"]:
                b += "\n(tools: " + " ; ".join(t["notes"]) + ")"
            blocks.append(b + f"\nYou: {t['assistant']}")
        while blocks and sum(map(len, blocks)) > self.CONTEXT_CHARS:
            blocks.pop(0)
        facts = self.relevant(query)
        head = ["Known about the user: " + "; ".join(facts)] if facts else []
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
