"""assistantd — the voice loop.

push-to-talk → record (VAD) → whisper → fast-path | Ollama | Claude → tools → Kokoro → speaker
                                                                              └→ avatar lip-sync
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import signal
import time

from . import browse, config
from .audio import Player, Recorder, chime, envelope
from .brain import fastpath
from .brain.claude import ClaudeBrain, ClaudeError
from .brain.local import Escalate, LocalBrain
from .memory import Memory, Usage
from .stt import STT
from .tools import ToolBox, sh
from .tts import TTS, split_sentences
from .wakeword import strip_name

log = logging.getLogger("assistantd")

EMOTIONS = [  # first match wins; drives Mio's face while she speaks
    ("worried", re.compile(r"\b(couldn'?t|can'?t|cannot|failed|error|not able|unable|went wrong|"
                           r"didn'?t work|not working|careful|warning)\b", re.I)),
    ("sad", re.compile(r"\b(sorry|unfortunately|declined|no luck|lost)\b", re.I)),
    ("surprised", re.compile(r"\b(wow|whoa|oh!|no way|really\?|amazing|incredible|huge)\b", re.I)),
    ("shy", re.compile(r"\b(you'?re welcome|thank you|aww+|cute|love|sweet|flatter)\b", re.I)),
    ("happy", re.compile(r"!|\b(great|done|here you go|opening|starting|sure|yay|good morning|nice|perfect|"
                         r"have fun|up to date|healthy|ready)\b", re.I)),
    ("curious", re.compile(r"\?\s*$")),
]


def emotion_for(text: str) -> str:
    return next((name for name, rx in EMOTIONS if rx.search(text)), "neutral")


# requests with several parts get split into steps first ("open discord and then start roblox")
VERBS = (r"open|play|turn|set|close|launch|start|put|search|check|mute|pause|lower|raise|send|tell|show|remind|make|"
         r"switch|find|read|go|take|clip|move|tidy|clean|delete|message")
MULTI = re.compile(rf"\b(and then|then|after that|afterwards|and also|as well as)\b|,\s*(?:and )?(?:{VERBS})\b|"
                   rf"\band (?:{VERBS})\b", re.I)
# one action that just sounds like two (the fast path does it in one go)
COMPOUND = re.compile(r"\b(?:on|in) (youtube|yt) and (open|play|click|start|watch) (the )?(first|top)\b|"
                      # "open brave origin and look up X": a search in the browser, one step
                      r"^\W*(?:hey mio,? )?open(?: up)? (?:brave(?: origins?)?|(?:the |my )?browser) and "
                      r"(?:search|look up|google)\b(?!.* and )|"
                      # "look at my screen and tell me what you see": the second part only reports on the first
                      r"\band (?:tell|let) me (?:know )?(?:what|how|if|whether|why|who|which|where) "
                      r"(?:you|it|it'?s|that|this|they|there|he|she)\b", re.I)
# teaching a routine: "when I say gaming time, open discord, launch roblox and turn on do not disturb"
TEACH = re.compile(r"^\W*(?:hey mio,? )?(?:when(?:ever)? i say|every time i say|if i say|make a routine called|"
                   r"create a routine called|new routine called)\s+[\"'“]?(.+?)[\"'”]?\s*[,:]\s*(?:(?:you should|please|"
                   r"then|i want you to|that means|it should|do|to)\s+)?(.+)$", re.I)

YES = re.compile(r"\b(yes|yeah|yep|sure|ok(ay)?|do it|go ahead|confirm(ed)?|please do|affirmative)\b", re.I)
NO = re.compile(r"\b(no|nope|don'?t|stop|cancel|never ?mind)\b", re.I)
# her offer after a search: "Want me to open the top result?" / "Should I play the first video?"
# "open the first video", "play the second one", "click the top result"
PICK_RESULT = re.compile(r"(?:open|play|click(?: on)?|watch|start|put on|go to|take me to)(?: up)? (?:the |that )?"
                         r"(first|1st|second|2nd|third|3rd|top|last) (video|result|link|one)(?: on (?:the|this) page)?")
OFFER_TOP = re.compile(r"\b(open|play|watch|start|put on|click)\b[^.?!]{0,40}\b(top|first|best)\b[^.?!]{0,20}"
                       r"\b(result|video|one|hit|link)s?\b[^.?!]*\?\s*$", re.I)


class Avatar:
    """Client for the avatar process's unix socket. Silently no-ops if it isn't running."""

    def __init__(self):
        self._w: asyncio.StreamWriter | None = None

    async def send(self, **msg):
        for _ in range(2):
            try:
                if self._w is None or self._w.is_closing():
                    _, self._w = await asyncio.wait_for(asyncio.open_unix_connection(str(config.AVATAR_SOCK)), 1)
                self._w.write((json.dumps(msg) + "\n").encode())
                await self._w.drain()
                return
            except (OSError, asyncio.TimeoutError):
                self._w = None


def _call(c: dict) -> str:
    return f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})"


class Assistant:
    def __init__(self):
        self.cfg = config.load()
        self.state = "idle"  # idle | listening | thinking | speaking | confirming
        self.pinned = False
        self.avatar = Avatar()
        self.recorder = Recorder(self.cfg.stt)
        self.player = Player(self.cfg.tts.get("volume", 1.0))
        self.stt = STT(self.cfg.stt, self.cfg.persona.get("name", ""))
        self.tts = TTS(self.cfg.tts)
        self.memory = Memory(self.cfg.brain.get("history_turns", 4))
        self.usage = Usage(self.cfg.brain.get("claude_daily_limit", 60))
        self.tools = ToolBox(self.cfg.security, self.confirm, self.memory.remember, self.cfg.lookup)
        self.tools.on_tool = self._tool_status
        self.tools.on_result = self._on_result
        self.learning = None
        if self.cfg.learning.get("enabled", True):
            from .learning import Learning

            self.learning = Learning(self.cfg.brain)
            from .brain.local import EXAMPLES

            self.learning.lexicon.set_domain(EXAMPLES + " ".join(f"{t.description} {json.dumps(t.params)}"
                                                                 for t in self.tools.tools.values()))
        self._trace: list[dict] = []  # tool calls of the request in progress (local or Claude)
        self._hints: list[str] = []   # unsure name fixes for the request in progress
        self._asked: dict | None = None  # she asked what they meant: {"text": the request, "t": when}
        self._no_asking = False          # …and this is their answer: if it fails again, Claude gets it
        self._last_actions: dict | None = None  # the last request that changed something, for "again" / "undo"
        self._before: dict = {}                 # state from just before this request changed it (volume level)
        from .knowledge import Knowledge

        self.tools.knowledge = Knowledge(self.cfg.knowledge.get("paths", []))
        self.tools.launchers = self.cfg.games.get("launchers", {})
        self.tools.vision = self.cfg.brain
        self.tools.run_steps = self._run_steps
        from .timers import Timers

        self.timers = Timers(config.STATE_DIR / "timers.json", self._timer_due)
        self.tools.timers = self.timers
        from .clips import Clipper

        self.clipper = Clipper(self.cfg.clips)
        self.tools.clipper = self.clipper
        self.gamemode = None
        self.wake = None
        self.gaming = False
        self._regame_task: asyncio.Task | None = None
        self._last_activity = 0.0
        self.local = LocalBrain(self.cfg.brain, self.cfg.persona, self.tools)
        self.dreamer = None
        if self.learning:
            from .dream import Dreamer, active_model, use_trained

            self.dreamer = Dreamer(self, self.cfg.learning, self.cfg.brain["ollama_model"])
            if trained := active_model(self.cfg.learning, self.dreamer.base):
                use_trained(self, trained)  # her fine-tuned self
        self.claude = ClaudeBrain(self.cfg.brain, self.cfg.persona, self.tools)
        self._task: asyncio.Task | None = None
        self._hide_task: asyncio.Task | None = None
        self._confirm_lock = asyncio.Lock()

    # ── avatar visibility ───────────────────────────────────────────────────
    async def set_state(self, state: str):
        self.state = state
        await self.avatar.send(cmd="state", state=state)
        face = {"listening": "attentive", "thinking": "thinking", "confirming": "curious", "idle": "neutral"}
        if state in face:
            await self.avatar.send(cmd="emotion", name=face[state])

    async def show(self):
        if self._hide_task:
            self._hide_task.cancel()
            self._hide_task = None
        if self.gaming and not self.cfg.gamemode.get("show_avatar", False):
            return  # voice only while a game is running
        await self.avatar.send(cmd="show")

    async def hide(self, delay: float = 0):
        async def _later():
            await asyncio.sleep(delay)
            if not self.pinned and self.state == "idle":
                await self.avatar.send(cmd="hide")
                self.end_session()
        if self._hide_task:
            self._hide_task.cancel()
        self._hide_task = asyncio.create_task(_later())

    def _on_result(self, name: str, args: dict, result: str):
        self.memory.note(name, args, result)
        self._trace.append({"name": name, "args": args, "result": result})
        if self.learning:
            self.learning.lexicon.from_tool(name, args, result)

    def _refresh_learning(self):
        """Re-read app/game/channel names now and then and give the newest to Whisper."""
        if not self.learning:
            return
        from .tools import app_index

        from .games import steam_games

        games = ["Roblox", "Froststrap", "Sober"] + [g.title() for g in self.tools.launchers] + list(steam_games())
        try:
            self.learning.refresh(lambda: ([a["name"] for a in app_index().values()], games))
        except Exception as e:
            log.warning("learning refresh failed: %s", e)
        self.stt.names = self.learning.lexicon.prompt_names()

    def end_session(self):
        if self.memory.active():
            log.info("session ended, conversation forgotten")
        self.memory.end_session()

    async def toggle_pin(self):
        self.pinned = not self.pinned
        if self.pinned:
            await self.show()
        else:
            await self.hide()

    STATUS = {"web_research": "Searching the web for “{query}”…", "read_link": "Reading the page…",
              "read_clipboard_link": "Reading the link you copied…", "read_screen": "Reading your screen…", "screen": "Looking at your screen…",
              "look_at_screen": "Looking at your screen…", "check_install": "Checking that package…",
              "arch_wiki": "Checking the ArchWiki…", "wikipedia": "Checking Wikipedia…",
              "system_updates": "Checking for updates…", "arch_news": "Reading the news…",
              "weather": "Checking the weather…", "system_health": "Checking your PC…",
              "game_news": "Reading the patch notes for {game}…", "game_price": "Checking Steam prices…",
              "browser_page": "Reading the page…", "browser_history": "Searching your history…"}

    async def _tool_status(self, name: str, args: dict):
        if name == "volume" and "volume" not in self._before:  # so "undo that" can put it back exactly
            code, out = await sh("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
            if code == 0 and (m := re.search(r"Volume: ([\d.]+)", out)):
                self._before["volume"] = float(m[1])
        text = self.STATUS.get(name)
        if text:
            await self.show()
            await self.avatar.send(cmd="status", text=text.format(**{k: str(v)[:60] for k, v in args.items()}))

    # ── speech ──────────────────────────────────────────────────────────────
    async def say(self, text: str):
        sentences = split_sentences(text)
        if not sentences:
            return
        await self.set_state("speaking")
        await self.show()
        await self.avatar.send(cmd="emotion", name=emotion_for(text))
        loop = asyncio.get_running_loop()
        nxt = asyncio.create_task(self.tts.synth(sentences[0]))
        for i in range(len(sentences)):
            audio, sr = await nxt
            if i + 1 < len(sentences):
                nxt = asyncio.create_task(self.tts.synth(sentences[i + 1]))
            env = envelope(audio, sr)
            started = lambda: asyncio.run_coroutine_threadsafe(
                self.avatar.send(cmd="speak", env=env, fps=30, text=sentences[i]), loop)
            if not await self.player.play(audio, sr, on_start=started):
                nxt.cancel()
                break
        await self.avatar.send(cmd="speak_end")

    async def beep(self, rising: bool):
        if self.cfg.ptt.get("chime", True):
            await self.player.play(*chime(rising))

    # ── confirmation gate (called by ToolBox for risky actions) ─────────────
    async def confirm(self, summary: str) -> bool:
        async with self._confirm_lock:
            prev = self.state
            timeout = self.cfg.security.get("confirm_timeout_s", 20)
            short = summary if len(summary) < 90 else summary[:87] + "..."
            notif = asyncio.create_task(sh("notify-send", "-a", "Assistant", "-u", "critical", "-w",
                                           "-A", "yes=Allow", "-A", "no=Deny", "Allow this action?", summary,
                                           timeout=timeout))
            await self.say(f"Should I do this? {short}. Say yes or no.")
            await self.set_state("confirming")
            voice = asyncio.create_task(self._listen_once())
            pending, ok = {notif, voice}, False
            deadline = asyncio.get_running_loop().time() + timeout
            while pending:
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    break
                done, pending = await asyncio.wait(pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                if notif in done:
                    answer = notif.result()[1].strip()
                    if answer in ("yes", "no"):  # clicked a button
                        ok = answer == "yes"
                        break
                    continue  # dismissed / no action support: keep listening for voice
                if voice in done:
                    heard = voice.result() or ""
                    ok = bool(YES.search(heard)) and not NO.search(heard)
                    break
            for t in pending:
                t.cancel()
            self.recorder.cancel()
            await self.set_state(prev)
            log.info("confirmation for %r: %s", summary, ok)
            return ok

    async def _listen_once(self) -> str:
        audio = await self.recorder.record()
        return await self.stt.transcribe(audio) if audio is not None else ""

    # ── main interaction ────────────────────────────────────────────────────
    async def ptt(self):
        if self.state == "listening":
            self.recorder.finish()
            return
        if self.state == "speaking":
            self.player.stop()
        if self.state in ("thinking", "confirming"):
            return
        if self._task and not self._task.done():
            await asyncio.sleep(0.05)
        self._task = asyncio.create_task(self._interaction())

    async def _interaction(self, text: str | None = None):
        if self.dreamer:
            asyncio.create_task(self.dreamer.stop("you called me"))
        loop_time = asyncio.get_running_loop().time()
        if loop_time - self._last_activity > self.cfg.brain.get("session_timeout_s", 600):
            self.end_session()  # e.g. pinned on screen but untouched for a long time
        self._last_activity = loop_time
        voice = text is None
        try:
            if text is None:
                await self.set_state("listening")
                if self.cfg.avatar.get("show_while_listening", True):
                    await self.show()
                asyncio.create_task(self.beep(True))
                self.clipper.asked_at = loop_time  # "clip that" should reach back to when you started talking
                if self.cfg.brain.get("local_enabled", True) and not self.gaming:
                    asyncio.create_task(self.local.preload())  # warm the local model while you speak
                audio = await self.recorder.record()
                await self.beep(False)
                if audio is None:
                    return
                await self.set_state("thinking")
                text = await self.stt.transcribe(audio)
                log.info("heard: %r", text)
                if not text or len(text) < 2:
                    return
            text = strip_name(text)
            if self.learning:  # typos, shorthand and misheard names, typed or spoken
                text, self._hints = self.learning.correct(text, voice)
            await self.set_state("thinking")
            await self.show()  # stay visible while working, also for typed requests
            reply = await self.respond(text)
            self._hints = []
            log.info("reply: %r", reply)
            if reply:
                await self.say(reply)
        except Exception as e:
            log.exception("interaction failed")
            await self.say("Sorry, something went wrong.")
            await self.avatar.send(cmd="error", text=str(e)[:200])
        finally:
            await self.set_state("idle")
            await self.hide(self.cfg.avatar.get("hide_after_s", 15))
            if self.gaming:
                self._schedule_regame()

    def _schedule_regame(self):
        """After using her mid-game, free the GPU again once she's been quiet for a minute."""
        async def later():
            await asyncio.sleep(self.cfg.gamemode.get("unload_after_s", 60))
            if self.gaming and self.state == "idle":
                await self._free_gpu()
        if self._regame_task:
            self._regame_task.cancel()
        self._regame_task = asyncio.create_task(later())

    def _wake_in_game(self) -> bool:
        """"Hey Mio" mid-game keeps Whisper loaded (~1.3 GB VRAM); the big local model is still freed."""
        return bool(self.wake) and self.cfg.gamemode.get("wake_word", True)

    async def _free_gpu(self):
        if not self._wake_in_game():
            await asyncio.to_thread(self.stt.unload)
        await self.local.unload()

    async def _on_gamemode(self, active: bool, game: str):
        self.gaming = active
        self.tools.gaming = active
        if active and self.dreamer:
            await self.dreamer.stop("a game started")
        if self.cfg.clips.get("enabled", True) and not self.cfg.clips.get("always", False):
            if active:
                asyncio.create_task(self.clipper.start(game, self.gamemode.pattern if self.gamemode else None))
            else:
                asyncio.create_task(self.clipper.stop())
        if active:
            if self.state == "idle":
                await self.avatar.send(cmd="hide")
                await self._free_gpu()
        else:
            if self._regame_task:
                self._regame_task.cancel()
            if self.stt._model is None:
                await asyncio.to_thread(self.stt.load)  # back to instant replies

    async def respond(self, text: str) -> str:
        if re.search(r"\b(what|which)\b.*\b(couldn'?t|could not|can'?t|cannot|didn'?t)\b.*\b(do|handle|answer)\b"
                     r".*\b(alone|yourself|on your own|without claude)\b|\bwhat did you (ask|send to) claude\b", text, re.I):
            return self.escalation_report()[0]
        self._trace = []
        self._before = {}
        self.tools.said = ""  # again/undo replay exactly what was done before
        if (reply := await self._redo(text)) is not None:
            self.last_src = "fast"
            self.memory.add(text, reply)
            return reply
        if (reply := await self._take_offer(text)) is not None:
            self.last_src = "fast"
            self.memory.add(text, reply)
            return reply
        if self.learning:
            self._refresh_learning()
            self.learning.feedback(text)
        self.tools.said = text
        pending = self._asked if self._asked and time.time() - self._asked["t"] < 180 else None
        self._asked, self._no_asking = None, bool(pending)
        reply, src = await self._respond(text)
        if reply and src in ("fast", "replay") and MULTI.search(text) and not COMPOUND.search(text) and \
                self.cfg.brain.get("local_enabled", True):  # those do one thing: did it cover every part of the request?
            did = [(_call(c), c["result"]) for c in self._trace] + [("her reply", reply)]
            if missing := self.local.check(text, did):
                log.info("self-check: not done yet: %s", missing)
                more, _ = await self._respond(missing, nested=True)
                reply = f"{reply} {more}".strip()
        self.last_src = src
        from .learning import NEVER_KEEP, READ_ONLY

        if acts := [c for c in self._trace if c["name"] not in READ_ONLY and not NEVER_KEEP.match(c["result"])]:
            self._last_actions = {"t": time.time(), "text": text, "calls": acts, "before": dict(self._before)}
        if src == "ask":
            self._asked = {"text": text, "t": time.time()}
        if self.learning:
            try:
                await self.learning.done(text, self._trace, reply, src)
                if pending and src in ("local", "replay", "claude") and self._trace:
                    # the answer to her question made it work: the original request means this from now on
                    await self.learning.skills.record(pending["text"], self._trace, reply, src)
                self.stt.names = self.learning.lexicon.prompt_names()
            except Exception as e:
                log.warning("learning failed: %s", e)
        return reply

    async def _take_offer(self, text: str) -> str | None:
        """'Open the first video' / 'sure' to her 'want me to open the top result?': click that result on the page
        the user sees (the extension); without it, the top hit of the last search. The model alone searched again
        or replayed an old skill with another video. None if this isn't that."""
        from urllib.parse import parse_qs, urlparse

        prev = self.memory.turns[-1]["assistant"] if self.memory.turns else ""
        t = re.sub(r"^\W*(?:hey mio,? )?(?:(?:can|could|would) you |please )*|\W*(?:please|for me)?\W*$", "",
                   text.lower())
        if m := PICK_RESULT.fullmatch(t):
            nth, kind = (m[1] or "first").replace("top", "first"), m[2].replace("one", "result")
        elif OFFER_TOP.search(prev) and YES.search(text) and not NO.search(text) and len(text.split()) <= 6:
            nth, kind = "first", "video" if re.search(r"video|youtube", prev, re.I) else "result"
        else:
            return None
        r = await self.tools.call("browser_click", {"target": f"{nth} {kind}"})
        if r.startswith("clicked"):
            log.info("%r: clicked the %s %s on the page", text, nth, kind)
            return "Here it is!" if kind != "video" else "Playing it!"
        last = self._last_actions
        query, youtube = "", False
        for c in reversed(last["calls"] if last and time.time() - last["t"] < 600 else []):
            a = c["args"]
            if c["name"] == "search_site":
                query, youtube = a.get("query", ""), a.get("site", "").lower() == "youtube"
            elif c["name"] == "open" and (q := parse_qs(urlparse(a.get("target", "")).query)):
                query = (q.get("search_query") or q.get("q") or q.get("k") or [""])[0]
                youtube = "youtube." in a.get("target", "")
            if query:
                break
        if not query or nth != "first":
            return None if r.startswith("the browser extension isn't") else r[0].upper() + r[1:].removeprefix("error: ") + "."
        log.info("%r: click failed (%s), top hit of the last search %r", text, r, query)
        self.tools.said = ""  # they asked for this video: no "did they ask to play" check
        if youtube:
            r = await self.tools.call("youtube_play", {"query": query})
            if hit := re.match(r"playing “(.+?)” by (.+?)(?:\. Other results|$)", r):
                return f"Playing {hit[1]} by {hit[2]}!"
        else:
            r = await self.tools.call("open_best_match", {"query": query})
            if r.startswith("opened"):
                return "Here it is!"
        return r[0].upper() + r[1:] + "."

    async def _redo(self, text: str) -> str | None:
        """'Do that again' / 'a bit more' / 'undo that' for her last request that changed something; None if it
        isn't one of those (or there's nothing to redo), then it goes the normal way."""
        from . import redo

        last = self._last_actions
        again, more, undo = redo.AGAIN.match(text), redo.MORE.match(text), redo.UNDO.match(text)
        if not (again or more or undo) or not last or time.time() - last["t"] > 900:
            return None
        prev = self.memory.turns[-1]["assistant"] if self.memory.turns else ""
        if (again or more) and prev.rstrip().endswith("?"):
            return None  # "more" as an answer to her question
        from .learning import DESTRUCTIVE

        if undo:
            done, cant, restored = [], [], False
            for c in reversed(last["calls"]):
                if c["name"] == "volume" and restored:
                    continue  # the level from before covers every volume change
                inv = redo.inverse(c, last["before"])
                if isinstance(inv, str):
                    cant.append(inv)
                    continue
                r = await self.tools.call(inv["name"], inv["args"])
                (cant if r.startswith(("error", "refused", "the user declined")) else done).append(r)
                restored |= c["name"] == "volume"
            self._last_actions = None
            log.info("undo %r: %d undone, %d not", last["text"], len(done), len(cant))
            if not done:
                return "I can't undo that one: " + "; ".join(dict.fromkeys(cant)) + "." if cant else None
            return "Undone!" + (" But " + "; ".join(dict.fromkeys(cant)) + "." if cant else "")
        calls = redo.more_of(last["calls"]) if more else last["calls"]
        if not calls:
            return None
        if any(c["name"] in DESTRUCTIVE for c in calls):
            return "I'd rather not repeat that one automatically. Tell me again what to do, and I'll do it."
        results = [await self.tools.call(c["name"], c["args"]) for c in calls]
        log.info("%s %r: %s", "more" if more else "again", last["text"], results)
        bad = [r for r in results if r.startswith(("error", "refused", "the user declined"))]
        last["t"] = time.time()
        return ("A bit more!" if more else "Done again!") if not bad else "That didn't work this time: " + bad[0]

    async def _run_steps(self, steps: list[str], request: str = "") -> str:
        """Do several requests in order (a split-up request or a routine); each one is learned on its own.
        For a split-up request she checks at the end that every part of it got done."""
        replies = []
        start_all = len(self._trace)
        for i, step in enumerate(steps):
            await self.avatar.send(cmd="status", text=f"Step {i + 1}/{len(steps)}: {step[:50]}")
            start = len(self._trace)
            reply, src = await self._respond(step, nested=True)
            log.info("step %d/%d %r → %r (%s)", i + 1, len(steps), step, reply, src)
            if self.learning:
                await self.learning.done(step, self._trace[start:], reply, src)
            if reply:
                replies.append(reply)
            if re.search(r"\b(declined|couldn'?t|can'?t|failed|error)\b", reply or "", re.I) and i < len(steps) - 1:
                replies.append(f"I stopped there, so I didn't do: {', '.join(steps[i + 1:])}.")
                return " ".join(replies)
        if request:  # the planner sometimes drops a part
            did = [(_call(c), c["result"]) for c in self._trace[start_all:]] + [("her reply", r) for r in replies]
            if missing := self.local.check(request, did):
                log.info("self-check after steps: not done yet: %s", missing)
                start = len(self._trace)
                reply, src = await self._respond(missing, nested=True)
                if self.learning:
                    await self.learning.done(missing, self._trace[start:], reply, src)
                if reply:
                    replies.append(reply)
        return " ".join(replies)

    async def _teach_routine(self, name: str, rest: str) -> str:
        steps = await self.local.plan(rest) if await self.local.available() else \
            [x.strip() for x in re.split(r",\s*(?:and\s+)?|\s+and\s+(?:then\s+)?", rest) if x.strip()]
        r = await self.tools.call("routine", {"action": "save", "name": name, "steps": steps})
        if not r.startswith("saved"):
            return r
        return f"Got it! When you say {name}, I'll " + ", then ".join(s[0].lower() + s[1:] for s in steps) + "."

    async def _right_now(self) -> str:
        """The 'Right now — focused window…; Discord chat open…' line; people in the open Discord chat are learned."""
        from . import awareness

        try:
            line, people = await awareness.snapshot(self.gamemode.game if self.gaming and self.gamemode else "",
                                                    self.timers)
        except Exception as e:  # never let this break a request
            log.debug("right now: %s", e)
            return ""
        if self.learning:
            known = {n.lower() for n in self.learning.lexicon.names}
            new = [p for p in people if p.lower() not in known]
            for p in new:
                self.learning.lexicon.learn(p, "discord", weight=2, save=False)
            if new:
                self.learning.lexicon.save()
        return line

    async def _respond(self, text: str, nested: bool = False) -> tuple[str, str]:
        """→ (reply, who answered: fast | local | replay | ask | claude | steps | routine | none)"""
        from .actions import find_routine

        if not nested and (m := TEACH.match(text)):
            reply = await self._teach_routine(m[1].strip(), m[2].strip())
            self.memory.add(text, reply)
            return reply, "routine"
        if not nested and (steps := find_routine(text)):
            log.info("routine %r: %s", text, steps)
            self.tools.said = ""  # the user taught these steps: "play rain sounds" in "goodnight" really plays
            reply = await self._run_steps(steps)
            self.memory.add(text, reply)
            return reply, "routine"
        # browser tasks in several steps: "open brave, look up X and click the first video"
        if not nested and self.cfg.brain.get("local_enabled", True) and (steps := browse.parse(text)):
            log.info("browser task: %s", steps)
            await self.avatar.send(cmd="status", text="Doing it in the browser…")

            async def other(part: str) -> str:
                return (await self._respond(part, nested=True))[0]
            local = self.local if await self.local.available() else None
            reply = await browse.Task(self.tools, other, local).run(steps)
            self.memory.add(text, reply)
            return reply, "browser"
        # several things in one request: split first, or the fast path would only catch one of them
        if not nested and MULTI.search(text) and not COMPOUND.search(text) \
                and not re.match(r"^\s*(hey )?(ask|use) claude\b", text, re.I) \
                and self.cfg.brain.get("local_enabled", True) and await self.local.available():
            steps = await self.local.plan(text)
            if len(steps) > 1:
                log.info("split into %d steps: %s", len(steps), steps)
                reply = await self._run_steps(steps, text)
                return reply or "Done!", "steps"
        last = self.memory.turns[-1]["assistant"] if self.memory.turns else ""
        fast = await fastpath.handle(text, self.tools, self.usage, last)
        if fast is not None:
            if fast.control == "stop":
                return "", "fast"
            if fast.control == "hide":
                self.pinned = False
            if fast.control == "forget":
                self.memory.clear()
            if fast.control == "briefing":
                from . import briefing

                return await briefing.build(self.tools, self.cfg.persona.get("name", "Mio")), "fast"
            if fast.reply and fast.control is None:
                self.memory.add(text, fast.reply)  # so "yes" after "Want me to …?" has context
            return fast.reply, "fast"

        force_claude = re.match(r"^\s*(hey )?(ask|use) claude\b[,:]?\s*", text, re.I)
        query = text[force_claude.end():] if force_claude else text
        context = self.memory.context_block()
        reply, src = None, "none"
        hints = ("Speech recognition note: " + "; ".join(self._hints) + ". Act on what they probably said.") \
            if self._hints else ""
        now = await self._right_now()

        if not force_claude and self.cfg.brain.get("local_enabled", True) and await self.local.available():
            learned, replay = await self.learning.learned(query) if self.learning else ([], None)
            try:
                facts = "\n".join(x for x in (self.memory.facts_line(), now, hints) if x)
                if not replay and not self._no_asking and (q := self.learning.choice_question() if self.learning
                                                           else ""):
                    reply, src = q, "ask"  # two learned ways that would both *do* something: ask, don't guess
                else:
                    reply = await self.local.respond(query, facts, self.memory.messages(), self.memory.tools_used(),
                                                     learned, replay, no_asking=self._no_asking)
                    src = "replay" if self.local.replayed else "ask" if self.local.asked else "local"
                log.info("%s reply", src)
            except Escalate as e:
                log.info("escalating to Claude: %s", e)
                self._log_escalation(query, str(e))
            except Exception as e:
                log.warning("local brain failed: %s", e)

        if reply is None:
            if not self.claude.enabled():
                reply = "I can't do that one on my own, and Claude is turned off."
            elif not self.usage.allowed():
                reply = "I've used up today's Claude budget, so I can only do simple things until tomorrow."
            else:
                await self.avatar.send(cmd="state", state="thinking_hard")
                await self.avatar.send(cmd="status", text="Thinking it through with Claude…")
                try:
                    reply, cost = await self.claude.respond(query, "\n".join(x for x in (context, now, hints) if x))
                    self.usage.bump(cost)
                    src = "claude"
                except ClaudeError as e:
                    log.error("claude failed: %s", e)
                    reply = "Sorry, I couldn't reach Claude right now."

        if not reply:  # never just go silent
            reply = "Sorry, I got a bit lost there. Could you say that again?"
        self.memory.add(text, reply)
        self._last_activity = asyncio.get_running_loop().time()
        return reply, src

    # ── control socket ──────────────────────────────────────────────────────
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while line := await reader.readline():
                req = json.loads(line)
                resp = await self.dispatch(req)
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionError, json.JSONDecodeError):
            pass
        finally:
            writer.close()

    async def dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ptt":
            await self.ptt()
        elif cmd == "toggle":
            await self.toggle_pin()
        elif cmd == "show":
            self.pinned = True
            await self.show()
        elif cmd == "hide":
            self.pinned = False
            await self.avatar.send(cmd="hide")
            self.end_session()
        elif cmd == "stop":
            self.player.stop()
            self.recorder.cancel()
        elif cmd == "say":
            asyncio.create_task(self._say_then_idle(req.get("text", "")))
        elif cmd == "ask":  # typed (CLI, browser popup): one after another, else two requests share one trace
            async def ask(text: str, before: asyncio.Task | None = self._task):
                if before and not before.done():
                    await asyncio.wait([before])
                await self._interaction(text)
            self._task = asyncio.create_task(ask(req.get("text", "")))
        elif cmd == "clip":  # hotkey: no voice, just a chime and a notification
            result = await self.tools.call("clip", {"seconds": req["seconds"]} if req.get("seconds") else {})
            ok = result.startswith("saved")
            asyncio.create_task(self.beep(True) if ok else self.player.play(*chime(False)))
            asyncio.create_task(sh("notify-send", "-a", "Assistant", "-t", "4000",
                                   "Clip saved" if ok else "No clip", result.split(" to /")[0] if ok else result))
            return {"result": result}
        elif cmd == "escalations":
            summary, rows = self.escalation_report(int(req.get("days") or 7))
            return {"summary": summary, "items": rows[-30:]}
        elif cmd == "dream" and self.dreamer:  # start one now (for testing): `assistant dream`
            if self.dreamer.proc is None:
                asyncio.create_task(self.dreamer.dream(dry=bool(req.get("dry"))))
            return {"result": "dreaming (see ~/.local/share/assistant/dream/train.log)"}
        elif cmd == "forget":
            self.memory.clear()
        elif cmd == "status":
            month_n, month_usd = self.usage.month()
            return {"state": self.state, "pinned": self.pinned, "game_mode": self.gaming,
                    "claude_today": self.usage.today(),
                    "claude_limit": self.usage.limit,
                    "claude_cents_today": round(self.usage.cost_today() * 100, 2),
                    "claude_requests_month": month_n, "claude_cents_month": round(month_usd * 100, 2),
                    "facts": len(self.memory.facts),
                    "learned_skills": len(self.learning.skills.items) if self.learning else 0,
                    "learned_names": len(self.learning.lexicon.names) if self.learning else 0,
                    "vocabulary": len(self.learning.lexicon.words) if self.learning else 0,
                    "local_model": self.local.cfg["ollama_model"]}
        elif cmd == "learned":
            if not self.learning:
                return {"error": "learning is off ([learning] enabled = false)"}
            from .learning import example_line

            from .dream import state

            sk = self.learning.skills.items
            return {"dream": (self.dreamer.due() if self.dreamer else "") or "due when you're away", "model":
                    self.local.cfg["ollama_model"], "last_dream": state(), "skills": len(sk), "from_claude": sum(s.get("src") == "claude" for s in sk),
                    "names": self.learning.lexicon.prompt_names(40), "fixes": self.learning.lexicon.fixes,
                    "words": sorted(self.learning.lexicon.words.items(), key=lambda kv: -kv[1])[:40],
                    "vocabulary": len(self.learning.lexicon.words),
                    "recent": [example_line(x) for x in sk[-15:]]}
        elif cmd == "tools":
            return {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema()}
                              for t in self.tools.specs()]}
        elif cmd == "tool":
            return {"result": await self.tools.call(req["name"], req.get("args") or {})}
        else:
            return {"error": f"unknown command {cmd}"}
        return {"ok": True}

    async def _timer_due(self, item: dict, late: bool):
        from .timers import say_duration, you

        label = you(item["label"])
        if item["kind"] == "timer":
            what = f"{say_duration(item['seconds'], adjective=True)} timer" if item.get("seconds") else "timer"
            text = f"Your {what}{' for the ' + label if label else ''} is done!"
        elif label:
            text = f"Reminder: {label}!"
        else:
            text = f"It's {dt.datetime.now().strftime('%-H:%M')}, here's your alarm!"
        if late:
            text = "While I was away, " + text[0].lower() + text[1:]
        asyncio.create_task(sh("notify-send", "-a", "Assistant", "-u", "critical", "Mio", text))
        for _ in range(60):  # don't talk over an ongoing conversation
            if self.state == "idle":
                break
            await asyncio.sleep(1)
        await self.set_state("speaking")
        await self.player.play(*chime(True))
        await self.player.play(*chime(True))
        await self._say_then_idle(text)

    def _on_wake(self):
        if self.state == "idle":
            asyncio.create_task(self.ptt())

    async def _auto_briefing(self):
        """Once per day, the first time the PC is up in the morning."""
        from . import briefing

        b = self.cfg.briefing
        if not b.get("auto", True) or not briefing.due_today(b.get("from_hour", 5), b.get("until_hour", 12)):
            return
        await asyncio.sleep(b.get("delay_s", 25))  # let the desktop and models settle after login
        if self.state != "idle":
            return
        briefing.mark_done()
        log.info("morning briefing")
        await self.set_state("thinking")
        await self.show()
        try:
            text = await briefing.build(self.tools, self.cfg.persona.get("name", "Mio"))
            await self.say(text)
        finally:
            await self.set_state("idle")
            await self.hide(self.cfg.avatar.get("hide_after_s", 15))

    def _log_escalation(self, text: str, reason: str):
        import datetime as dt

        with config.ESCALATIONS.open("a") as f:
            f.write(json.dumps({"t": dt.datetime.now().isoformat(timespec="seconds"), "text": text,
                                "reason": reason}) + "\n")

    def escalation_report(self, days: int = 7, examples: int = 5) -> tuple[str, list[dict]]:
        import datetime as dt

        since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
        try:
            rows = [json.loads(l) for l in config.ESCALATIONS.read_text().splitlines() if l.strip()]
        except FileNotFoundError:
            rows = []
        rows = [r for r in rows if r["t"] >= since]
        if not rows:
            return f"In the last {days} days I handled everything myself, nothing went to Claude.", rows
        recent = "; ".join(f"“{r['text'][:60]}”" for r in rows[-examples:])
        return (f"In the last {days} days I passed {len(rows)} requests to Claude. The latest were: {recent}.", rows)

    async def _say_then_idle(self, text: str):
        await self.say(text)
        await self.set_state("idle")
        await self.hide(self.cfg.avatar.get("hide_after_s", 6))

    async def warmup(self):
        self._refresh_learning()
        await asyncio.gather(asyncio.to_thread(self.stt.load), asyncio.to_thread(self.tts.load))
        if self.gaming and not self._wake_in_game():  # a game was already running when we started
            await asyncio.to_thread(self.stt.unload)
        log.info("ready")

    async def run(self):
        sock = config.DAEMON_SOCK
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self.handle_client, path=str(sock))
        os.chmod(sock, 0o600)
        asyncio.create_task(self.warmup())
        loop = asyncio.get_running_loop()
        if self.cfg.wakeword.get("enabled", False):
            try:
                from .wakeword import WakeWord

                verify = None
                if self.cfg.wakeword.get("verify", True):
                    verify = lambda audio: self.stt._transcribe(audio, hotwords=False) if self.stt._model else ""
                    hinted = lambda audio: self.stt._transcribe(audio) if self.stt._model else ""
                self.wake = WakeWord(self.cfg.wakeword,
                                     on_wake=lambda: loop.call_soon_threadsafe(self._on_wake),
                                     is_busy=lambda: self.state != "idle" or (self.gaming and not self._wake_in_game()),
                                     verify=verify,
                                     verify_hinted=hinted if verify else None)
                self.wake.start()
            except Exception as e:
                log.warning("wake word disabled: %s", e)
        asyncio.create_task(self._auto_briefing())
        if self.dreamer:
            asyncio.create_task(self.dreamer.run())
        asyncio.create_task(self.timers.run())
        from . import riff

        riff.bridge = riff.Bridge()
        asyncio.create_task(riff.bridge.serve())
        if self.cfg.gamemode.get("enabled", True):
            from .gamemode import GameMode

            self.gamemode = GameMode(self.cfg.gamemode, self._on_gamemode)
            asyncio.create_task(self.gamemode.run())
        if self.cfg.clips.get("enabled", True) and self.cfg.clips.get("always", False):
            asyncio.create_task(self.clipper.start())
        try:
            from .tray import Tray

            asyncio.create_task(Tray(self).run())
        except Exception as e:
            log.warning("tray disabled: %s", e)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)
        async with server:
            await stop.wait()
        self.player.stop()
        self.recorder.cancel()
        if self.wake:
            self.wake.stop()
        await self.clipper.stop()


def main():
    logging.basicConfig(level=os.environ.get("ASSISTANT_LOG", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(Assistant().run())


if __name__ == "__main__":
    main()
