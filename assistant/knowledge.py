"""Tiny local search over the user's own configs and notes (BM25, no model, no network).

Answers things like "what's my screenshot shortcut?" or "how do I change your voice?".
Lines that look like secrets are never indexed.
"""
from __future__ import annotations

import glob
import math
import os
import re
from collections import Counter
from pathlib import Path

SECRET = re.compile(r"(password|passwd|token|secret|api[_-]?key|ANTHROPIC|private)", re.I)
WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "to", "of", "and", "or", "is", "in", "on", "for", "my", "me", "i", "it", "how", "do",
        "what", "whats", "s", "you", "your", "with", "this", "that", "can", "be", "are", "by", "at", "as"}


def _tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


class Knowledge:
    def __init__(self, patterns: list[str]):
        self.patterns = patterns
        self._sig: tuple = ()
        self.chunks: list[tuple[str, str]] = []  # (path, text)
        self.tf: list[Counter] = []
        self.df: Counter = Counter()

    def _files(self) -> list[Path]:
        out = []
        for p in self.patterns:
            out += [Path(f) for f in glob.glob(os.path.expanduser(p), recursive=True) if Path(f).is_file()]
        return sorted(set(out))

    def _refresh(self):
        files = self._files()
        sig = tuple((str(f), f.stat().st_mtime) for f in files)
        if sig == self._sig:
            return
        self._sig, self.chunks = sig, []
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            lines = [l for l in text.splitlines() if not SECRET.search(l)]
            # chunk on blank lines / headings, ~700 chars, so a hit carries its surrounding context
            buf: list[str] = []
            for line in lines + [""]:
                if (not line.strip() or line.startswith("#")) and sum(map(len, buf)) > 350 or sum(map(len, buf)) > 700:
                    if any(l.strip() for l in buf):
                        self.chunks.append((str(f).replace(str(Path.home()), "~"), "\n".join(buf).strip()))
                    buf = []
                buf.append(line)
        self.tf = [Counter(_tokens(f"{p} {t}")) for p, t in self.chunks]
        self.df = Counter(w for tf in self.tf for w in tf)

    def search(self, query: str, k: int = 4) -> str:
        self._refresh()
        q = _tokens(query)
        if not q or not self.chunks:
            return "nothing found in your configs/notes"
        n = len(self.chunks)
        avg = sum(sum(tf.values()) for tf in self.tf) / n
        scores = []
        for i, tf in enumerate(self.tf):
            length = sum(tf.values())
            s = 0.0
            for w in q:
                if w in tf:
                    idf = math.log(1 + (n - self.df[w] + 0.5) / (self.df[w] + 0.5))
                    s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * length / avg))
            if self.chunks[i][0].startswith("~/.config"):
                s *= 1.3  # the user's own config beats the manual for "my …" questions
            scores.append((s, i))
        best = [i for s, i in sorted(scores, reverse=True)[:k] if s > 0]
        if not best:
            return "nothing found in your configs/notes"
        return "\n\n".join(f"[{self.chunks[i][0]}]\n{self.chunks[i][1][:900]}" for i in best)
