"""Zero-token fast path: common commands handled with regexes, no LLM at all."""
from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass

from .. import clock
from ..tools import SEARCH, ToolBox, find_app, to_url

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
       "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
       "eighty": 80, "ninety": 90, "hundred": 100, "zero": 0}


@dataclass
class Result:
    reply: str
    control: str | None = None  # stop | hide | forget


def _num(s: str) -> int | None:
    s = s.strip().lower().rstrip("%")
    if s.isdigit():
        return int(s)
    return NUM.get(s)


def _clean(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"^(hey |ok |okay )?(mio|mia|miyo|meo|mio-chan|assistant)[,!.]?\s*", "", t)
    t = re.sub(r"^(please |can you |could you |would you )+", "", t)
    t = re.sub(r"[.!?,]+$", "", t).strip()
    return re.sub(r"\s+please$", "", t)


def _noted(tools: ToolBox, args: dict, result: str):
    """Timers are set here directly, not through the timer tool: still report them like a tool call, so they
    are in the trace ('undo that', self-check, learning)."""
    if tools.on_result:
        tools.on_result("timer", args, result)


async def _timers(t: str, tools: ToolBox) -> Result | None:
    """'timer 5 minutes', 'remind me in 20 minutes to …', 'remind me to … at 6 pm', 'cancel my timer', 'how long left'."""
    from .. import timers as tm

    kinds = r"\b(timers?|reminders?|alarms?)\b"
    if re.search(r"\b(how (much time|long)( is)? left|time left)\b", t) or (
            re.search(kinds, t) and re.search(r"\b(what|which|any|list|show|how many|check)\b", t)
            and not re.search(r"\b(set|start|make|create)\b", t)):
        return Result(tools.timers.summary())
    if re.search(r"\b(cancel|stop|delete|remove|clear|turn off|dismiss)\b", t) and re.search(kinds, t):
        which = "all" if re.search(r"\ball\b", t) else ""
        if not which and (m := re.search(r"\b(?:for|about|to) (.+)$", t)):
            which = m[1]
        elif not which and (m := re.search(r"\b(?:the |my )?([a-z]+) (?:timer|reminder)\b", t)) and m[1] not in (
                "the", "my", "a", "that", "this"):
            which = m[1]
        kind = ("timer" if re.search(r"\btimers?\b", t) else
                "reminder" if re.search(r"\b(reminders?|alarms?)\b", t) else "")
        gone = tools.timers.cancel(which, kind)
        _noted(tools, {"action": "cancel", "label": which}, f"cancelled {len(gone)}")
        if not gone:
            return Result("You don't have any timers running." if not which else f"I couldn't find one for {which}.")
        return Result("Cancelled." if len(gone) == 1 else f"Cancelled all {len(gone)}.")

    # "set a reminder in six minutes (to …)", "set me a reminder for 5 pm" → the "remind me …" form below
    if m := re.fullmatch(r"(?:set|make|create|add|put in|give me) (?:me )?(?:a |an )?reminder(?: for me)? (.+)", t):
        rest = re.sub(r"^for (?=(?:an? |half an? )?[\w.-]+ (?:seconds?|minutes?|hours?|mins?|secs?|hrs?)\b)", "in ",
                      m[1])
        t = "remind me " + re.sub(r"^for (?=\d|noon|midnight)", "at ", rest)
    now = dt.datetime.now()
    if m := re.fullmatch(r"remind me (.+)", t):
        rest = m[1]
        secs = tm.parse_duration(rest) if re.search(r"\b(in|after)\b", rest) else None
        when = None if secs else tm.parse_clock(rest, now)
        if not secs and not when:
            return None  # "remind me about it later": let the model ask when
        label = re.sub(r"\b(in|after) (an? |half an? )?[\w .-]*?\b(seconds?|minutes?|hours?|mins?|secs?|hrs?)\b|"
                       r"\b(tomorrow|tonight|this evening|this afternoon)\b|"
                       r"\bat (noon|midnight|\d{1,2}([:.]\d{2})?\s*(am|pm|a\.m\.|p\.m\.|o'?clock)?)", " ", rest)
        label = re.sub(r"^\s*(to|about|that|of)\s+|\s+$", "", re.sub(r"\s+", " ", label)).strip()
        label = re.sub(r"^(to|about|that|of) ", "", label)
        if secs:
            tools.timers.add(now + dt.timedelta(seconds=secs), label, "reminder", secs)
            _noted(tools, {"action": "set", "minutes": secs // 60 if secs % 60 == 0 else round(secs / 60, 2), "label": label}, "reminder set")
            told = f"in {tm.say_duration(secs)}"
        else:
            tools.timers.add(when, label, "reminder")
            _noted(tools, {"action": "set", "at": when.strftime("%H:%M"), "label": label}, "reminder set")
            told = tm.say_clock(when, now)
        return Result(f"Okay, I'll remind you {told}" + (f" to {tm.you(label)}." if label else "."))
    if re.search(r"\b(timer|alarm)\b", t) and re.search(r"\b(set|start|make|create|put|timer|alarm)\b", t):
        if secs := tm.parse_duration(t):
            label = ""
            if lm := re.search(r"\b(?:for|called|named) (?:the |my )?([a-z][a-z ]+)$", t):
                label = "" if tm.parse_duration(lm[1]) else lm[1]
            elif lm := re.search(r"\b(?:a |an |the )?([a-z]+) (?:timer|alarm)\b", t):
                label = "" if lm[1] in ("a", "an", "the", "my", "set", "start", "minute", "second", "hour") \
                    or tm.parse_duration(lm[1]) else lm[1]
            tools.timers.add(now + dt.timedelta(seconds=secs), label, "timer", secs)
            _noted(tools, {"action": "set", "minutes": secs // 60 if secs % 60 == 0 else round(secs / 60, 2), "label": label}, "timer set")
            return Result(f"Timer set for {tm.say_duration(secs)}" + (f" for the {tm.you(label)}!" if label else "!"))
        if m := re.search(r"\b(?:alarm|timer) (?:for |at )(.+)$", t):
            if when := tm.parse_clock("at " + m[1].removeprefix("at "), now):
                tools.timers.add(when, "", "reminder")
                _noted(tools, {"action": "set", "at": when.strftime("%H:%M")}, "alarm set")
                return Result(f"Alarm set for {tm.say_clock(when, now)}.")
    return None


async def _browser(t: str, tools: ToolBox) -> Result | None:
    """YouTube, tabs, scrolling, clicking and site searches through the browser extension."""
    from urllib.parse import quote_plus

    from ..timers import parse_duration

    async def video(action: str, value=None, done: str = "Okay!") -> Result:
        r = await tools.call("browser_video", {"action": action, **({"value": value} if value is not None else {})})
        if r.startswith("error: "):
            r = r[7:]
        return Result(done if " done. " in r else r[0].upper() + r[1:] + ".")

    if m := re.fullmatch(r"(?:play|put on|open(?: up)?|pull up|bring up|watch|show me|start) (?:the )?(?:video |song )?"
                         r"(.+?) (?:on|in) (?:youtube|yt)|"
                         r"(?:search|find|look up) (?:for )?(.+?) (?:on|in) youtube and (?:open|play|click|start|watch) "
                         r"(?:the )?(?:first|top) (?:video|result|one)|(?:play|put on) (?:the )?(?:youtube|yt) "
                         r"(?:video )?(?:of |for )?(.+)", t):
        query = re.sub(r"^(?:a |some |the )?(?:videos? (?:of|about|from) )?", "", m[1] or m[2] or m[3])
        r = await tools.call("youtube_play", {"query": query})
        if hit := re.match(r"playing “(.+?)” by (.+?)(?:\. Other results|$)", r):
            return Result(f"Playing {hit[1]} by {hit[2]}!")
        return Result(r[0].upper() + r[1:] + ".")

    # "open brave origin and search X on youtube and play the first video": the browser opens with the page anyway
    if (m := re.fullmatch(r"open(?: up)? (?:brave(?: origins?)?|(?:the |my )?browser) and (.+)", t)) \
            and (r := await _browser(m[1], tools)):
        return r
    # "open brave origin and look up X", "look up X in brave": Brave's search page, as if typed in the address bar
    if m := re.fullmatch(r"(?:open(?: up)? (?:brave(?: origins?)?|(?:the |my )?browser) and )(?:search|look up|google)"
                         r"(?: for| up)? (.+?)(?: in (?:brave|the browser))?|(?:search|look up|google)(?: for)? (.+?) "
                         r"in (?:brave(?: origins?)?|(?:the |my )?browser)", t):
        query = m[1] or m[2]
        if " and " in query:  # "… and open the first video": more than one thing, not all search words
            return None
        await tools.call("open", {"target": SEARCH["brave"] + quote_plus(query), "app": "Brave Origin"})
        return Result(f"Searching for {query}!")

    if m := re.fullmatch(r"(?:look up|search up)(?: for)? (.+?) (?:on|in) (youtube|amazon|google|reddit|github|twitch|steam|the aur|"
                         r"aur|duckduckgo|brave)|"
                         r"(?:search|find)(?: for)? (.+?) (?:on|in) (youtube|amazon|google|reddit|github|wikipedia|twitch|"
                         r"steam|the aur|aur|duckduckgo|brave)|(?:search )?(youtube|amazon|google|reddit|github|"
                         r"wikipedia|twitch|steam|the aur|aur|duckduckgo) (?:search )?for (.+)", t):
        query, site = (m[1], m[2]) if m[1] else (m[3], m[4]) if m[3] else (m[6], m[5])
        query = re.sub(r"^(?:me |us )?(?:some |a |an |the )?", "", query) or query
        await tools.call("open", {"target": SEARCH[site] + quote_plus(query)})
        name = {"youtube": "YouTube", "github": "GitHub", "duckduckgo": "DuckDuckGo", "aur": "the AUR",
                "the aur": "the AUR"}.get(site, site.title())
        return Result(f"Searching {name} for {query}!")

    if re.fullmatch(r"(?:skip|close) (?:the |this |that )?ads?|skip (?:the )?(?:youtube )?ad(?:vertisement)?", t):
        return await video("skip_ad", done="Skipped!")
    if re.fullmatch(r"(?:play |go to )?(?:the )?next video|skip (?:this|the) video", t):
        return await video("next", done="Next video!")
    if m := re.fullmatch(r"(?:set |change |put )?(?:the )?(?:video |playback )?speed (?:to )?(\d+(?:\.\d+)?|normal|double)"
                         r"(?: ?x| times)?|play (?:it |this |the video )?at (\d+(?:\.\d+)?) ?(?:x|times)(?: speed)?|"
                         r"(\d+(?:\.\d+)?) ?x(?: speed)?|(normal|double|half) speed", t):
        word = m[1] or m[2] or m[3] or m[4]
        rate = {"normal": 1, "double": 2, "half": 0.5}.get(word) or float(word)
        if 0.25 <= rate <= 4:
            return await video("speed", rate, done=f"{rate:g}x speed!" if rate != 1 else "Normal speed.")
    if m := re.fullmatch(r"(?:go back|rewind|skip back|jump back|back)(?: by)? (.+)|"
                         r"(?:skip|jump|go|fast forward|forward)(?: ahead| forward)?(?: by)? (.+)", t):
        secs = parse_duration(m[1] or m[2])
        if secs:
            sign = -1 if m[1] else 1
            from ..timers import say_duration

            return await video("seek", sign * secs, done=f"{'Back' if sign < 0 else 'Skipped'} {say_duration(secs)}.")

    if m := re.fullmatch(r"close (?:all )?(?:the |my )?(.+?) tabs?", t):
        what = m[1].strip()
        r = await tools.call("browser_tabs", {"action": "close", "query": "this" if what in ("this", "current", "that")
                                              else what})
        n = re.search(r"closed (\d+)", r)
        if n:
            return Result("Closed!" if n[1] == "1" else f"Closed {n[1]} tabs." if n[1] != "0" else
                          f"I couldn't find any {what} tabs.")
        return Result("Okay, I left them open." if "declined" in r else r.removeprefix("error: "))
    if re.fullmatch(r"close (?:this|the|that) tab", t):
        await tools.call("browser_tabs", {"action": "close", "query": "this"})
        return Result("Closed!")
    if re.fullmatch(r"(?:reopen|restore|bring back|undo close)(?: the| that| my)?(?: last)?(?: closed)? tab", t):
        r = await tools.call("browser_tabs", {"action": "reopen"})
        return Result("Brought it back!" if r.startswith("reopened") else r.removeprefix("error: "))
    if (m := re.fullmatch(r"(?:go to|switch to|show me|open|find) (?:my |the )?(.+?) tab", t)) \
            and not re.match(r"(a )?new\b|another\b", m[1]):
        r = await tools.call("browser_tabs", {"action": "switch", "query": m[1]})
        return Result("Here it is!" if r.startswith("switched") else
                      f"I couldn't find a {m[1]} tab." if r.startswith("no tab") else r.removeprefix("error: "))

    if m := re.fullmatch(r"scroll (up|down)(?: a bit)?|scroll to the (top|bottom)|(?:go|jump) to the (top|bottom)"
                         r"(?: of the page)?", t):
        r = await tools.call("browser_scroll", {"direction": m[1] or m[2] or m[3]})
        return Result("" if r.startswith("scrolled") else r.removeprefix("error: "))
    if m := re.fullmatch(r"click (?:on )?(?:the )?(.+)", t):
        r = await tools.call("browser_click", {"target": m[1]})
        return Result("Okay, I didn't click it." if "declined" in r else
                      "Clicked!" if r.startswith("clicked") else r.removeprefix("error: "))
    return None


UPDATE_WORDS = set("""can could you check if my pc computer system systems needs need any updates update for are there is
do does i have has new pending available waiting the to be updated see look search me whether package packages anything
got on upgrades upgrade what which how many arch cachyos linux it outdated up date please tell whats there's""".split())


async def _music(t: str, tools: ToolBox) -> Result | None:
    """Songs, playlists, liked songs and My Wave in Riff, the music player. YouTube/video requests never get here."""
    from .. import riff
    from ..games import find_game

    if not find_app("riff") or re.search(r"\b(youtube|yt|videos?|spotify)\b", t):
        return None
    on = riff.connected()
    t = re.sub(r" (?:on|in|with|using) riff$", "", t)

    async def go(action: str, query: str = "", shuffle: bool = False) -> Result:
        r = await tools.call("riff", {"action": action, **({"query": query} if query else {}),
                                      **({"shuffle": True} if shuffle else {})})
        if hit := re.match(r"(?:playing|now playing) “(.+?)”(?: by (.+?))?(?: in Riff|\.|$)", r):
            return Result(f"Playing {hit[1]}" + (f" by {hit[2]}!" if hit[2] else "!"))
        if hit := re.match(r"playing the playlist (.+?)(?: on shuffle)?, starting", r):
            return Result(f"Playing your {hit[1]} playlist{' on shuffle' if shuffle else ''}!")
        if r.startswith("playing your"):
            return Result(f"Playing your liked songs{' on shuffle' if shuffle else ''}!")
        if r.startswith("playing My Wave"):
            return Result("Starting My Wave!")
        if r.startswith("opened the playlist"):
            return Result(f"Here's your {r.split('playlist ', 1)[1].split(' (')[0]} playlist!")
        if m := re.match(r"no Riff playlist called '(.+?)'\. Playlists: (.+)", r):
            return Result(f"I couldn't find a playlist called {m[1]}. You have {re.sub(r' \(\d+ songs\)', '', m[2])}.")
        if r.startswith("Riff playlists: "):
            names = re.sub(r" \((\d+) songs\)", r" with \1 songs", r[16:])
            return Result("You don't have any playlists yet." if names == "none" else f"Your playlists: {names}.")
        if r.startswith(("liked ", "unliked ")):
            return Result("Liked it!" if r.startswith("liked") else "Okay, removed it from your liked songs.")
        if r.startswith("Riff is "):
            return Result(r[0].upper() + r[1:] + ".")
        r = r.removeprefix("Riff: ")
        return Result(r[0].upper() + r[1:] + ("" if r.endswith(".") else "."))

    shuffled = r"(?: (?:on shuffle|shuffled|in random order|randomly))?"
    if m := re.fullmatch(r"(play|shuffle|put on|start|listen to|open|show me|pull up|go to)(?: me)? (?:my |the )?"
                         r"(?:playlist (?:called |named )?(.+?)|(.+?) playlist)" + shuffled, t):
        name = m[2] or m[3]
        if m[1] in ("open", "show me", "pull up", "go to"):
            return await go("open_playlist", name)
        return await go("playlist", name, shuffle=m[1] == "shuffle" or "shuffle" in t or "random" in t)
    if re.fullmatch(r"(?:play|shuffle|put on|listen to|start)(?: me)? (?:my |the )?(?:liked|favou?rite|saved) "
                    r"(?:songs|tracks|music)" + shuffled, t):
        return await go("liked", shuffle=t.startswith("shuffle") or "shuffle" in t or "random" in t)
    if re.fullmatch(r"(?:play|start|put on|turn on)(?: me)? (?:my wave|the radio|riff radio)|play some music|"
                    r"put on some music|play something(?: good| for me| i like)?", t):
        return await go("wave")
    if re.fullmatch(r"(?:what|which) playlists do i have|(?:list|show me|tell me) my playlists|"
                    r"what are my playlists", t):
        return await go("playlists")
    if re.fullmatch(r"(?:like|heart|favou?rite) (?:this|the|that) (?:song|track)|i like this song|add (?:this|that) "
                    r"(?:song |track )?to (?:my )?(?:liked songs|likes|favou?rites)", t):
        return await go("like") if on else None
    if re.fullmatch(r"unlike (?:this|the|that) (?:song|track)|remove (?:this|that) (?:song |track )?from (?:my )?"
                    r"(?:liked songs|likes|favou?rites)", t):
        return await go("unlike") if on else None
    if m := re.fullmatch(r"(?:turn )?shuffle (on|off)|turn (on|off) (?:the )?shuffle", t):
        return await go(f"shuffle_{m[1] or m[2]}") if on else None
    if re.fullmatch(r"what(?:'s| is) (?:this|the|that) song(?: called)?|what song is (?:this|that|playing)|"
                    r"what(?:'s| is) playing(?: right now)?|who sings this(?: song)?|what am i listening to", t):
        return await go("status") if on else None
    if m := re.fullmatch(r"(?:play|put on|listen to|i want to (?:hear|listen to)|start playing|let me hear)(?: me)? "
                         r"(?:the song |the track |a song |some songs? |songs )?(?:(?:called|named|by) )?(.+)", t):
        query = re.sub(r"^(?:some|the|a) ", "", m[1])
        if re.fullmatch(r"(?:the )?(?:music|song|track|it|this|that|again|next|previous|something)", query) \
                or find_game(query, tools.launchers):
            return None
        return await go("play", query)
    return None


def _is_update_check(t: str) -> bool:
    """'can you check if my pc needs any updates', 'do I have updates', 'how many updates are there'."""
    words = re.findall(r"[a-z']+", t.replace("'s", ""))
    return (bool(re.search(r"\b(updates?|upgrades?|updated|outdated|up to date)\b", t))
            and not re.search(r"\b(install|run|start)\b|\bdo (?:the |all |my )?updates\b", t)
            and not re.match(r"update\b", t)  # "update my pc" means install
            and all(w in UPDATE_WORDS for w in words))


async def handle(text: str, tools: ToolBox, usage=None, last_reply: str = "") -> Result | None:
    t = _clean(text)
    if not t:
        return Result("", "stop")

    if re.fullmatch(r"(stop|cancel|never ?mind|nothing|shut up|be quiet|quiet)", t):
        return Result("", "stop")
    # "open it" / "play the first one" point at something from just before: the model has that context, the
    # app matcher doesn't ("it" → Orbit)
    if re.fullmatch(r"(?:open|launch|start|run|play|show me|pull up|watch) (?:it|them|that|this|those|these|"
                    r"the (?:first|top|second|third|last|next)(?: one| result| file| video| link)?)", t):
        return None
    if re.fullmatch(r"(hide|go away|bye|goodbye|bye bye|see you|that'?s all|thanks?,? that'?s all)", t):
        return Result(random.choice(["Bye bye!", "See you later!", "Call me anytime!"]), "hide")
    if re.fullmatch(r"(forget|clear) (everything|our conversation|the conversation|history)", t):
        return Result("Okay, fresh start!", "forget")
    if re.fullmatch(r"(thank you|thanks|thank you so much|thanks a lot)", t):
        return Result(random.choice(["You're welcome!", "Anytime!", "Happy to help!"]))

    if _is_update_check(t):
        from ..briefing import updates_line

        line = updates_line(await tools.call("system_updates", {}))
        if not line:
            return Result("Sorry, I couldn't check for updates right now.")
        return Result(line if "up to date" in line else line + " Want me to install them?")
    offered_updates = re.search(r"install them\?$", last_reply.strip())
    if (offered_updates and re.fullmatch(r"(?:yes|yeah|yep|yup|sure|ok|okay|please|do it|go ahead|go for it|"
                                         r"install (?:them|it)|of course)(?:,? (?:please|do it|go ahead|thanks?|"
                                         r"install them|sure))*", t)) or \
            re.fullmatch(r"(?:(?:yes|yeah|okay|ok|sure),? )?(?:install|do) (?:the |all |my )?(?:system )?updates?(?: now)?(?: for me)?(?: now)?|"
                    r"update (?:my |the )?(?:system|pc|computer)(?: now)?|(?:yes|yeah),? (?:install|update) (?:them|it)(?: now)?", t):
        result = await tools.call("install_updates", {})
        return Result("Opening a terminal for the update. Just confirm and type your password there!"
                      if result.startswith("Opened") else "Sorry, I couldn't open the update terminal.")

    if usage and re.search(r"\bclaude\b.*\b(requests?|usage|used|use|limit|spent|spend|cost|budget)\b|"
                           r"\b(how much|how many)\b.*\b(spent|spend|cost|requests?)\b", t):
        return Result(usage.summary())

    if tools.timers is not None and (r := await _timers(t, tools)):
        return r

    # time / date, optionally "in <place>": "what's the date and time", "what time is it in Tokyo"
    if len(t.split()) <= 14 and re.search(
            r"\b(what|tell me|check|look up|current|today'?s?|which)\b.*\b(time|date|day)\b|^(the )?(time|date)\b", t) \
            and not re.search(r"\b(release|come out|happen|start|open|close|weather|timer|alarm|remind)", t):
        place = re.search(r"\b(?:in|for) ((?!the morning|the evening)[a-z][a-z .'-]+?)(?: right now| now| today)?$", t)
        reply = clock.describe(place.group(1) if place else None,
                               want_time=bool(re.search(r"\btime\b", t)) or not re.search(r"\b(date|day)\b", t),
                               want_date=bool(re.search(r"\b(date|day)\b", t)))
        if reply:
            return Result(reply)

    # phone (KDE Connect)
    if re.fullmatch(r"(?:find|ring|where'?s|where is) my (?:phone|iphone)|make my (?:phone|iphone) ring", t):
        r = await tools.call("phone", {"action": "ring"})
        return Result("Ringing your phone!" if r.startswith("ringing") else r)
    if re.search(r"\b(phone|iphone)('?s)? battery\b|\bbattery (on|of) my (phone|iphone)\b", t):
        r = await tools.call("phone", {"action": "battery"})
        return Result(re.sub(r"^.*? battery: ", "Your phone is at ", r) if "battery:" in r else r)

    # morning briefing (the daemon builds it)
    if re.fullmatch(r"(good )?morning( mio)?|(give me |what'?s )?(my |the )?(morning )?briefing|"
                    r"what did i miss|catch me up", t):
        return Result("", "briefing")

    # instant replay: "clip that", "clip the last 2 minutes", "save a replay"
    if re.fullmatch(r"(?:clip|klip|kip|clipp|clips|clip it|record|save) (?:that|this|it|dat|dead)(?: for me)?|(?:save|make|take) (?:a |the )?(?:clip|replay)"
                    r"(?: of (?:that|this))?|clip it|clip|clip the last .+|save the last .+", t):
        from ..timers import parse_duration, say_duration as tm_say

        secs = parse_duration(re.sub(r"\blast (minute|hour)\b", r"last 1 \1", t)) if re.search(r"\blast\b", t) else None
        r = await tools.call("clip", {"seconds": secs} if secs else {})
        if r.startswith("saved"):
            got = re.search(r"last (\d+) seconds", r)
            if secs and got and int(got[1]) < secs - 5:
                return Result(f"Saved, but I only had the last {tm_say(int(got[1]))} recorded.")
            return Result(random.choice(["Clipped!", "Got it, clip saved!", "Saved that one!"]))
        return Result(r[0].upper() + r[1:] + ".")

    if r := await _browser(t, tools):
        return r
    if r := await _music(t, tools):
        return r

    # game updates and Steam prices: "did roblox update", "is elden ring on sale", "anything on my wishlist on sale"
    if m := re.fullmatch(r"(?:did|has|have) ([\w .:'-]+?) (?:get |gotten |got |had )?(?:an? |any )?(?:new )?"
                         r"(?:update|updated|patch|patched|updates|patches)(?: recently| lately| today| yet)?|"
                         r"(?:when (?:was|did) )([\w .:'-]+?)(?:'s)? (?:last )?(?:update|updated|patched|patch)", t):
        from .. import gamenews

        game = (m[1] or m[2]).strip()
        if gamenews.ROBLOX.search(game):
            return Result(await tools.call("game_news", {"game": game}))
        notes = await tools.call("game_news", {"game": game})
        if hit := re.search(r"^\* (.+?) — (.+)$", notes, re.M):
            title = notes.split(" — ", 1)[0]
            return Result(f"{title}'s latest update is “{hit[1]}” from {hit[2]}. Want me to go through the patch notes?")
        return Result(notes)
    if re.search(r"\bwishlist\b", t) and re.search(r"\b(sale|sales|deals?|discount|cheaper|cheap)\b", t):
        return Result(await tools.call("game_price", {"game": "wishlist"}))
    if re.fullmatch(r"(?:are there |any |what are (?:the )?|show me (?:the )?|what'?s )?(?:any )?(?:good )?(?:steam )?"
                    r"(?:deals|sales|specials|on sale)(?: on steam)?(?: right now| today| at the moment)?", t):
        return Result(await tools.call("game_price", {"game": "deals"}))
    if m := re.fullmatch(r"(?:is|are) ([\w .:'-]+?) (?:on sale|discounted|cheaper)(?: on steam)?(?: right now| today)?|"
                         r"(?:how much (?:is|does|do)|what(?:'s| is) the price (?:of|for)) ([\w .:'-]+?) "
                         r"(?:cost )?on steam", t):
        return Result(await tools.call("game_price", {"game": (m[1] or m[2]).strip()}))

    # games: "launch sober via froststrap", "play roblox", "open my steam library", "start rocket league"
    if re.fullmatch(r"(?:open|show)(?: me)? (?:my |the )?steam(?: game)? library|(?:open )?my (?:steam )?games", t):
        await tools.call("steam_library", {})
        return Result("Here's your Steam library!")
    if m := re.fullmatch(r"(?:launch|start|play|open|run|boot up) (?:the game )?(?:some )?([\w .:'-]+?)"
                         r"(?: (?:via|through|with|using|in) (?:froststrap|steam|sober))?(?: for me)?", t):
        from ..games import find_game

        hit = find_game(m[1], tools.launchers)
        if hit and not (find_app(m[1]) and re.sub(r"\W", "", m[1]) in re.sub(r"\W", "", find_app(m[1])["name"].lower())
                        and hit[1][0] == "steam"):
            await tools.call("launch_game", {"name": m[1]})
            return Result(f"Starting {hit[0]}, have fun!")

    # "open youtube", "open github.com in brave origin", "open the youtube website in my browser"
    if m := re.fullmatch(r"(?:open|go to|show me|pull up)(?: me)? (?:the |my )?(?:official )?([\w .+/-]+?)"
                         r"(?: website| site| page| homepage)?(?: in (?:my |the )?([\w .-]+?))?", t):
        target, browser = m[1].strip(), (m[2] or "").strip()
        browser = "" if browser in ("browser", "default browser", "web browser") else browser
        url = to_url(target)
        app = find_app(target)
        is_app = app is not None and re.sub(r"\W", "", target) in re.sub(r"\W", "", app["name"].lower())
        if url and (browser or "." in target or not is_app):
            result = await tools.call("open", {"target": url, **({"app": browser} if browser else {})})
            if result.startswith("opened"):
                return Result(f"Opening {target}!")
    if m := re.fullmatch(r"(?:open|launch|start|run) (?:the |my )?([\w .+-]+?)(?: app)?", t):
        app = find_app(m[1])
        if app:
            await tools.call("launch_app", {"name": m[1]})
            return Result(f"Opening {app['name']}!")
        return None  # let the LLM figure out what was meant

    if m := re.fullmatch(r"(?:go to|switch to|show) workspace (\w+)", t):
        if (n := _num(m[1])) is not None:
            await tools.call("niri_action", {"action": "focus-workspace", "arg": str(n)})
            return Result(f"Workspace {n}.")
    if m := re.fullmatch(r"move (?:this |the )?window to workspace (\w+)", t):
        if (n := _num(m[1])) is not None:
            await tools.call("niri_action", {"action": "move-window-to-workspace", "arg": str(n)})
            return Result(f"Moved it to workspace {n}.")
    if re.fullmatch(r"close (?:this |the )?window", t):
        await tools.call("niri_action", {"action": "close-window"})
        return Result("Closed.")
    if re.fullmatch(r"(?:show |open |toggle )?(?:the )?overview", t):
        await tools.call("niri_action", {"action": "toggle-overview"})
        return Result("Here you go.")
    if re.fullmatch(r"(?:make (?:it|this|the window) )?full ?screen", t):
        await tools.call("niri_action", {"action": "fullscreen-window"})
        return Result("Fullscreen!")
    if re.fullmatch(r"take a screenshot|screenshot", t):
        await tools.call("niri_action", {"action": "screenshot-screen"})
        return Result("Got it, screenshot saved.")

    if m := re.fullmatch(r"(?:set )?(?:the )?volume (?:to )?(\w+)(?: percent| %)?", t):
        if (n := _num(m[1])) is not None:
            await tools.call("volume", {"mode": "set", "value": n})
            return Result(f"Volume {n} percent.")
    if re.fullmatch(r"(?:turn (?:it|the volume) up|volume up|louder)", t):
        await tools.call("volume", {"mode": "up", "value": 10})
        return Result("Louder!")
    if re.fullmatch(r"(?:turn (?:it|the volume) down|volume down|quieter|softer)", t):
        await tools.call("volume", {"mode": "down", "value": 10})
        return Result("A bit quieter.")
    if re.fullmatch(r"(?:un)?mute(?: (?:the )?(?:sound|audio|volume))?", t):
        await tools.call("volume", {"mode": "mute"})
        return Result("Okay.")

    if re.fullmatch(r"(?:next|skip)(?: (?:song|track))?|skip (?:this|it)", t):
        await tools.call("media", {"action": "next"})
        return Result("Next one!")
    if re.fullmatch(r"(?:previous|last)(?: song| track)|go back a song", t):
        await tools.call("media", {"action": "previous"})
        return Result("Going back.")
    if re.fullmatch(r"(?:pause|resume|play)(?: (?:the )?(?:music|song|video|it))?", t):
        await tools.call("media", {"action": "play-pause"})
        return Result("Okay!")
    return None
