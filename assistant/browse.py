"""Browser tasks in several steps: "open brave origins, look up rohaze asian maps and click the first video".

The request is cut into clauses and each clause becomes one step (open the browser, go to a site, search, pick a
result, click, type, scroll, back…). The steps run in order, and each one waits for the page the step before
opened, so "click the first video" clicks on the new results page, not the old one. Zero tokens; a clause it
can't read goes to the local model as one browser step, and anything else the normal way as its own request.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import quote_plus, urlparse

from . import browser
from .tools import SEARCH, SITES, ToolBox, find_app, to_url

log = logging.getLogger("browse")

BROWSER = r"(?:brave(?:[ -]origins?| browser)?|(?:the |my |a )?(?:web )?browser)"
# "…, on brave origin", "… using the browser": which browser, not part of the search words
MENTION = re.compile(rf"\s+(?:on|in|with|using|through|via|inside) {BROWSER}\b")
OPEN_BROWSER = re.compile(rf"(?:open|launch|start|run|fire up|pull up|bring up|go to|go on|switch to)(?: up)? "
                          rf"{BROWSER}(?: for me| real quick| quickly)?")
NAMES = {"youtube": "YouTube", "github": "GitHub", "duckduckgo": "DuckDuckGo", "aur": "the AUR", "the aur": "the AUR",
         "brave": "Brave Search", "tiktok": "TikTok", "youtube music": "YouTube Music"}
SITE = "|".join(sorted((re.escape(k) for k in SEARCH), key=len, reverse=True)) + "|yt"
ORD = r"first|1st|top|second|2nd|third|3rd|fourth|4th|fifth|5th|last"
NTH = {"first": "first", "1st": "first", "top": "first", "second": "second", "2nd": "second", "third": "third",
       "3rd": "third", "fourth": "fourth", "4th": "fourth", "fifth": "fifth", "5th": "fifth", "last": "last"}
KIND = (r"videos?|vids?|clips?|songs?|results?|search results?|links?|ones?|posts?|items?|hits?|options?|entry|"
        r"entries|threads?|articles?|pages?|products?|listings?|things?")
# a new clause starts at ", " / "then" / "and <verb>" ("search tom and jerry" stays one clause)
VERB = (r"open|launch|go|head|visit|navigate|search|look|find|google|type|write|enter|press|hit|click|tap|select|"
        r"choose|pick|play|watch|put|pull|bring|scroll|pause|resume|skip|mute|unmute|close|switch|read|tell|show|"
        r"summari[sz]e|reload|refresh|start|take|then")
SPLIT = re.compile(rf"\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+then\s+|\s+(?:and\s+)?after that\s+|\s+then\s+|"
                   rf"\s+and\s+(?=(?:{VERB})\b)")
# clauses about a page/browser thing the local model may still turn into a step
BROWSERY = re.compile(r"\b(video|vid|result|link|search|look|find|click|tap|tab|page|site|youtube|channel|post|"
                      r"comments?|scroll|type|button)\b")
PAGE_OPS = ("pick", "click", "type", "scroll", "nav", "other")


def _name(site: str) -> str:
    return NAMES.get(site, site.title())


def _clean(text: str) -> str:
    from .brain.fastpath import _clean as fast_clean

    t = re.sub(r"^\W*(?:hey|hi|ok|okay)?[\s,]*(?:mio|miu|mia|miyo|meo)\b[\s,.!]*", "", text.strip(), flags=re.I)
    t = fast_clean(t)
    return re.sub(r"\s+(?:for me|real quick|please)$", "", t)


def _query(q: str) -> tuple[str, bool]:
    """Search words without the filler; True if they asked for videos ('videos of X')."""
    video = bool(re.match(r"(?:me )?(?:some |a |the )?(?:videos?|vids?|clips?) (?:of|about|from|by|on|for)\b", q) or
                 re.search(r" (?:videos?|vids?|clips?)$", q))
    q = re.sub(r"^(?:me |us )?(?:some |a |an |the |that |this )?(?:(?:videos?|vids?|clips?) (?:of|about|from|by|on|for) )?",
               "", q)
    q = re.sub(r" (?:videos?|vids?|clips?)$", "", q)
    return q.strip(" '\"“”"), video


def _site_key(word: str) -> str:
    return "youtube" if word == "yt" else word


def clause(c: str, mentioned: bool) -> dict:
    """One clause → one step: {"op": …}; op "other" when it isn't a browser step this parser knows."""
    c = c.strip()
    if OPEN_BROWSER.fullmatch(c):
        return {"op": "browser"}
    c = MENTION.sub("", c).strip()
    if re.fullmatch(r"(?:open|make|start)(?: up)? (?:a )?new tab", c):
        return {"op": "newtab"}

    # "open the first video", "click the second result", "play it" (right after a search)
    if m := re.fullmatch(rf"(?:open|play|click(?: on)?|tap(?: on)?|watch|start|put on|go to|go into|take me to|"
                         rf"select|choose|pick|show me|pull up|bring up|hit)(?: up)? (?:the |that |on the )?"
                         rf"(?:very )?({ORD})(?: ({KIND}))?(?: (?:on|in|from|of) (?:the |this )?"
                         rf"(?:page|list|results|search(?: results)?|screen))?(?: (?:you see|there|that comes up|"
                         rf"that shows up|that pops up))?|(?:open|play|click(?: on)?|watch|start) (?:it|that|that one)",
                         c):
        kind = m[2] or ""  # "the second one": a video on YouTube, a result elsewhere
        return {"op": "pick", "nth": NTH.get(m[1] or "first", "first"), "it": not m[1],
                "kind": "video" if re.match(r"vid|clip|song", kind) else "" if re.match(r"one|thing", kind) or not kind
                else "result"}

    if m := re.fullmatch(r"(?:type|write|enter)(?: in| out)? (.+?)(?: (?:in|into) the (?:search )?(?:box|bar|field))?"
                         r"(?: (?:and )?(?:press|hit) enter| and (?:send|search|submit) it)?", c):
        return {"op": "type", "text": m[1].strip(" '\"“”"), "submit": bool(re.search(r"enter|send|search|submit", c[m.end(1):]))}
    if m := re.fullmatch(r"scroll (up|down)(?: a bit| a little| some more| more)?|scroll (?:all the way )?(?:to the )?"
                         r"(top|bottom)(?: of the page)?|(?:go|jump) to the (top|bottom)(?: of the page)?", c):
        return {"op": "scroll", "direction": m[1] or m[2] or m[3]}
    if m := re.fullmatch(r"(?:go )?(back|forward)(?: a page| one page| to the (?:last|previous) page)?|"
                         r"(?:reload|refresh)(?: the| this)?(?: page| tab)?", c):
        if not re.search(r"\d|second|minute", c):
            return {"op": "nav", "to": m[1] or "reload"}

    # searches: "look up X (on youtube)", "search youtube for X", "youtube search X"
    if not re.search(r"\btabs?$", c):
        if m := re.fullmatch(rf"(?:search|look|go)(?: on| in)? ({SITE}) for (.+)|({SITE}) search(?: for)? (.+)|"
                             rf"(?:search|look up|google|find|look for|search up|search for)(?: for)? (.+?)"
                             rf"(?: (?:on|in|at|using|over on) ({SITE}))?", c):
            site = _site_key(m[1] or m[3] or m[6] or "")
            q, video = _query(m[2] or m[4] or m[5])
            files = re.search(r"\b(pdfs?|files?|folders?|downloads?|downloaded|documents?|screenshots?|photos?)\b", q)
            if q and (site != "wikipedia" or mentioned) and not (files and not site and not mentioned):
                return {"op": "search", "query": q, "site": site, "video": video}
        # "play X on youtube", "watch X"
        if m := re.fullmatch(rf"(?:play|put on|watch|pull up|bring up)(?: me)? (?:the )?(?:video |song )?(.+?)"
                             rf"(?: (?:on|in) (youtube|yt))?", c):
            q, _ = _query(m[1])
            if q and not re.fullmatch(r"(?:it|this|that|the video|music|the music|something)", q) and \
                    (m[2] or mentioned):
                return {"op": "play", "query": q}

    # "go to youtube", "open reddit", "visit github.com"
    if m := re.fullmatch(r"(?:open|go (?:on|to|onto|over to)|visit|head (?:over )?to|navigate to|pull up|bring up|"
                         r"take me to|load)(?: up)? (?:the |my )?(.+?)(?: website| site| page| homepage| dot com)?", c):
        target = m[1].strip()
        url = to_url(target)
        app = find_app(target)
        is_app = app is not None and re.sub(r"\W", "", target) in re.sub(r"\W", "", app["name"].lower())
        if url and (mentioned or "." in target or not is_app or re.search(r"\b(website|site|page)$", c)):
            key = re.sub(r"^(?:www\.)?([a-z0-9-]+)\..*$", r"\1", target)
            return {"op": "site", "url": url, "site": key if key in SEARCH else target if target in SEARCH else "",
                    "name": _name(target) if target in SITES or target in SEARCH else target}
    if re.fullmatch(r"(?:click|tap|press|hit)(?: on)? (?:the )?.+", c) and not re.fullmatch(r"(?:press|hit) enter", c):
        target = re.sub(r"^(?:click|tap|press|hit)(?: on)? (?:the )?|(?: button| link)$", "", c)
        return {"op": "click", "target": target}
    return {"op": "other", "text": c}


def parse(text: str) -> list[dict] | None:
    """The steps of a browser task, or None if this isn't one (then it goes the normal way)."""
    t = _clean(text)
    if not t:
        return None
    mentioned = bool(re.search(rf"\b{BROWSER}\b", t))
    parts = [p for p in SPLIT.split(t) if p and p.strip() not in ("and", "then", "")]
    steps = [clause(p, mentioned) for p in parts]
    ops = [s["op"] for s in steps]
    navigates = any(o in ("browser", "site", "search", "newtab", "play") for o in ops)
    sited = any(o == "search" and s["site"] for o, s in zip(ops, steps))
    if not navigates or ops == ["browser"]:
        return None
    if len(steps) == 1:  # one thing: only when the fast path would read it wrong ("… on brave origin in youtube")
        return steps if mentioned and ops[0] != "play" else None
    if not ({"browser", "site", "newtab"} & set(ops) or mentioned or sited or
            ("search" in ops and any(s["op"] == "pick" and not s.get("it") for s in steps[ops.index("search"):]))):
        return None  # "look up the weather and set a timer": not about the browser
    return steps


class Task:
    """Runs the steps in order. respond(text) handles a clause that isn't a browser step (fast path/local model)."""

    def __init__(self, tools: ToolBox, respond: Callable[[str], Awaitable[str]], local=None):
        self.tools, self.respond, self.local = tools, respond, local
        self.site = ""        # the site we're on (a SEARCH key), for "search X" without a site
        self.query = ""       # the last search, for the fallback when a result can't be clicked
        self.youtube = False  # …and whether it was on YouTube
        self.expect = ""      # what the active tab's URL has to contain once the last navigation is done
        self.loading = False  # something was opened and the page hasn't been waited for yet
        self.said: list[str] = []  # short phrases; a whole reply from another step starts with "\0"

    async def _open(self, url: str):
        r = await self.tools.call("open", {"target": url, "app": "Brave Origin"})
        self.loading = r.startswith("opened")
        return r

    async def _connected(self, timeout: float = 20) -> bool:
        """The extension connects a few seconds after Brave starts."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            r = await browser.call("tabs", op="list")
            if "error" not in r:
                return True
            if r["error"] != browser.NOT_CONNECTED or loop.time() > end:
                return False
            await asyncio.sleep(0.5)

    async def _settle(self):
        """Wait until the page the last step opened is the active tab and has loaded."""
        if not self.loading:
            return
        self.loading = False
        if not await self._connected():
            return
        r = await browser.call("wait", expect=self.expect, timeout=12000)
        if "unknown action" in r.get("error", ""):  # extension before 1.2: no waiting, just give it a moment
            await asyncio.sleep(3)
        elif "error" in r:
            log.info("waiting for %r: %s", self.expect, r["error"])

    async def _click(self, target: str, tries: float = 8) -> str:
        """Results are often drawn a moment after the page loads: the extension keeps looking for a few seconds.
        One before 1.2 answers right away, so then look again from here."""
        loop = asyncio.get_running_loop()
        end = loop.time() + tries
        while True:
            start = loop.time()
            r = await self.tools.call("browser_click", {"target": target, "wait": tries})
            if not r.startswith("couldn't find") or loop.time() > end or loop.time() - start > tries / 2:
                return r
            await asyncio.sleep(1)

    async def _ask_model(self, c: str) -> dict:
        """A clause the patterns didn't get ("find that rohaze guy's asian maps vid"): one structured call."""
        if not self.local or not BROWSERY.search(c):
            return {"op": "other", "text": c}
        step = await self.local.browser_step(c, self.site)
        if not step:
            return {"op": "other", "text": c}
        words = set(re.findall(r"[a-z0-9']+", c))
        for k in ("query", "text", "target"):  # never search/type/click for words they didn't say
            got = re.findall(r"[a-z0-9']+", str(step.get(k, "")).lower())
            if got and sum(w in words for w in got) < max(1, len(got) * 0.7):
                log.info("model step %s dropped: %r isn't in %r", step, step.get(k), c)
                return {"op": "other", "text": c}
        log.info("model read %r as %s", c, step)
        return step

    async def run(self, steps: list[dict]) -> str:
        for i, s in enumerate(steps):
            if s["op"] == "other":
                s = await self._ask_model(s["text"])
            ok = await self.step(s, steps[i + 1:])
            if not ok:
                if i < len(steps) - 1:
                    self.said.append("so I stopped there")
                break
        return self.reply()

    def reply(self) -> str:
        """'Opened Reddit, searched for niri and opened “…”!'; a whole answer from another step is its own sentence."""
        out, run = [], []

        def flush():
            if run:
                text = run[0] if len(run) == 1 else ", ".join(run[:-1]) + " and " + run[-1]
                text = text[0].upper() + text[1:]
                out.append(text + ("." if re.search(r"couldn'?t|stopped", text) else "!"))
                run.clear()
        for p in self.said:
            if not p:
                continue
            if p.startswith("\0") and (re.search(r"[.!?] \S", p) or len(p) > 70):
                flush()
                out.append(p[1:] if p.endswith((".", "!", "?")) else p[1:] + ".")
            else:
                p = p.lstrip("\0").rstrip(".!")
                run.append(p[0].lower() + p[1:] if len(p) > 1 and p[1].islower() else p)
        flush()
        return " ".join(out) or "Done!"

    async def step(self, s: dict, rest: list[dict]) -> bool:
        op = s["op"]
        if op in PAGE_OPS:
            await self._settle()
        if op == "browser":
            if not any(r["op"] in ("site", "search", "newtab", "play") for r in rest):
                await self.tools.call("launch_app", {"name": "Brave Origin"})
                self.said.append("opened Brave")
            return True
        if op == "newtab":
            if rest and rest[0]["op"] in ("site", "search", "play"):
                return True  # that opens in a new tab anyway
            r = await browser.call("open", url="chrome://newtab/")
            self.said.append("opened a new tab" if "error" not in r else "")
            return True
        if op == "site":
            if s.get("site") and rest and rest[0]["op"] == "search" and not rest[0].get("site"):
                self.site = s["site"]  # "go to reddit and search X": straight to reddit's search page
                return True
            r = await self._open(s["url"])
            if not self.loading:
                self.said.append(f"couldn't open {s['name']}")
                return False
            self.site, self.expect = s.get("site", ""), urlparse(s["url"]).netloc.removeprefix("www.")
            self.said.append(f"opened {s['name']}")
            return True
        if op == "search":
            return await self._search(s, rest)
        if op == "play":
            said, self.tools.said = self.tools.said, ""  # a play step is them asking for the video ("get me that vid")
            try:
                r = await self.tools.call("youtube_play", {"query": s["query"]})
            finally:
                self.tools.said = said
            if hit := re.match(r"playing “(.+?)” by (.+?)(?:\. Other results|$)", r):
                self.said.append(f"playing {hit[1]} by {hit[2]}")
                return True
            self.said.append(r.removeprefix("error: "))
            return r.startswith("showing")
        if op == "pick":
            return await self._pick(s)
        if op == "click":
            r = await self._click(s["target"], 5)
            self.loading = True  # a link opens a new page
            self.expect = ""
            self.said.append("clicked it" if r.startswith("clicked") else "left it alone" if "declined" in r
                             else r.removeprefix("error: "))
            return r.startswith("clicked")
        if op == "type":
            r = await self.tools.call("browser_type", {"text": s["text"], "submit": bool(s.get("submit"))})
            if s.get("submit"):
                self.loading, self.expect = True, ""
            self.said.append(f"typed {s['text']}" if r.startswith("typed") else r.removeprefix("error: "))
            return r.startswith("typed")
        if op == "scroll":
            r = await self.tools.call("browser_scroll", {"direction": s["direction"]})
            self.said.append("" if r.startswith("scrolled") else r.removeprefix("error: "))
            return r.startswith("scrolled")
        if op == "nav":
            r = await browser.call("nav", to=s["to"])
            if "error" in r:
                self.said.append("I couldn't go " + s["to"] if s["to"] != "reload" else "I couldn't reload it")
                return False
            self.loading, self.expect = True, ""
            self.said.append({"back": "went back", "forward": "went forward"}.get(s["to"], "reloaded it"))
            return True
        reply = await self.respond(s.get("text", ""))
        if re.fullmatch(r"(?:okay|ok|done|sure|got it|here you go)[.!]*", reply or "", re.I):  # "…and paused it"
            verb, _, rest = s.get("text", "").partition(" ")
            past = {"pause": "paused", "skip": "skipped", "mute": "muted", "unmute": "unmuted", "play": "played",
                    "resume": "resumed", "close": "closed", "stop": "stopped"}.get(verb)
            reply = f"{past} {rest}".strip() if past else ""
        self.said.append("\0" + reply if reply else "")
        return not re.search(r"\b(declined|couldn'?t|can'?t|failed|error)\b", reply or "", re.I)

    async def _search(self, s: dict, rest: list[dict]) -> bool:
        site = s.get("site") or self.site
        if not site and (s.get("video") or any(r["op"] == "pick" and r.get("kind") == "video" for r in rest)):
            site = "youtube"  # "look up X and play the first video"
        query = s["query"]
        if not site and self.expect and not self.site:  # on a site without a known search page: its search box
            await self._settle()
            r = await self.tools.call("browser_type", {"text": query, "submit": True})
            if r.startswith("typed"):
                self.loading, self.expect, self.query = True, "", query
                self.said.append(f"searched for {query}")
                return True
        site = site or "brave"
        r = await self._open(SEARCH[site] + quote_plus(query))
        if not self.loading:
            self.said.append(f"couldn't open the search: {r}")
            return False
        self.site, self.query, self.youtube, self.expect = site, query, site == "youtube", query.lower()
        self.said.append(f"searched {_name(site)} for {query}" if site != "brave" else f"searched for {query}")
        return True

    async def _pick(self, s: dict) -> bool:
        kind = s.get("kind") or ("video" if self.youtube else "result")
        nth = s.get("nth", "first")
        r = await self._click(f"{nth} {kind}") if await self._connected(3) else browser.NOT_CONNECTED
        if r.startswith("clicked"):
            label = re.sub(r"\s+", " ", r.removeprefix("clicked ")).strip()
            label = label if len(label) < 90 else label[:87].rsplit(" ", 1)[0] + "…"
            if self.said and self.said[-1].startswith("searched"):  # "Searched YouTube for X and opened …" is a lot
                self.said.pop()
            self.said.append(f"playing “{label}”" if kind == "video" else f"opened “{label}”")
            self.loading, self.expect = True, ""
            return True
        if "declined" in r:
            self.said.append("left it alone")
            return False
        if self.query and nth == "first":  # no extension (or no such result on the page): the top hit
            log.info("click failed (%s), top hit of %r", r, self.query)
            said, self.tools.said = self.tools.said, ""  # they asked for this result
            try:
                if self.said and self.said[-1].startswith("searched"):
                    self.said.pop()
                if self.youtube:
                    r = await self.tools.call("youtube_play", {"query": self.query})
                    if hit := re.match(r"playing “(.+?)” by (.+?)(?:\. Other results|$)", r):
                        self.said.append(f"playing {hit[1]} by {hit[2]}")
                        return True
                else:
                    where = "" if self.site in ("brave", "google", "duckduckgo") else " " + self.site
                    r = await self.tools.call("open_best_match", {"query": self.query + where})
                    if r.startswith("opened"):
                        self.said.append("opened the top result")
                        return True
            finally:
                self.tools.said = said
        self.said.append(f"I couldn't find the {nth} {kind or 'result'} on the page")
        return False
