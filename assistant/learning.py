"""Mio learns from use: names she hears (Lexicon) and requests that worked (Skills).

Lexicon: names from the user's apps, games, YouTube channels and tool results. They prime Whisper and
fix sound-alike mishearings ("Jordan and Sammy" → "Jordan & Semih"). Common English words are never
rewritten, only words Whisper doesn't know as a whole word.

Spelling: typos and texting shorthand ("hows the wether", "cancle my timmer", "rn") are fixed before she
reads a request, typed or heard. Her own vocabulary comes first (the words of her tools plus the words the
user says to her, counted in lexicon.json), then the dictionary, and only when one fix clearly wins.

Skills: every request that worked (local or Claude) is kept with the tool calls that did it. Similar
new requests show the local model what worked before; near-identical ones are replayed without thinking.
"No, that's wrong" forgets the last one. The kept wins are also the training data for assistant/dream.py.

Everything runs on the CPU (difflib + a 23M-parameter embedding model in Ollama), so games aren't touched.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import math
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import MODELS_DIR, STATE_DIR

log = logging.getLogger("learning")

LEXICON_FILE = STATE_DIR / "lexicon.json"
SKILLS_FILE = STATE_DIR / "skills.json"
BRAVE_HISTORY = [Path.home() / ".config/BraveSoftware/Brave-Origin/Default/History",
                 Path.home() / ".config/BraveSoftware/Brave-Browser/Default/History"]

MAX_NAMES = 400
MAX_WORDS = 3000
MAX_SKILLS = 1500


def _common_words() -> dict[str, int]:
    """Whole-word tokens of Whisper's vocabulary ≈ common English words (24k) → token id (lower ≈ more common);
    names mostly aren't in it."""
    for f in MODELS_DIR.glob("whisper/models--*/snapshots/*/tokenizer.json"):
        try:
            vocab = json.loads(f.read_text())["model"]["vocab"]
            out: dict[str, int] = {}
            for k, i in vocab.items():
                if k.startswith("Ġ") and k[1:].isalpha():
                    out[k[1:].lower()] = min(i, out.get(k[1:].lower(), i))
            return out
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    return {}


def _key(word: str) -> str:
    """Rough phonetic key: spellings that sound alike get the same key (nearie/niri, semih/sammy)."""
    w = word.lower()
    for a, b in [("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("c(?=[eiy])", "s"), ("c", "k"), ("z", "s"),
                 ("y", "i"), ("w", "v"), ("h", ""), (r"(.)\1+", r"\1")]:
        w = re.sub(a, b, w)
    return w[:1] + re.sub("[aeiou]", "", w[1:])


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("&", " and "))


def similarity(a: str, b: str) -> float:
    """Average of spelling and sound similarity of two short phrases."""
    spelled = difflib.SequenceMatcher(None, a, b).ratio()
    sound = difflib.SequenceMatcher(None, " ".join(map(_key, a.split())), " ".join(map(_key, b.split()))).ratio()
    return (spelled + sound) / 2


def _dist(a: str, b: str, cap: int) -> int:
    """Edit distance where swapping two neighbours counts as one typo; gives up (cap + 1) past cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


EDGE = {"the", "a", "an", "and", "to", "of", "in", "on", "my", "for", "with", "at", "is", "it"}

# texting shorthand → what it means (whole words only)
SLANG = {"rn": "right now", "pls": "please", "plz": "please",
         "vid": "video", "vids": "videos", "tmrw": "tomorrow", "tmr": "tomorrow", "abt": "about", "smth": "something",
         "sth": "something", "bc": "because", "cuz": "because", "msg": "message", "pic": "picture", "pics": "pictures",
         "yt": "YouTube"}
# chat words that stay exactly as said: they may be the message to send ("tell them brb")
CHAT_WORDS = {"brb", "gg", "wp", "ggwp", "afk", "lol", "lmao", "lmfao", "omg", "idk", "ngl", "tbh", "btw", "gn", "gm",
              "ty", "thx", "np", "xd", "imo", "fr", "ong", "bruh", "nvm", "ikr", "irl", "rip", "pog", "poggers", "kek",
              "hbu", "wyd", "wbu", "ily", "ur", "u", "ok", "kk", "yw", "sry", "pls", "plz", "hru", "smh", "ez"}
# real words in the wrong place, only fixed in these spots ("turn of the wifi", "skip the add")
SLIPS = [(r"\b(this|next|last) weak\b", r"\1 week"), (r"\bturn of\b", "turn off"), (r"\bswitch of\b", "switch off"),
         (r"\b(?:to|two) (hot|cold|warm|loud|quiet|bright|dark|slow|fast|big|small|much|many|high|low|long)\b", r"too \1"),
         (r"\b(skip|the|this|that|an) add\b", r"\1 ad"), (r"\b(skip|the|these|those) adds\b", r"\1 ads"),
         (r"\bhow meny\b", "how many"), (r"\bwhats\b", "what's"), (r"\bhows\b", "how's"), (r"\bwheres\b", "where's"),
         (r"\bwhos\b", "who's"), (r"\bwhens\b", "when's"), (r"\bthats\b", "that's"), (r"\bdont\b", "don't"), (r"\bcant\b", "can't"),
         (r"\bdoesnt\b", "doesn't"), (r"\bisnt\b", "isn't"), (r"\bim\b", "I'm"), (r"\bive\b", "I've"),
         (r"\bhole (screen|thing|day|week|playlist|album|folder)\b", r"whole \1"), (r"\bfile manger\b", "file manager"),
         (r"\bclos (the|this|that|it|my|all)\b", r"close \1"), (r"\bthere (on|online|playing)\b", r"they're \1")]
PERSON = {"with", "to", "message", "msg", "dm", "friend", "tell", "ping", "text", "call", "invite", "named", "called"}
# misspellings the sound/typo rules can't settle on their own
TYPOS = {"fourty": "forty", "alot": "a lot", "untill": "until", "wierd": "weird", "recieve": "receive",
         "definately": "definitely", "seperate": "separate", "tommorow": "tomorrow", "tommorrow": "tomorrow",
         "occured": "occurred", "becuase": "because", "teh": "the", "adn": "and", "waht": "what", "youre": "you're",
         "theyre": "they're", "wont": "won't", "didnt": "didn't", "wasnt": "wasn't", "arent": "aren't"}
# words her tools talk about that the dictionary (Whisper's tokens) doesn't have as a whole word
DOMAIN = ("lofi phonk synthwave nightcore playlist youtube reddit github discord roblox fortnite minecraft valorant "
          "steam wifi bluetooth screenshot screenshots wallpaper workspace workspaces monitor monitors fullscreen "
          "notifications volume brightness timer timers reminder minutes seconds hours percent tomorrow tonight "
          "weekend weather forecast temperature kernel driver drivers updates update install uninstall package "
          "packages downloads documents pictures videos music folder clips clip summarize summary search "
          "everything something somewhere anything nothing message messages video videos window windows tab tabs "
          "browser please pause resume skip mute unmute shader shaders modpack modpacks mods lobby laggy lag fps "
          "ping keybind keybinds hotkey hotkeys overlay screenshare wallpapers dotfiles config configs terminal "
          "compositor crosshair sensitivity speedrun gameplay livestream streamer tiktok pinterest modrinth")


class Lexicon:
    def __init__(self):
        self.names: dict[str, dict] = {}   # display name → {"n": uses, "t": last seen, "src": where from}
        self.fixes: dict[str, str] = {}    # heard phrase (lower) → what the user said
        self.words: dict[str, int] = {}    # words the user says to her → how often (her vocabulary)
        self.common = _common_words()
        self.curated = set(DOMAIN.split())
        self.domain: set[str] = set(self.curated)  # + the words of her tools (set_domain)
        try:
            data = json.loads(LEXICON_FILE.read_text())
            self.names, self.fixes = data.get("names", {}), data.get("fixes", {})
            self.words = data.get("words", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        if len(self.names) > MAX_NAMES:  # keep the most used / most recent
            keep = sorted(self.names.items(), key=lambda kv: (kv[1]["n"], kv[1]["t"]), reverse=True)[:MAX_NAMES]
            self.names = dict(keep)
        if len(self.words) > MAX_WORDS:
            self.words = dict(sorted(self.words.items(), key=lambda kv: kv[1], reverse=True)[:MAX_WORDS])
        LEXICON_FILE.write_text(json.dumps({"names": self.names, "fixes": self.fixes, "words": self.words},
                                           indent=1, ensure_ascii=False))

    def learn(self, name: str, src: str, weight: int = 1, save: bool = True):
        name = re.sub(r"\s+", " ", name).strip(" .,!?\"'“”-")
        if not (3 <= len(name) <= 40) or len(name.split()) > 4 or not re.search(r"[A-Za-z]", name):
            return
        same = next((k for k in self.names if k.lower() == name.lower()), name)
        e = self.names.setdefault(same, {"n": 0, "t": 0, "src": src})
        e["n"] += weight
        e["t"] = int(time.time())
        if save:
            self.save()

    def learn_fix(self, heard: str, meant: str):
        heard, meant = heard.strip().lower(), meant.strip()
        if heard and meant and heard != meant.lower():
            self.fixes[heard] = meant
            self.learn(meant, "correction", weight=3, save=False)
            self.save()
            log.info("learned: '%s' means '%s'", heard, meant)

    # ── harvesting ──────────────────────────────────────────────────────────
    def refresh(self, apps: list[str], games: list[str]):
        """Re-read the user's world (cheap, ~50 ms): apps, games, YouTube channels from the browser history."""
        for a in apps:
            self.learn(a, "app", weight=0, save=False)
        for g in games:
            self.learn(g, "game", weight=2, save=False)
        for name, visits in self._sites():  # "open modrinth", "go to pinterest"
            self.learn(name, "site", weight=0 if visits < 25 else 1, save=False)
        for name, visits in self._youtube_channels():
            self.learn(name, "youtube", weight=min(visits, 5), save=False)
            base = re.sub(r"\s+(live|highlights|clips|shorts|official|tv|gaming|music|vods?|extra|2)$", "", name, flags=re.I)
            if base != name:  # "Jordan & Semih Live" → "Jordan & Semih" too
                self.learn(base, "youtube", weight=min(visits, 5), save=False)
        self.save()

    @staticmethod
    def _sites() -> list[tuple[str, int]]:
        """Sites the user visits a lot, by name ("modrinth.com" → Modrinth). Only host names, never what was
        searched or read there."""
        db = next((p for p in BRAVE_HISTORY if p.exists()), None)
        if not db:
            return []
        try:
            con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
            rows = con.execute("SELECT url, visit_count FROM urls ORDER BY visit_count DESC LIMIT 3000").fetchall()
            con.close()
        except sqlite3.Error as e:
            log.debug("history: %s", e)
            return []
        out: dict[str, int] = {}
        for url, n in rows:
            host = re.sub(r"^www\.|^[a-z]{2}\.(?=[a-z0-9-]+\.[a-z]+$)", "", urlparse(url).hostname or "")
            parts = host.split(".")
            if len(parts) < 2 or re.match(r"(accounts?|login|api|auth|search|mail|app|id|sso|oauth)$", parts[0]) \
                    and len(parts) > 2:
                continue
            name = parts[-2] if len(parts[-2]) > 3 else ".".join(parts[-2:])  # "x.ai" stays as is
            out[name] = out.get(name, 0) + (n or 1)
        return [(n.capitalize() if n.isalpha() else n, v) for n, v in out.items() if v >= 10][:40]

    @staticmethod
    def _youtube_channels() -> list[tuple[str, int]]:
        db = next((p for p in BRAVE_HISTORY if p.exists()), None)
        if not db:
            return []
        try:  # immutable: reads the file as is, even while Brave holds its lock
            con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
            rows = con.execute("SELECT title, visit_count FROM urls WHERE url LIKE 'https://www.youtube.com/@%' "
                               "AND title LIKE '% - YouTube' ORDER BY last_visit_time DESC LIMIT 200").fetchall()
            con.close()
        except sqlite3.Error as e:
            log.debug("history: %s", e)
            return []
        out: dict[str, int] = {}
        for title, n in rows:
            name = re.sub(r"^\(\d+\)\s*|\s+- YouTube$", "", title).strip()
            out[name] = out.get(name, 0) + (n or 1)
        return list(out.items())

    def from_tool(self, name: str, args: dict, result: str):
        """Names that tools found out are real (channel of the video she opened, game names…)."""
        if name in ("youtube_play", "browser_video"):
            if m := re.search(r"\bby (.+?)(?: \(\d|\. Other results|\n|$)", result):
                self.learn(m[1], "youtube", weight=2)
        elif name in ("game_news", "game_price", "launch_game") and not result.startswith(("error", "no ", "I couldn")):
            if args.get("game") or args.get("name"):
                self.learn(str(args.get("game") or args.get("name")).title(), "game")

    def set_domain(self, text: str):
        """Words from her tool descriptions and examples: what requests to her are made of."""
        self.domain |= {w for w in re.findall(r"[a-z]+", text.lower())  # content words, not "the", "one", "too"
                        if len(w) >= 3 and self.common.get(w, 99999) >= 1500}

    def hear(self, text: str, voice: bool, save: bool = True):
        """Count the words of a request. Heard words are spelled by Whisper, so new ones join her vocabulary
        once said twice; typed words only count if they're real words (a typo must never become one)."""
        for w in re.findall(r"[a-z]+", text.lower()):
            if len(w) >= 3 and (voice or self._known(w)):
                self.words[w] = self.words.get(w, 0) + 1
        if save:
            self.save()

    def _known(self, w: str) -> bool:
        return w in CHAT_WORDS or w in self.common or w in self.domain or self.words.get(w, 0) >= 2 or \
            any(w in n.lower().split() for n in self.names)

    def _near_name(self, w: str) -> bool:
        """Sounds like a real name she knows ("trimax" ~ Trymacs): left to correct(), never spelled into a word."""
        return any(similarity(w, p) >= 0.75 for n in self.names for p in _words(n) if len(p) >= 4 and
                   abs(len(p) - len(w)) <= 2 and p not in self.common and p not in self.domain)

    def _spell_word(self, w: str, person: bool = False) -> str | None:
        """The word this typo most likely meant, or None if unsure. After "message", "with"… it may be a
        friend's name (alex), so only her own vocabulary is used there, never the dictionary (alec)."""
        key = _key(w)

        def ok(c: str, d: int, cap: int) -> bool:  # sound-alikes may be one typo further off
            return d <= cap or (d == cap + 1 and _key(c) == key)

        # her own vocabulary first: tool words + what the user says. Sound-alikes first, then fewest typos, then
        # her core words, the user's favourites, and the closest spelling ("paus" → pause, not pass)
        cap = 1 if len(w) <= 5 else 2
        mine = sorted((-(_key(c) == key), d, -(c in self.curated), -min(self.words.get(c, 0), 5),
                       -round(difflib.SequenceMatcher(None, w, c).ratio(), 3), c)
                      for c in self.domain | set(self.words)
                      if self._known(c) and ok(c, d := _dist(w, c, cap + 1), cap))
        if mine and (len(mine) == 1 or mine[0][:5] < mine[1][:5]):
            return mine[0][5]
        if person:
            return None
        # then the dictionary, stricter: it must sound the same, or be two swapped letters ("cancle")
        cap = 1 if len(w) <= 6 else 2
        found = sorted((d, self.common[c], c) for c in self.common
                       if abs(len(c) - len(w)) <= cap and (d := _dist(w, c, cap)) <= cap and
                       (_key(c) == key or (d == 1 and sorted(c) == sorted(w))))
        if found and (len(found) == 1 or found[0][0] < found[1][0]):
            return found[0][2]
        return None

    def spell(self, text: str) -> tuple[str, list[str]]:
        """→ (text with typos and shorthand fixed, what was changed)."""
        changed: list[str] = []

        def slip(m: re.Match, repl: str) -> str:
            changed.append(f"{m[0]}→{m.expand(repl)}")
            return m.expand(repl)

        for pattern, repl in SLIPS:
            text = re.sub(pattern, lambda m: slip(m, repl), text, flags=re.I)
        out: list[str] = []
        for i, tok in enumerate(re.split(r"(\s+)", text)):
            m = re.fullmatch(r"([\"'(]*)([A-Za-z]+)([.,!?;:)\"']*)", tok)
            if not m:
                out.append(tok)
                continue
            pre, word, post = m.groups()
            low = word.lower()
            fix = None
            if low in SLANG or low in TYPOS:
                fix = SLANG.get(low) or TYPOS[low]
            elif len(low) >= 3 and not self._known(low) and not (word[0].isupper() and i > 0) \
                    and not self._near_name(low):  # Capitalized mid-sentence = probably a name
                before = out[-2].lower().strip(".,!?\"'") if len(out) >= 2 else ""
                fix = self._spell_word(low, before in PERSON)
            if fix:
                changed.append(f"{word}→{fix}")
                word = fix.capitalize() if word[0].isupper() and fix.islower() else fix
            out.append(pre + word + post)
        return "".join(out), changed

    # ── using it ────────────────────────────────────────────────────────────
    def prompt_names(self, limit: int = 30) -> list[str]:
        """Most used names for Whisper's prompt (it has room for ~220 tokens in total)."""
        ranked = sorted(self.names.items(), key=lambda kv: (kv[1]["n"], kv[1]["t"]), reverse=True)
        return [k for k, v in ranked if v["n"] > 0][:limit]

    def _uncommon(self, words: list[str]) -> bool:
        return bool(self.common) and any(w not in self.common and not w.isdigit() for w in words)

    def correct(self, text: str) -> tuple[str, list[str]]:
        """→ (text with sure fixes applied, hints about unsure ones for the LLM)."""
        for heard, meant in self.fixes.items():
            text = re.sub(rf"(?<![\w']){re.escape(heard)}(?![\w'])", meant, text, flags=re.I)
        words = text.split()
        low = [" ".join(_words(w)) for w in words]
        hints: list[str] = []
        targets = [(n, " ".join(_words(n))) for n in self.names]
        best: tuple[float, int, int, str, float] | None = None
        for size in range(1, 5):
            for i in range(len(words) - size + 1):
                span = [w for w in low[i:i + size] if w]
                if not span:
                    continue
                phrase = " ".join(span)
                if span[0] in EDGE or span[-1] in EDGE:
                    continue
                # words Whisper knows well are only corrected as part of a longer, very similar name
                uncommon = self._uncommon(phrase.split())
                if len(phrase) < 4 or (not uncommon and len(span) < 2):
                    continue
                bar = 0.0 if uncommon else 0.08
                exact = [norm for _, norm in targets if f" {norm} " in f" {phrase} "]
                for name, norm in targets:
                    if f" {norm} " in f" {phrase} " or abs(len(norm) - len(phrase)) > max(4, len(norm) // 2):
                        continue
                    if any(f" {e} " in f" {norm} " for e in exact):  # the name was said right, the rest isn't it
                        continue
                    if difflib.SequenceMatcher(None, phrase, norm).quick_ratio() < 0.5:
                        continue
                    s = similarity(phrase, norm)
                    if s >= 0.72 and (best is None or s > best[0]):
                        best = (s, i, size, name, bar)
        if best:
            s, i, size, name, bar = best
            heard = " ".join(words[i:i + size])
            if s - bar >= 0.8:
                tail = re.search(r"[.,!?]+$", words[i + size - 1])
                words[i:i + size] = [name + (tail[0] if tail else "")]
                log.info("heard '%s' → '%s' (%.2f)", heard, name, s)
                text = " ".join(words)
            else:
                kind = {"youtube": " (a YouTube channel)", "game": " (a game)", "app": " (an app)"}.get(
                    self.names[name]["src"], "")
                hints.append(f"the user probably said '{name}'{kind}, it was heard as '{heard.strip('.,!?')}'")
        return text, hints


# ── skills ──────────────────────────────────────────────────────────────────
NEGATIVE = re.compile(r"^\W*(no+|nope|wrong|that'?s (wrong|not (it|right|what i (said|meant|wanted)))|not that( one)?|"
                      r"you (got it|misheard|heard it) wrong|that'?s the wrong)\b", re.I)
CORRECTION = re.compile(r"\b(?:i (?:said|meant|wanted)|i'?m saying)\s+(?:the |a )?(.+?)(?:,? not .+)?[.!?]*$", re.I)
DESTRUCTIVE = {"trash", "move_file", "write_file", "install_packages", "install_updates", "type_text", "browser_type"}
FOLLOW_UP = re.compile(r"^\W*(yes|yeah|yep|sure|ok(ay)?|do it|go ahead|please do)\b|\b(them|those|that one|it)\W*$", re.I)
FORCE_CLAUDE = re.compile(r"^\s*(hey )?(ask|use) claude\b[,:]?\s*(to )?", re.I)
# tools that only look: guessing wrong between two of these costs nothing, so she just tries
READ_ONLY = {"system_info", "system_health", "weather", "web_research", "wikipedia", "arch_wiki", "world_clock",
             "game_news", "game_price", "package_info", "arch_news", "search_my_setup", "system_updates",
             "list_windows", "screen", "browser_page", "browser_history", "list_files", "find_files", "read_file",
             "read_link", "check_install", "routine"}
NEVER_KEEP = re.compile(r"^(error|refused|the user declined|no |nothing found|web search failed|unknown|"
                        r"there is no tool)", re.I)


# tools that were merged for the local model: old skills are read with the new names
ALIASES = {"read_screen": "screen", "see_screen": "screen", "terminal_text": "screen", "look_at_screen": "screen",
           "browser_type": "type_text"}


def _migrate(skill: dict) -> dict:
    for c in skill.get("calls", []):
        if c["name"] in ALIASES:
            c["name"] = ALIASES[c["name"]]
            c["args"] = {k: v for k, v in c["args"].items() if k in ("question", "text", "submit")}
    return skill


def _norm(text: str) -> str:
    t = re.sub(r"^(hey |ok |okay )?mio[,!.]?\s*", "", text.lower().strip())
    t = re.sub(r"^(please |can you |could you |would you |will you )+", "", t)
    return re.sub(r"[^\w& ]+", "", re.sub(r"\s+please$", "", t)).strip()


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a) * sum(y * y for y in b)) or 1)


class Skills:
    def __init__(self, cfg: dict):
        self.url = cfg.get("ollama_url", "http://127.0.0.1:11434")
        self.model = cfg.get("embed_model", "all-minilm")
        self.items: list[dict] = []
        self._embed_ok = True
        try:
            self.items = [_migrate(s) for s in json.loads(SKILLS_FILE.read_text())]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        self.items = self.items[-MAX_SKILLS:]
        SKILLS_FILE.write_text(json.dumps(self.items, ensure_ascii=False))

    async def _embed(self, text: str) -> list[float] | None:
        """CPU only (num_gpu 0): ~20 ms, never touches the GPU a game is using."""
        if not self._embed_ok:
            return None
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=10) as c:
                r = await c.post("/api/embed", json={"model": self.model, "input": text, "keep_alive": "30m",
                                                      "options": {"num_gpu": 0}})
            if r.status_code == 404:  # model not pulled: fall back to text similarity
                log.warning("embedding model %s missing (ollama pull %s); using text similarity", self.model,
                            self.model)
                self._embed_ok = False
                return None
            r.raise_for_status()
            return [round(x, 5) for x in r.json()["embeddings"][0]]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            log.debug("embed failed: %s", e)
            return None

    async def similar(self, text: str, k: int = 3) -> list[tuple[float, dict]]:
        if not self.items:
            return []
        norm = _norm(text)
        emb = await self._embed(norm)
        scored = []
        for s in self.items:
            if s.get("norm") == norm:
                score = 1.0
            elif emb and s.get("emb"):
                score = _cos(emb, s["emb"])
                # her own practice wordings count a little less than what the user really said
                score = max([score] + [_cos(emb, a["emb"]) * 0.95 for a in s.get("alts", []) if a.get("emb")])
            else:
                score = difflib.SequenceMatcher(None, norm, s.get("norm", "")).ratio() * 0.9
            scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]

    @staticmethod
    def replayable(text: str, score: float, skill: dict) -> bool:
        """Safe to redo without the LLM: (almost) the same words, and every argument is in what was said."""
        norm = _norm(text)
        if skill.get("norm") == norm:
            return True
        if score < 0.93 or set(re.findall(r"\d+", norm)) != set(re.findall(r"\d+", skill.get("norm", ""))):
            return False
        said = set(_words(norm))
        for c in skill["calls"]:
            for v in c["args"].values():
                if isinstance(v, str) and any(w not in said for w in _words(v) if len(w) > 2):
                    return False
        return True

    async def record(self, text: str, trace: list[dict], reply: str, src: str) -> dict | None:
        if any(c["name"] in DESTRUCTIVE or (c["name"] == "discord" and c["args"].get("action") == "send") or
               (c["name"] == "tidy_downloads" and c["args"].get("apply")) for c in trace) or FOLLOW_UP.search(text):
            return None  # never a shortcut: deleting/sending/installing, or "yes, delete them" (means what came before)
        calls = [c for c in trace if not NEVER_KEEP.match(c["result"])]
        if any(c["name"] == "routine" and c["args"].get("action") == "run" for c in calls):
            calls = [c for c in calls if c["name"] == "routine"]  # its steps ran inside it
        if not calls or not reply or len(calls) > 6:
            return None
        norm = _norm(text)
        skill = {"id": f"{int(time.time() * 1000):x}", "t": dt.datetime.now().isoformat(timespec="seconds"),
                 "text": text, "norm": norm, "src": src, "uses": 1, "reply": reply,
                 "calls": [{"name": c["name"], "args": c["args"], "result": c["result"][:1500]} for c in calls]}
        old = next((s for s in self.items if s["norm"] == norm), None)
        if old:
            skill["uses"] = old.get("uses", 1) + 1
            self.items.remove(old)
        skill["emb"] = (old or {}).get("emb") or await self._embed(norm)
        self.items.append(skill)
        self.save()
        log.info("learned skill (%s): %r → %s", src, text, ", ".join(c["name"] for c in skill["calls"]))
        return skill

    async def add_alts(self, skill: dict, alts: list[str]) -> int:
        """Other ways to say a learned request (from practice). Kept only if they still name every argument."""
        keep = []
        for alt in alts:
            norm = _norm(alt)
            if not norm or norm == skill["norm"] or any(a["norm"] == norm for a in keep) or not covers(alt, skill):
                continue
            keep.append({"text": alt, "norm": norm, "emb": await self._embed(norm)})
        skill["alts"] = keep[:4]
        return len(skill["alts"])

    def forget(self, skill_id: str) -> bool:
        before = len(self.items)
        self.items = [s for s in self.items if s["id"] != skill_id]
        if len(self.items) != before:
            self.save()
            log.info("forgot skill %s (user said it was wrong)", skill_id)
            return True
        return False


WORDY_ARGS = {"query", "who", "text", "game", "name", "target", "path", "place", "location", "label", "site"}


def covers(text: str, skill: dict) -> bool:
    """A rewording of a request must still contain its numbers and a word of each wordy argument."""
    said = set(_words(text))
    nums = set(re.findall(r"\d+", text))
    for c in skill["calls"]:
        for k, v in c["args"].items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and str(int(v)) in skill["text"] and str(int(v)) not in nums:
                return False
            if k in WORDY_ARGS and isinstance(v, str):
                words = [w for w in _words(v) if len(w) > 2 and w in set(_words(skill["text"]))]
                if words and not any(w in said for w in words):
                    return False
    return True


def example_line(skill: dict) -> str:
    calls = ", ".join(f"{c['name']}({', '.join(f'{k}={json.dumps(v, ensure_ascii=False)}' for k, v in c['args'].items())})"
                      for c in skill["calls"])
    return f'- "{skill["text"]}" → {calls}'


class Learning:
    """What the daemon talks to."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lexicon = Lexicon()
        self.skills = Skills(cfg)
        self.last_skill: dict | None = None
        self.last_text = self.last_reply = ""
        self.last_time = 0.0
        self._refreshed = 0.0
        self._hits: list[tuple[float, dict]] = []

    def refresh(self, sources) -> bool:
        """sources() → (app names, game names); re-read at most every 6 hours."""
        if time.time() - self._refreshed < 6 * 3600:
            return False
        self._refreshed = time.time()
        self.lexicon.refresh(*sources())
        return True

    def correct(self, text: str, voice: bool) -> tuple[str, list[str]]:
        """Every request goes through here first: typos, then mishearings of names; then its words are counted.
        → (fixed text, hints about unsure name fixes for the LLM)."""
        text, changed = self.lexicon.spell(text)
        if changed:
            log.info("spelling: %s", ", ".join(changed))
        text, hints = self.lexicon.correct(text)
        self.lexicon.hear(text, voice)
        return text, hints

    def feedback(self, text: str) -> None:
        """Called with every new request: negative feedback / corrections about the previous one."""
        recent = time.time() - self.last_time < 120
        if not recent:
            self.last_skill = None
            return
        answering = self.last_reply.rstrip().endswith("?")  # "no" to her question isn't a complaint
        if NEGATIVE.search(text) and self.last_skill and not answering:
            self.skills.forget(self.last_skill["id"])
            self.last_skill = None
        if (m := CORRECTION.search(text)) and self.last_text and not answering:
            meant = m[1].strip()
            # find what she heard instead: the span of the last request that sounds most like the correction
            words = self.last_text.split()
            n = len(meant.split())
            spans = [" ".join(words[i:i + size]) for size in range(max(1, n - 1), n + 2)
                     for i in range(len(words) - size + 1)]
            scored = [(similarity(" ".join(_words(s)), " ".join(_words(meant))), s) for s in spans]
            if scored:
                s, heard = max(scored)
                if 0.45 <= s < 1 and len(meant) <= 40:
                    self.lexicon.learn_fix(heard.strip(".,!?"), meant)

    async def learned(self, text: str) -> tuple[list[dict], dict | None]:
        """→ (examples of what worked for similar requests, a skill to replay directly or None)."""
        hits = self._hits = await self.skills.similar(text, k=3)
        examples = [s for score, s in hits if score >= 0.6]
        replay = hits[0][1] if hits and self.skills.replayable(text, hits[0][0], hits[0][1]) else None
        return examples, replay

    def choice_question(self) -> str:
        """After learned(): two past requests match about equally well, did different things, and at least one
        changes something ("close this": the tab or the window?) → a question instead of a guess, else ''."""
        if len(self._hits) < 2:
            return ""
        (s1, a), (s2, b) = self._hits[:2]
        ta, tb = ({c["name"] for c in x["calls"]} for x in (a, b))
        if s1 < 0.85 or s1 - s2 > 0.03 or ta == tb or ta | tb <= READ_ONLY:
            return ""
        return f"Do you mean like \u201c{a['text']}\u201d, or like \u201c{b['text']}\u201d?"

    async def practice(self, brain, limit: int = 60) -> int:
        """Her own homework: for learned requests without them yet, write other ways the user might say them
        (casual, short, with typos) so new wordings find them. Uses the local model (~1 s each)."""
        todo = [s for s in self.skills.items if "alts" not in s][-limit:]
        n = 0
        for s in todo:
            alts = await brain.paraphrase(s["text"], s["calls"])
            n += await self.skills.add_alts(s, alts)
        if todo:
            self.skills.save()
            log.info("practised %d requests: %d new wordings", len(todo), n)
        return n

    async def done(self, text: str, trace: list[dict], reply: str, src: str):
        text = FORCE_CLAUDE.sub("", text)
        complaint = NEGATIVE.search(text) or CORRECTION.search(text)  # what she did then is a fix, not a skill
        self.last_text, self.last_reply, self.last_time = text, reply, time.time()
        self.last_skill = await self.skills.record(text, trace, reply, src) \
            if src in ("local", "claude", "replay") and not complaint else None
