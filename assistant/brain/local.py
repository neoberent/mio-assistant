"""Local brain via Ollama: handles requests with tool calling, escalates what it can't do.

Small models get confused by long tool lists, so each request only sees the tools of the
categories it looks like (plus the ones used earlier in the session), and a one-line catalog of
all the others, which it may also call. It thinks (qwen3 thinking mode) before picking a tool unless
it's small talk. Anything that fails gets a second, deeper local attempt before Claude.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

import httpx

from .. import clock
from ..learning import ALIASES, NEVER_KEEP, example_line
from ..tools import ToolBox

log = logging.getLogger("local")

ESCALATE = "ask_claude"

# category → (trigger regex, tools)
CATEGORIES: dict[str, tuple[str, list[str]]] = {
    # first: its words are specific, and topics after them ("…that i like lofi music") would fill MAX_TOOLS
    "memory": (r"\b(remember|note|don'?t forget|keep in mind)\b", ["remember", "notify"]),
    "music": (r"\b(music|songs?|tracks?|lofi|playlists?|riff|album|artist|singer|band|listen(ing)? to|put on|"
              r"liked songs|my wave|shuffle|hear|in the mood for)\b", ["riff", "media", "volume"]),
    "youtube": (r"\b(youtube|yt|video|channel|youtuber|stream)\b",
                ["youtube_play", "browser_video", "search_site"]),
    "browser": (r"\b(page|article|site|website|tab|browser|brave|video|click|scroll|history|visited|"
                r"looked at|summari[sz]e|reading|watching|first (result|link|one)|this (page|video|article|post))\b",
                ["browser_page", "browser_video", "browser_tabs", "browser_click", "type_text", "browser_scroll",
                 "browser_history", "search_site", "open_best_match"]),
    "apps": (r"\b(open|launch|start|run|close|focus|switch|window|workspace|app|fullscreen|overview|monitor|"
             r"screenshot|float|maximi[sz]e|type|running)\b",
             ["launch_app", "open", "list_windows", "focus_window", "niri_action", "type_text"]),
    "games": (r"\b(game|games|play|roblox|sober|froststrap|steam|launch|patch|patch ?notes|sale|deals?|"
              r"wishlist|discount|price|cost|clip|replay|record)\b",
              ["launch_game", "steam_library", "game_news", "game_price", "clip"]),
    "phone": (r"\b(phone|iphone|mobile|handy)\b", ["phone", "clipboard"]),
    "media": (r"\b(volume|louder|quieter|mute|music|song|track|play|pause|skip|spotify|playing)\b",
              ["volume", "media", "riff"]),
    "desktop": (r"\b(night ?light|do not disturb|dnd|wallpaper|theme|dark mode|light mode|bluetooth|wi-?fi|"
                r"caffeine|control cent(er|re)|panel|launcher|noctalia|bar|dock|notification)\b",
                ["shell_panel", "notify"]),
    "web": (r"\b(search|look ?up|google|find out|internet|online|web|news|latest|newest|release|when (is|does|will)|"
            r"who (is|was)|price|cost|review|link|url|website|site|page|github|youtube|reddit)\b",
            ["web_research", "read_link", "read_clipboard_link", "open", "open_best_match", "search_site"]),
    "screen": (r"\b(screen|this|window|terminal|error|clipboard|copied|pasted|reading|says|see|look|picture|"
               r"image|showing)\b",
               ["screen", "clipboard", "read_clipboard_link"]),
    "packages": (r"\b(package|install|update|upgrade|uninstall|remove|aur|paru|yay|pacman|safe|trust|pkgbuild|"
                 r"arch news|cachyos|kernel|driver|outdated|up to date)\b",
                 ["package_info", "check_install", "system_updates", "install_updates", "install_packages",
                  "arch_news"]),
    "knowledge": (r"\b(wiki|wikipedia|arch ?wiki|how (do|can|to)|what is|what are|explain|meaning)\b",
                  ["arch_wiki", "wikipedia", "web_research", "search_my_setup"]),
    "setup": (r"\b(my (config|setup|shortcut|keybind|bind|hotkey|alias|function)|shortcut|keybind|hotkey|"
              r"config|settings?|your (voice|name|settings)|mio|kitty|niri|fish)\b",
              ["search_my_setup"]),
    "system": (r"\b(health|temperature|temp|hot|cpu|gpu|ram|memory|disk|space|uptime|load|failed|errors?|"
               r"fans?|sensors?)\b",
               ["system_health", "system_info", "run_shell"]),
    "shell": (r"\b(command|run|kill|process|service|ip|network|ping|internet|connection|git|how many|how big|"
              r"size|biggest|largest|version|installed|usb|mount|drive|battery|bluetooth device|port|log|journal|"
              r"restart|uptime|wifi|ethernet|lock|sleep|suspend|shut ?down|reboot|log ?out)\b",
              ["run_shell"]),
    "files": (r"\b(files?|folders?|directory|downloads|documents|pictures|music|videos|desktop|pdf|photo|image|"
              r"screenshot|tidy|clean up|organi[sz]e|sort|trash|delete|rename|move)\b",
              ["find_files", "list_files", "open", "tidy_downloads", "move_file", "trash"]),
    "keys": (r"\b(press|shortcut|hotkey|keys?|ctrl|control|alt|enter|escape|tab|type)\b",
             ["press_keys", "focus_window", "type_text"]),
    "discord": (r"\b(discord|dm|dms|server|channel|message|text (him|her|them)|tell (him|her|them)|tell \w+ (that|on))\b",
                ["discord", "focus_window"]),
    "routines": (r"\b(routines?|when i say|whenever i say|every time i say)\b", ["routine"]),
    "misc": (r"\b(weather|rain|umbrella|snow|sunny|cold|warm|jacket|temperature outside|forecast|time|date|clock)\b",
             ["weather", "world_clock"]),
    "timers": (r"\b(timers?|remind(er)?s?|alarms?|wake me|minutes?|hours?|left|later|tomorrow|tonight)\b",
               ["timer"]),
}
# merged into one clear tool each for her (Claude still gets all of them): screen, type_text
HIDDEN = set(ALIASES)
DEFAULT_TOOLS = ["web_research", "open", "launch_app", "run_shell", "search_my_setup", "weather"]
MAX_TOOLS = 10
# the second, deeper attempt also gets these
DEEP_TOOLS = ["run_shell", "web_research", "browser_page", "open"]

# real programming/writing jobs: the 8B model writes broken (even destructive) scripts, so these go to Claude
CODING = re.compile(r"\b(write|create|make|code|program|build|fix|debug|refactor)\b.{0,40}\b(script|code|program|"
                    r"function|class|app|bot|website|essay|letter|email|story|poem|cover letter)\b", re.I)

# small talk: no tools needed, no thinking needed
CHAT = re.compile(r"^\W*(hi|hello|hey|yo|heya|what'?s up|sup|how are you( doing)?|how'?s it going|good (night|evening|"
                  r"afternoon)|(i )?love (you|u)|miss(ed)? you|bravo|cheers|nice|cool|great|awesome|lol|haha|ok(ay)?|"
                  r"wow|thanks?( you)?|you'?re (cute|the best|amazing|smart|funny)|good (girl|job)|who are you|"
                  r"what'?s your name)\b[\w ,!.?']{0,40}$", re.I)

# live facts the model must never answer from memory
LIVE = re.compile(r"\b(weather|rain|umbrella|snow|sunny|forecast|jacket|cold outside|warm outside|hot outside)\b", re.I)
# arguments that are words to send/type for the user: only what the user actually said
SAID_ARGS = {("discord", "text"), ("phone", "text"), ("type_text", "text"), ("browser_type", "text")}

HARD = re.compile(r"\b(why|explain|compare|comparison|difference|differences|should i|which (is|one)|pros and cons|"
                  r"recommend|better|best way|step by step|plan|analy[sz]e|summari[sz]e|troubleshoot|figure out)\b",
                  re.I)

EXAMPLES = """Examples of good tool use:
- "open reddit in brave origin" → open(target="reddit", app="Brave Origin")
- "is discord running?" → list_windows, then answer
- "what's the newest kernel version?" → web_research(query="latest Linux kernel version")
- "turn on night light" → shell_panel(command="nightlight-toggle")
- "is paru -S foo safe?" / "is this safe to install?" → check_install
- "what's my screenshot shortcut?" → search_my_setup(query="screenshot keybind")
- "what does this error mean?" / "explain my terminal" → screen(question="what does this error mean?"), then explain briefly
- "how do I enable bluetooth on arch?" → arch_wiki(query="Bluetooth")
- "what's in the latest Elden Ring patch?" → game_news(game="Elden Ring"), then summarize the main changes
- "summarize this page" / "what does this article say?" → browser_page, then answer from it
- "what's this video about?" → browser_video(action="about"), then summarize the transcript in 2-3 sentences
- "find that recipe I looked at yesterday" → browser_history(query="recipe", days=2)
- "remind me to stretch in 45 minutes" → timer(action="set", minutes=45, label="stretch")
- "do I need updates?" / "check my pc for updates" → system_updates, then say how many and the notable ones
- "play lofi music on youtube" / "open Asian maps from Rohat on YouTube" → youtube_play(query="Asian maps Rohat")
- "search cat videos on youtube and open the first one" → youtube_play(query="cat videos")
- "look up Rohaze asian maps on youtube" / "search lofi on youtube" → search_site(site="youtube", query="Rohaze asian maps") — "look up"/"search" shows results; youtube_play only when they say play/watch/open/put on
- "how many packages do I have?" → run_shell(command="pacman -Q | wc -l")
- "what's my IP?" → run_shell(command="ip -brief addr")
- "kill discord" → run_shell(command="pkill -i discord")
- "what's using the most memory?" → run_shell(command="ps -eo comm,%mem --sort=-%mem | head -5")
- "put on some piano music" / "play me some jazz" / "play blinding lights by the weeknd" → riff(action="play", query="piano music") — songs and music go to Riff (YouTube only when they say YouTube or video); "play <something>" always searches it, even while music is playing; media(action="play-pause") is only for "pause", "resume" or a bare "play"
- "shuffle my gym playlist" → riff(action="playlist", query="gym", shuffle=true); "open my chill playlist" → riff(action="open_playlist", query="chill"); "play my liked songs" → riff(action="liked")
- "what song is this?" → riff(action="status"); "skip this song" → riff(action="next"); "like this song" → riff(action="like")
- "open the github page of niri" → open_best_match(query="niri github")
- "how much RAM is brave using?" → system_info, then read the 'RAM by app' line
- "how hot is my gpu?" / "cpu temperature?" → system_health, then give the temperature
- "is bluetooth on?" → shell_panel(command="bluetooth-status"); a question only checks, it never toggles anything
- "turn on do not disturb" → shell_panel(command="notification-dnd-set on")
- "make it quieter" → volume(mode="down", value=10)
- "where's my tax pdf?" → find_files(query="tax", kind="pdf"); "the pdf I downloaded yesterday" → find_files(kind="pdf", days=2)
- "clean up my downloads" → tidy_downloads (shows the plan), then tidy_downloads(apply=true) if they say yes
- "delete that file" → trash(path=...) (recoverable), never rm -rf
- "delete everything in my downloads" / "yes, delete them" after a Downloads plan → trash(path="~/Downloads", inside=true): the files, never the folder
- "open my DM with Alex on discord" / "message Alex on discord" → discord(action="open", who="Alex"), then ask what to send
- "tell him <words>" (Discord open) → discord(action="send", text="<exactly the words the user said>")
- "open a new tab in kitty" → press_keys(keys="ctrl+shift+t", app="kitty"); "close this tab in kitty" → press_keys(keys="ctrl+shift+w", app="kitty")
- "switch to brave" / "go to discord" (an app, not a tab) → focus_window(query="brave")
- "open my downloads folder" → open(target="~/Downloads")
- "it's way too loud" → volume(mode="down", value=20), not mute
- "what's in this picture?" / "what game is this?" / "what am I looking at?" → screen(question=...)
- "type minecraft shaders in the search bar and press enter" → type_text(text="minecraft shaders", submit=true)
- "find my roblox clips" → find_files(query="roblox", kind="video"); "what's in my clips folder?" → list_files(path="~/Videos/Clips")
- "what routines do I have?" → routine(action="list")
- "yes" after you offered something → do exactly what you offered, now."""

HINT = ("Speech recognition sometimes mishears words (names, games, channels). Work out what the user most likely "
        "meant and act on that; search tools are forgiving with misspellings. Think briefly about which tool does "
        "it, then call it. The 'Right now' line says what is open and focused: use it for 'this', 'them', 'this game', 'skip "
        "this'. Prefer doing over asking, except when a request that changes or closes something could mean two "
        "different things and nothing says which ('close it': the tab or the window?): then ask one short either/or "
        "question. After a tool ran, answer with its result, never 'let me "
        "check'. Never answer with an empty message.")

# a request with several parts: worth a quick check that every part got done
PARTS = re.compile(r"\b(and|then|also|after that|plus)\b|,", re.I)
SPLIT = re.compile(r",\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+|also\s+)?|\s+then\s+|\s+after that\s+|"
                   r"\s+plus\s+", re.I)
# parts that aren't separate jobs: "…and open the first one" (same search), "…and the time" (she knows it)
FOLLOWS = re.compile(r"^(?:(?:open|play|click|start|watch|show me) )?(?:it|them|that|this|the (?:first|top|second|"
                     r"next)(?: one| result| video| link)?)$|^(?:what'?s |what is |tell me )?(?:the )?(?:time|date|"
                     r"day)(?: is it)?(?: now| today)?$", re.I)
ACTS = re.compile(r"^(open|close|launch|start|play|put|set|turn|make|mute|unmute|pause|resume|skip|tell|show|search|"
                  r"find|send|clip|take|move|go|switch|check|copy|read|lower|raise|stop|remind|text|message|what|"
                  r"how|is|are|who|when|where|give|run|kill|install|update|delete|empty|save|lock)\b", re.I)
# anything that asks her to do or find something (else it's just talk)
REQUEST = re.compile(r"\b(open|close|launch|start|play|put|set|turn|make|mute|unmute|pause|skip|tell|show|search|find|"
                     r"send|clip|take|move|go|switch|check|copy|read|lower|raise|stop|remind|text|message|give|run|"
                     r"kill|install|update|delete|empty|save|lock|look|can you|could you|would you|please|help|"
                     r"what|who|when|where|why|how|which|is there|are there)\b|\?", re.I)
# words that say nothing about which call did a part
FILLER = {"the", "and", "then", "please", "can", "you", "could", "for", "tell", "open", "put", "set", "make", "turn",
          "start", "launch", "play", "show", "give", "get", "some", "this", "that", "with", "what", "what's", "how",
          "much", "many", "also", "too", "all", "everything", "mio", "hey", "now", "it's", "its", "there", "any"}
# a question about the world (not the PC, not "my …"): she can research it herself before giving up
WORLD_Q = re.compile(r"^\W*(who|what|when|where|why|how|which|is|are|was|were|does|did|will|can)\b", re.I)
PERSONAL = re.compile(r"\b(my|mine|me|i|this|that|these|it|here|screen|window|pc|computer|folder|file|tab|"
                      r"discord|terminal|you|your)\b", re.I)

LEARNED = ("These requests of the user worked before like this (a request like the current one most likely wants "
           "the same tools; take the arguments from what the user says now):\n")


class Escalate(Exception):
    pass


def _loose(pattern: str) -> str:
    """'\\b(update|…)\\b' → also matches 'updates', 'updated', 'installing'."""
    return pattern[:-3] + r")(?:e?s|e?d|ing)?\b" if pattern.endswith(r")\b") else pattern


LOOSE = {k: (re.compile(_loose(p)), tools) for k, (p, tools) in CATEGORIES.items()}


def pick_tools(text: str, previous: list[str]) -> list[str]:
    t = text.lower()
    chosen: list[str] = []
    for _, (pattern, tools) in LOOSE.items():
        if pattern.search(t):
            chosen += [x for x in tools if x not in chosen]
    for name in previous:  # follow-ups ("open it", "check again") reuse what was just used
        if name not in chosen:
            chosen.append(name)
    if len(chosen) < 3:
        chosen += [x for x in DEFAULT_TOOLS if x not in chosen]
    chosen = chosen[:MAX_TOOLS]
    if "run_shell" not in chosen:  # the way to do anything nothing else covers
        chosen.append("run_shell")
    return chosen


class LocalBrain:
    def __init__(self, cfg: dict, persona: dict, tools: ToolBox):
        self.cfg = cfg
        self.persona = persona
        self.tools = tools
        self.client = httpx.AsyncClient(base_url=cfg["ollama_url"], timeout=120)
        self.replayed = False  # the last reply came from a replayed skill
        self.asked = False     # the last reply is a question back to the user (what did they mean?)

    def _tool_specs(self, names: list[str]) -> list[dict]:
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.schema()}}
                 for t in self.tools.specs() if t.name in names and t.name not in HIDDEN]
        specs.append({"type": "function", "function": {
            "name": ESCALATE,
            "description": "Last resort: hand the request to a bigger assistant. Only for long code/writing, or "
                           "after you really tried your tools (including run_shell) and none can do it.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": []}}})
        return specs

    async def available(self) -> bool:
        try:
            return (await self.client.get("/api/version", timeout=2)).status_code == 200
        except httpx.HTTPError:
            return False

    async def preload(self):
        """Load the model into VRAM while the user is still talking (no-op if already loaded)."""
        try:
            await self.client.post("/api/generate", json={"model": self.cfg["ollama_model"],
                                                          "keep_alive": self.cfg.get("ollama_keep_alive", "5m")})
        except httpx.HTTPError:
            pass

    async def unload(self):
        try:
            await self.client.post("/api/generate", json={"model": self.cfg["ollama_model"], "keep_alive": 0})
        except httpx.HTTPError:
            pass

    async def _chat(self, messages: list[dict], tools: list[dict] | None = None, think: bool = False) -> dict:
        body = {"model": self.cfg["ollama_model"], "messages": messages, "stream": think, "think": think,
                "keep_alive": self.cfg.get("ollama_keep_alive", "5m"),
                "options": {"num_ctx": 8192, "temperature": 0.4, "num_predict": 1500 if think else 250}}
        if tools:
            body["tools"] = tools
        if not think:
            r = await self.client.post("/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
            log.debug("chat %.1fs, %s tokens", data.get("total_duration", 0) / 1e9, data.get("eval_count"))
            return data["message"]
        # thinking is streamed so it can be cut off: qwen3 sometimes deliberates for 20 s over a simple request
        budget = self.cfg.get("think_budget", 350)  # tokens, ~4-5 s
        msg: dict = {"role": "assistant", "content": "", "thinking": ""}
        steps = 0
        async with self.client.stream("POST", "/api/chat", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                part = json.loads(line).get("message", {})
                msg["thinking"] += part.get("thinking") or ""
                msg["content"] += part.get("content") or ""
                if part.get("tool_calls"):
                    msg["tool_calls"] = msg.get("tool_calls", []) + part["tool_calls"]
                steps += bool(part.get("thinking"))
                if steps > budget and not msg["content"] and not msg.get("tool_calls"):
                    break  # leaving the block closes the stream and Ollama stops generating
        log.debug("chat (thinking) %d think tokens, %s", steps, "cut off" if steps > budget else "done")
        if steps > budget and not msg["content"] and not msg.get("tool_calls"):
            # act on the thoughts so far, without thinking further
            plan = msg["thinking"][-1500:]
            out = await self._chat(messages + [{"role": "system", "content": "Your thoughts so far (act on them "
                                                f"now, call the tool or answer):\n{plan}"}], tools, False)
            out["thinking"] = out.get("thinking") or plan  # lets the caller see which tools it had in mind
            return out
        return msg

    async def _structured(self, system: str, user: str, schema: dict, n: int = 200) -> dict:
        """One quick call with a JSON answer (no thinking, ~0.5 s)."""
        body = {"model": self.cfg["ollama_model"], "stream": False, "think": False,
                "keep_alive": self.cfg.get("ollama_keep_alive", "5m"), "format": schema,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"num_ctx": 4096, "temperature": 0, "num_predict": n}}
        try:
            r = await self.client.post("/api/chat", json=body)
            r.raise_for_status()
            return json.loads(r.json()["message"]["content"])
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            log.info("structured call failed: %s", e)
            return {}

    def check(self, text: str, results: list[tuple[str, str]]) -> str:
        """Did the tool calls do every part of the request? results: (call with its arguments, result), and
        ("her reply", …) for parts the fast path did. Each part needs its own call: one that names its words
        ("steam" → launch_app(Steam)), else one of the tools its words point to ("louder" → volume). A failed
        call doesn't count. → the first part not done (in the user's words), or ''. No model call: ~0 ms."""
        parts = [x.strip(" .,!?") for x in SPLIT.split(text) if x and x.strip(" .,!?")]
        parts = [x for x in parts if not FOLLOWS.match(x)]
        if len(parts) < 2:
            return ""
        calls = [(c.split("(")[0], f"{c} {r}".lower()) for c, r in results if not NEVER_KEEP.match(r)]
        named: set[int] = set()
        todo = []
        for part in parts:  # first: calls that name the part's own words ("rock and roll" is one call, fine)
            words = [w for w in re.findall(r"[a-z0-9]+", part.lower()) if len(w) > 2 and w not in FILLER]
            hits = {i for i, (_, said) in enumerate(calls) if any(w in said for w in words)}
            if hits:
                named |= hits
            else:
                todo.append(part)
        free = [i for i in range(len(calls)) if i not in named]
        for part in todo:  # then: a call of a tool this part points to, that no other part claimed
            want = {t for pat, tools in LOOSE.values() if pat.search(part.lower()) for t in tools}
            if not want:
                continue  # can't tell what it needs: don't guess
            hit = next((i for i in free if calls[i][0] in want), None)
            if hit is None:
                i = parts.index(part)  # "open discord and steam" → "open steam"
                return part if i == 0 or ACTS.match(part) else f"{parts[i - 1].split()[0]} {part}"
            free.remove(hit)
        return ""

    async def _research(self, text: str, context: str, history: list[dict] | None) -> str:
        """A question about the world she couldn't handle with her tools: search the web herself and answer."""
        log.info("researching it herself before asking Claude")
        result = await self.tools.call("web_research", {"query": text})
        if NEVER_KEEP.match(result) or len(result) < 80:
            return ""
        system = (f"{self.persona['style']}\nAnswer the user's question from these web results in 2-3 short "
                  f"sentences, in English (also when the results are in another language), and name the sites. If they "
                  f"don't answer it, say exactly: NO ANSWER.\n"
                  f"{clock.now_line()}\n\nWeb results:\n{result[:5000]}")
        msg = await self._chat([{"role": "system", "content": system}, *(history or [])[-4:],
                                {"role": "user", "content": text}], None, False)
        reply = clean_reply((msg.get("content") or "").strip())
        return "" if not reply or "NO ANSWER" in reply.upper() or foreign(reply) else reply

    async def paraphrase(self, text: str, calls: list[dict]) -> list[str]:
        """Other ways the user might say this request (for practice), keeping every name and number."""
        did = ", ".join(c["name"] for c in calls)
        out = await self._structured(
            "Write 4 other ways a young gamer might say this request to a voice assistant: one casual, one very "
            "short, one with a typo or two (just misspell words, no notes about it), one with different words. Same meaning. Keep every name, number and "
            "title exactly as it is. Lowercase is fine.",
            f"Request: {text}\n(It was done with: {did})",
            {"type": "object", "properties": {"ways": {"type": "array", "items": {"type": "string"}}},
             "required": ["ways"]}, 200)
        ways = [re.sub(r"\s*[(\[]?\b(with )?typos?\b( allowed| included| added)?[)\]]?\s*$", "", w, flags=re.I).strip()
                for w in out.get("ways", []) if isinstance(w, str)]
        return [w for w in ways if 3 < len(w) < 150][:4]

    async def _small_talk(self, text: str, context: str, history: list[dict] | None) -> str:
        """Not a request at all: answer like a friend, no tools, no Claude."""
        system = (f"{self.persona['style']}\nThe user is just chatting, not asking for anything. Reply warmly in one "
                  "or two short sentences. Don't offer or claim to do anything.\n" + clock.now_line())
        msg = await self._chat([{"role": "system", "content": system}, *(history or [])[-6:],
                                {"role": "user", "content": text}], None, False)
        return clean_reply((msg.get("content") or "").strip())

    async def clarify(self, text: str, history: list[dict] | None, tools: list[str] | None = None,
                      context: str = "") -> str:
        """Instead of guessing (or handing it to Claude): one short question about what they mean."""
        offered = [t for t in self.tools.specs() if t.name in pick_tools(text, tools or []) and t.name not in HIDDEN]
        menu = "\n".join(f"- {t.name}: {re.split(r'(?<=[.!?]) ', t.description)[0][:90]}" for t in offered[:8])
        said = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in (history or [])[-4:])
        out = await self._structured(
            "You are Mio, a voice assistant on the user's PC. You couldn't work out what the user wants. Ask ONE "
            "short, friendly question (max 20 words) that would let you do it: if it could mean two things, offer "
            "them as an either/or in plain words (never tool names). If it isn't a request at all, set question "
            "to an empty string.",
            (f"Conversation so far:\n{said}\n" if said else "") + f"User now: {text}\nThings you can do:\n{menu}" +
            "".join(f"\n{line}" for line in context.splitlines() if line.startswith("Right now")),
            {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}, 80)
        q = clean_reply(re.sub(r"\s*\([^)]*\)\s*$", "", str(out.get("question") or "")).strip())
        if q.endswith("?") and 8 < len(q) < 200:
            return q
        # a short, vague request ("close it") is no job for Claude either: it can't know what was meant
        return "What should I do exactly?" if len(text.split()) <= 6 and not WORLD_Q.match(text) else ""

    async def plan(self, text: str) -> list[str]:
        """Split a request with several parts into short standalone requests, in order (no thinking, ~1 s)."""
        system = ("Split the user's request into the separate things to do, in order. Each step is a short, complete "
                  "request in the user's own words that makes sense on its own: replace 'it', 'that', 'them' with "
                  "what they refer to. Keep names and numbers exactly. Never add steps the user didn't ask for. "
                  "If it is really just one thing, return one step.")
        body = {"model": self.cfg["ollama_model"], "stream": False, "think": False,
                "keep_alive": self.cfg.get("ollama_keep_alive", "5m"),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                "format": {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
                           "required": ["steps"]},
                "options": {"num_ctx": 4096, "temperature": 0, "num_predict": 300}}
        try:
            r = await self.client.post("/api/chat", json=body)
            r.raise_for_status()
            steps = json.loads(r.json()["message"]["content"]).get("steps", [])
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            log.info("planning failed: %s", e)
            return [text]
        steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()][:8]
        return steps or [text]

    async def browser_step(self, text: str, site: str = "") -> dict | None:
        """One part of a browser task the patterns didn't read → {"op": search|pick|click|type|scroll|play, …}, or
        None for anything else (no thinking, ~1 s). The caller checks the words are the user's own."""
        system = ("This is one step of something the user wants done in their web browser" +
                  (f" (they are on {site})" if site else "") + ". Say which action it is: search (query, site if "
                  "named: youtube, reddit, google, amazon, github, twitch, steam, tiktok, or empty), pick (open a "
                  "result from the list on the page: nth = first/second/third/last, kind = video or result), click "
                  "(a button or link by its words: target), type (text into the page's box; submit true if they "
                  "want it sent/searched), scroll (direction up/down/top/bottom), play (a YouTube video by "
                  "description: query). Use the user's own words for query/target/text, leave out filler like "
                  "'that', 'some', 'video of' and the verb itself. If it is none of these, op = other.\n"
                  "Examples:\n"
                  "put the second video on → {\"op\": \"pick\", \"nth\": \"second\", \"kind\": \"video\"}\n"
                  "get me that lofi vid with the rain → {\"op\": \"play\", \"query\": \"lofi rain\"}\n"
                  "chuck cats into the search bar and hit enter → {\"op\": \"type\", \"text\": \"cats\", "
                  "\"submit\": true}\n"
                  "hit the like button → {\"op\": \"click\", \"target\": \"like\"}\n"
                  "find me some reviews of the steam deck on reddit → {\"op\": \"search\", \"query\": \"steam deck "
                  "reviews\", \"site\": \"reddit\"}\n"
                  "go down a bit → {\"op\": \"scroll\", \"direction\": \"down\"}\n"
                  "what time is it → {\"op\": \"other\"}")
        body = {"model": self.cfg["ollama_model"], "stream": False, "think": False,
                "keep_alive": self.cfg.get("ollama_keep_alive", "5m"),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                "format": {"type": "object", "properties": {
                    "op": {"type": "string", "enum": ["search", "pick", "click", "type", "scroll", "play", "other"]},
                    "query": {"type": "string"}, "site": {"type": "string"}, "nth": {"type": "string"},
                    "kind": {"type": "string"}, "target": {"type": "string"}, "text": {"type": "string"},
                    "submit": {"type": "boolean"}, "direction": {"type": "string"}}, "required": ["op"]},
                "options": {"num_ctx": 2048, "temperature": 0, "num_predict": 120}}
        try:
            r = await self.client.post("/api/chat", json=body)
            r.raise_for_status()
            step = json.loads(r.json()["message"]["content"])
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            log.info("browser step failed: %s", e)
            return None
        from ..tools import SEARCH

        log.info("browser step %r → %s", text, step)
        op = step.get("op")
        site = str(step.get("site") or "").lower().strip()
        ordinal = re.fullmatch(r"(?:the )?(first|second|third|fourth|fifth|last|top) (?:video|result|one|link)s?",
                               str(step.get("query") or step.get("target") or "").lower().strip())
        if op in ("play", "search", "click") and ordinal:  # "put the second video on" is a pick, not a search
            op, step = "pick", {"nth": ordinal[1].replace("top", "first"), "kind": "video" if "vid" in text else ""}
        if op == "search" and step.get("query"):
            return {"op": op, "query": step["query"].strip(), "site": site if site in SEARCH else "", "video": False}
        if op == "pick":
            nth = str(step.get("nth") or "first").lower()
            return {"op": op, "nth": nth if nth in ("first", "second", "third", "fourth", "fifth", "last") else "first",
                    "kind": "video" if "vid" in str(step.get("kind")) else "result" if step.get("kind") else ""}
        if op == "click" and step.get("target"):
            return {"op": op, "target": step["target"].strip()}
        if op == "type" and step.get("text"):
            return {"op": op, "text": step["text"], "submit": bool(step.get("submit"))}
        if op == "scroll" and step.get("direction") in ("up", "down", "top", "bottom"):
            return {"op": op, "direction": step["direction"]}
        if op == "play" and step.get("query"):
            return {"op": op, "query": step["query"].strip()}
        return None

    def _catalog(self, offered: list[str]) -> str:
        rest = [t for t in self.tools.specs() if t.name not in offered and t.name not in HIDDEN]
        lines = [f"- {t.name}: {re.split(r'(?<=[.!?]) ', t.description)[0][:90]}" for t in rest]
        return "Other tools you can also call by name (arguments as described):\n" + "\n".join(lines)

    async def respond(self, text: str, context: str, history: list[dict] | None = None,
                      previous_tools: list[str] | None = None, learned: list[dict] | None = None,
                      replay: dict | None = None, no_asking: bool = False) -> str:
        """Returns the reply, or raises Escalate when nothing local worked: a second, deeper attempt, researching
        it herself, asking the user what they mean (no_asking: she already did, for this request).

        learned: requests like this one that worked before (assistant/learning.py) — shown as examples, their
        tools are offered. replay: one that is safe to simply redo, no thinking needed."""
        self.replayed = self.asked = False
        done: dict[str, str] = {}  # shared by all attempts so actions aren't repeated
        if replay:
            try:
                reply = await self._replay(text, replay, context, history, done)
                if reply:
                    self.replayed = True
                    return reply
            except Exception as e:  # fall back to the normal way
                log.info("replay failed: %s", e)
        if CODING.search(text):
            raise Escalate("coding or writing task")
        # pure small talk ("what's up?", "love you, cheers"): no tools, no thinking. "hey, can you open steam"
        # also starts like chat, so only when nothing after the greeting asks for something
        if (m := CHAT.match(text)) and not REQUEST.search(re.sub(r"[\W_]+$", "", text[m.end(1):])) and \
                (answer := await self._small_talk(text, context, history)):
            return answer
        think_cfg = self.cfg.get("local_thinking", "auto")  # auto | always | never
        chat = bool(CHAT.match(text))
        think_first = think_cfg == "always" or (think_cfg == "auto" and not chat)
        think_rest = think_cfg == "always" or (think_cfg == "auto" and bool(HARD.search(text)))
        previous_tools = list(previous_tools or [])
        if learned:
            previous_tools += [c["name"] for s in learned for c in s["calls"] if c["name"] not in previous_tools]
            context = (context + "\n" if context else "") + LEARNED + "\n".join(map(example_line, learned))
        try:
            return await self._respond(text, context, history, previous_tools, think_first, think_rest, done)
        except Escalate as e:
            if think_cfg == "never":
                raise
            log.info("first attempt failed (%s), trying harder", e)
            try:
                return await self._respond(text, context, history, previous_tools, True, True, done,
                                           retry=str(e))
            except Escalate as e2:
                # twice nothing to say and nothing done: probably misheard, and Claude can't hear better
                if str(e2) == "empty reply" and not done:
                    return "Sorry, I didn't quite catch that. Could you say it again?"
                if done or not self.cfg.get("ask_before_claude", True) or no_asking:
                    raise  # she already did part of it, or asked once already: Claude finishes it
                # every last local bit before Claude: research a question about the world herself…
                if WORLD_Q.match(text) and not PERSONAL.search(text) and \
                        (answer := await self._research(text, context, history)):
                    return answer
                # …just talk, if it wasn't a request at all ("I don't mind", "you're so cute")…
                if not REQUEST.search(text) and (answer := await self._small_talk(text, context, history)):
                    return answer
                # …or ask what they mean instead of guessing
                if q := await self.clarify(text, history, previous_tools, context):
                    self.asked = True
                    return q
                raise

    async def _replay(self, text: str, skill: dict, context: str, history: list[dict] | None,
                      done: dict[str, str]) -> str:
        """Redo what worked last time for the same request, then only put the result into words (no thinking)."""
        log.info("replaying learned skill: %s", ", ".join(c["name"] for c in skill["calls"]))
        calls = [{"function": {"name": c["name"], "arguments": c["args"]}} for c in skill["calls"]]
        system = (f"{self.persona['style']}\nYou just did what the user asked with your tools; the results are "
                  "below. Answer from them in a few words, in English (also when the results are in another "
                  "language), spoken style: no lists, no headings. Never say you did something the results don't "
                  "show."
                  f"\n{clock.now_line()}" + ("\n" + context if context else ""))
        messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": text},
                    {"role": "assistant", "content": "", "tool_calls": calls}]
        for c in skill["calls"]:
            key = f"{c['name']}:{json.dumps(c['args'], sort_keys=True)}"
            result = done[key] = await self.tools.call(c["name"], c["args"])
            if NEVER_KEEP.match(result):
                raise RuntimeError(f"{c['name']} → {result[:80]}")
            messages.append({"role": "tool", "tool_name": c["name"], "content": result})
        msg = await self._chat(messages, None, False)
        reply = clean_reply((msg.get("content") or "").strip())
        if foreign(reply):
            msg = await self._chat(messages + [{"role": "assistant", "content": reply}, {"role": "user", "content":
                                   "Say that again in English, in one or two short sentences."}], None, False)
            reply = clean_reply((msg.get("content") or "").strip())
        return reply

    async def _respond(self, text: str, context: str, history: list[dict] | None, previous_tools: list[str],
                       think_first: bool, think_rest: bool, done: dict[str, str], retry: str = "") -> str:
        names = pick_tools(text, previous_tools)
        chat = bool(CHAT.match(text))
        if retry:
            names = (names + [x for x in DEEP_TOOLS if x not in names])[:MAX_TOOLS + 2]
        log.info("tools offered (%s): %s", "retry" if retry else "thinking" if think_first else "no thinking",
                 ", ".join(names))
        messages = [{"role": "system", "content": self.system_prompt(names, context, retry)}, *(history or []),
                    {"role": "user", "content": text}]
        return await self._loop(text, messages, names, history, think_first, think_rest, done, chat)

    def system_prompt(self, names: list[str], context: str = "", retry: str = "") -> str:
        system = (f"{self.persona['style']}\nAlways answer in English. You control the user's Linux desktop (CachyOS/Arch, niri, Noctalia "
                  "shell, Brave browser) with tools, and can do almost anything on it with run_shell. "
                  "After using a tool, answer from its result in a few words; for news mention the 2-3 most relevant "
                  "headlines with dates. Never say you did something unless a tool result confirms it. For facts, "
                  "news or anything current use web_research and name the sites. If the user says yes to something "
                  f"you offered, do it right away. Call {ESCALATE} only as a last resort. You know the current date "
                  f"and time from the line below.\n{HINT}\n{EXAMPLES}\n{self._catalog(names)}\n{clock.now_line()}")
        if retry:
            system += (f"\nYour first try at this request failed ({retry}). Think harder: what does the user really "
                       "want, and which tool (or run_shell command) gets it done? Then do it.")
        if context:
            system += "\n" + context
        return system

    async def _loop(self, text: str, messages: list[dict], names: list[str], history: list[dict] | None,
                    think_first: bool, think_rest: bool, done: dict[str, str], chat: bool) -> str:
        specs = self._tool_specs(names)
        known = set(t.name for t in self.tools.specs()) - HIDDEN
        used: list[str] = []
        nudged = asked_answer = expanded = checked = englished = False
        think = think_first
        for _ in range(8):
            msg = await self._chat(messages, specs, think)
            think = think_rest
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = re.sub(r"^\[tool results?:.*?\]\s*", "", (msg.get("content") or "").strip(), flags=re.S)
                if not reply:
                    # it can only call tools it has the full spec of: hand over the ones its thoughts picked
                    wanted = [n for n in known - set(names) if re.search(rf"\b{n}\b", msg.get("thinking") or "")]
                    if wanted and not expanded:
                        expanded = True
                        names += wanted
                        specs = self._tool_specs(names)
                        think = True
                        log.info("handing over %s from its thoughts", ", ".join(wanted))
                        continue
                    if used and not asked_answer:  # did the work but said nothing: ask for the answer once
                        asked_answer = True
                        messages.append({"role": "user", "content": "Now tell me the result in one or two sentences."})
                        continue
                    log.debug("empty reply; thinking was: %s", (msg.get("thinking") or "")[-600:])
                    raise Escalate("empty reply")
                # small models sometimes *say* they did something without calling a tool
                if not used and re.search(r"\bI(?:'ve| have)? (?:opened|launched|started|installed|closed|"
                                          r"moved|deleted|created|sent|set|turned|switched|checked|updated|"
                                          r"played|killed|found)\b", reply, re.I):
                    raise Escalate("claimed an action without a tool call")
                announced = not chat and re.search(
                    r"\b(let me (check|look|open|find|search|get|see|try|pull)|(i'll|i will|i'm going to) (check|look|"
                    r"open|find|search|get|do|run|see|pull|start|launch|install)|one moment|give me a (sec|second|"
                    r"moment)|hold on|just a (moment|sec))\b", reply, re.I)
                if announced and used and not asked_answer:  # "let me check" after the tool already ran
                    asked_answer = True
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": "You already have the tool result above. Tell me what it "
                                                             "says in one or two sentences."}]
                    continue
                if announced and not used and not nudged:  # announced an action but stopped: push it once
                    nudged = True
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": "If you meant to do something, call the tool for it "
                                                             "now. If that was just a normal reply, say it again."}]
                    continue
                if announced and not used:
                    raise Escalate("announced an action but did not do it")
                if foreign(reply) and not englished:
                    englished = True
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": "Say that again in English, in one or two short "
                                                             "sentences."}]
                    continue
                if not used and not nudged and LIVE.search(text):  # answered the weather from nothing
                    nudged = True
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": "Don't guess: check it with the weather tool now."}]
                    continue
                if used and not checked and (PARTS.search(text) or any(
                        NEVER_KEEP.match(m["content"]) for m in messages if m.get("role") == "tool")):
                    checked = True  # several parts, or a tool failed: did she really do all of it?
                    missing = self.check(text, [(k.replace(":", "(", 1) + ")", r) for k, r in done.items()])
                    if missing:
                        log.info("self-check: not done yet: %s", missing)
                        messages += [{"role": "assistant", "content": reply},
                                     {"role": "user", "content": f"You haven't done this part yet: {missing}. Do it "
                                                                 "now with your tools, then tell me everything in "
                                                                 "one or two sentences."}]
                        continue
                return clean_reply(reply)
            messages.append({k: v for k, v in msg.items() if k != "thinking"})
            for c in calls:
                fn = c["function"]
                name, args = fn["name"], fn.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                if name in ALIASES:  # an old name from her training or examples: same job, merged tool
                    name, args = ALIASES[name], {k: v for k, v in args.items() if k in ("question", "text", "submit")}
                if name == ESCALATE:
                    raise Escalate(args.get("reason", "") or "asked for Claude")
                if name not in known:
                    result = f"error: there is no tool called {name}. Use one of the listed tools."
                    messages.append({"role": "tool", "tool_name": name, "content": result})
                    continue
                if name not in names:  # picked from the catalog: give it the full spec for later calls
                    names.append(name)
                    specs = self._tool_specs(names)
                if name == "open" and re.match(r"https?://", str(args.get("target", ""))) and \
                        urlparse(args["target"]).path.strip("/") and \
                        args["target"].rstrip("/") not in json.dumps(messages, ensure_ascii=False):
                    # a URL with a path that nobody gave it is usually made up (github.com/niriwm/niri)
                    log.info("invented URL %s → open_best_match", args["target"])
                    name, args = "open_best_match", {"query": text}
                bad = next((k for k in args if (name, k) in SAID_ARGS and not _said(args[k], text, history)), None)
                if bad:  # it makes up messages ("I'm coming in 5 minutes" copied from the examples)
                    log.info("invented %s %s=%r, refused", name, bad, args[bad])
                    messages.append({"role": "tool", "tool_name": name, "content":
                                     f"error: the user didn't say what {bad} to send. Don't make it up: ask them."})
                    continue
                for k in ("location", "place"):  # qwen3 invents places ("Paris", "your location")
                    if args.get(k) and not _mentioned(args[k], text, history):
                        args.pop(k)
                key = f"{name}:{json.dumps(args, sort_keys=True)}"
                if key in done:  # same call twice (e.g. opening a tab twice): don't repeat the action
                    result = done[key] + " (already done, don't repeat)"
                else:
                    log.info("local tool %s %s", name, args)
                    used.append(name)
                    result = done[key] = await self.tools.call(name, args)
                messages.append({"role": "tool", "tool_name": name, "content": result})
        raise Escalate("too many steps")


def _mentioned(value: str, text: str, history: list[dict] | None) -> bool:
    said = " ".join([text] + [m["content"] for m in history or []]).lower()
    return any(w in said for w in re.findall(r"[a-zà-ÿ]{3,}", str(value).lower()))


def _said(value: str, text: str, history: list[dict] | None) -> bool:
    """Most words of a message to send must come from what the user said (now or in the last turns)."""
    said = set(re.findall(r"[\w']+", " ".join([text] + [m["content"] for m in (history or [])[-4:]
                                                      if m["role"] == "user"]).lower()))
    words = re.findall(r"[\w']+", str(value).lower())
    return not words or sum(w in said for w in words) / len(words) >= 0.6


def foreign(text: str) -> bool:
    """Mostly not Latin script (she once answered in Japanese because the web results were)."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) > 10 and sum(c > "\u024f" for c in letters) / len(letters) > 0.3


def clean_reply(text: str) -> str:
    """Spoken aloud: no markdown, no emoji."""
    text = re.sub(r"\*\*|__|`|^#+\s*", "", text, flags=re.M)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text).strip()
    # filler she'd say after every single answer
    text = re.sub(r"\s*(?:let me know|feel free to (?:ask|let me know)|just (?:let me know|ask))[^.!?]*"
                  r"(?:anything else|need|help|adjust|questions?)[^.!?]*[.!?]?\s*$", "", text, flags=re.I) or text
    # …and the empty question qwen3.5 tacks on ("What would you like to do with it?"); real ones stay
    text = re.sub(r"(?<=[.!?])\s+(?:so,? )?(?:what (?:else )?(?:would|do) you (?:like|want)(?: me)? to do(?: with it| next)?|"
                  r"(?:is there )?anything else(?: (?:i can|you'?d like|you want)[^?]*)?|how (?:else )?can i help"
                  r"(?: you)?(?: (?:with|next)[^?]*)?)\?\s*$", "", text, flags=re.I)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()
