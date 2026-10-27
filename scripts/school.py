"""Mio's school: she does the lessons in tests/curriculum.json, gets graded, and learns from it.

    .venv/bin/python scripts/school.py            quiz → lessons → quiz again (stops/starts assistantd)
    .venv/bin/python scripts/school.py --quiz     just the quiz
    .venv/bin/python scripts/school.py tests/curriculum_typos.json   another curriculum (round 2: typos)

Her real brain (local model), learning and read-only tools run for real; everything that would change something
(opening apps, sending messages, volume, files…) is faked with a realistic result, so nothing happens on the PC.
Right answers are kept as wins by the normal learning; for wrong ones the lesson's correct calls are recorded
instead (src "teacher"), with her own words for the reply. The quiz uses other wordings and is never recorded.
"""
import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import assistant.daemon as D  # noqa: E402

CURRICULUM = Path(__file__).resolve().parent.parent / "tests" / "curriculum.json"
STRICT = {"action", "mode", "source", "site", "direction", "keys"}
WORDY = {"query", "who", "text", "game", "name"}


def matches(call: dict, want: dict) -> bool:
    if call["name"] != want["name"]:
        return False
    for k, v in want["args"].items():
        got = str(call["args"].get(k, "")).lower()
        if k in STRICT and got != str(v).lower():
            return False
        if k in WORDY and isinstance(v, str):
            words = [w for w in re.findall(r"[a-z0-9]+", v.lower()) if len(w) > 2 and w not in ("the", "newest", "video")]
            if words and not any(w in got for w in words):
                return False
    return True


def passed(item: dict, trace: list[dict]) -> bool:
    ok = set(item.get("ok", []))
    for want in item["teach"]:
        if not any(matches(c, want) or c["name"] in ok for c in trace):
            return False
    return True


async def main():
    quiz_only = "--quiz" in sys.argv
    running = subprocess.run(["systemctl", "--user", "is-active", "-q", "assistantd"]).returncode == 0
    if running:  # it keeps the learned data in memory and would overwrite what school writes
        subprocess.run(["systemctl", "--user", "stop", "assistantd"])
    try:
        a = D.Assistant()
        a.avatar.send = nop
        a.memory.facts = []
        a.memory.save = lambda: None
        a.learning.lexicon.hear = lambda *_a, **_k: None  # her vocabulary is the user's words, not the curriculum's
        a.learning.lexicon.from_tool = lambda *_a, **_k: None  # fake results ("… by Phonk") aren't real names
        a._log_escalation = lambda *_a: None  # her real escalation log is for real use
        stub(a)
        path = next((Path(x) for x in sys.argv[1:] if x.endswith(".json")), CURRICULUM)
        cur = json.loads(path.read_text())
        record = a.learning.skills.record

        async def quiz(label: str) -> float:
            async def no_record(*_a, **_k):
                return None
            a.learning.skills.record = no_record
            right = 0
            for item in cur["quiz"]:
                ok, _, _ = await attempt(a, item)
                right += ok
            a.learning.skills.record = record
            score = right / len(cur["quiz"])
            print(f"\nquiz {label}: {right}/{len(cur['quiz'])} = {score:.0%}\n", flush=True)
            return score

        before = await quiz("before")
        if quiz_only:
            return
        stats = {"right": 0, "taught": 0, "fast": 0, "missed": 0}
        for item in cur["lessons"]:
            ok, src, trace = await attempt(a, item)
            if src == "fast":
                stats["fast"] += 1
            elif ok:
                stats["right"] += 1
            elif len(item["teach"]) == 1:
                if a.learning.last_skill:
                    a.learning.skills.forget(a.learning.last_skill["id"])
                a._trace = []
                try:
                    reply = await a.local._replay(item["say"], {"calls": item["teach"]}, "", [], {})
                except RuntimeError as e:  # the right tool failed for real (e.g. nothing found): nothing to learn
                    print(f"   couldn't teach: {e}", flush=True)
                    continue
                await a.learning.skills.record(item["say"], a._trace, reply, "teacher")
                stats["taught"] += 1
                print(f"   taught: {item['teach'][0]['name']}  → {reply!r}", flush=True)
            else:
                stats["missed"] += 1
        print(f"\nlessons: {stats['right']} right on her own, {stats['taught']} taught, {stats['fast']} instant "
              f"(fast path), {stats['missed']} multi-step misses", flush=True)
        n = await a.learning.practice(a.local, limit=10000)  # homework: other wordings for what she knows
        print(f"practice: {n} new ways to say what she knows", flush=True)
        after = await quiz("after")
        print(f"quiz: {before:.0%} → {after:.0%}; she now knows {len(a.learning.skills.items)} requests", flush=True)
    finally:
        if running:
            subprocess.run(["systemctl", "--user", "start", "assistantd"])


async def attempt(a, item) -> tuple[bool, str, list[dict]]:
    a.end_session()
    a._trace = []
    a._asked = None
    said, _ = a.learning.correct(item["say"], voice=False)  # typed: typos and names get fixed first
    t = time.time()
    try:
        reply = await a.respond(said)
    except Exception as e:  # noqa: BLE001
        reply = f"(crashed: {e})"
    src = getattr(a, "last_src", "?")
    ok = src == "fast" or passed(item, a._trace)
    calls = ", ".join(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)[:60]})" for c in a._trace) or "-"
    print(f"{'✓' if ok else '✗'} {item['say']!r} [{src}, {time.time() - t:.1f}s] {calls}", flush=True)
    return ok, src, list(a._trace)


async def nop(**_m):
    pass


def stub(a):
    import tempfile

    import assistant.tools as T  # noqa: F401
    from assistant.timers import Timers

    # the fast path sets timers directly, not through the timer tool: give it a throwaway list (it still reports
    # them like a tool call, so they're graded)
    a.timers = a.tools.timers = Timers(Path(tempfile.mkdtemp()) / "timers.json", lambda *_: None)
    # read-only tools run for real; everything that changes something is faked with a realistic result
    REAL = {"system_info", "system_health", "weather", "web_research", "wikipedia",
            "arch_wiki", "world_clock", "game_news", "game_price", "package_info", "arch_news", "search_my_setup",
            "system_updates", "list_windows", "routine", "steam_library_list"}
    STUB = {
        "youtube_play": lambda query: f"playing “{query.title()} (Official Video)” by {query.title()}",
        "volume": lambda mode, value=None: "ok", "media": lambda action: "ok",
        "open": lambda target, app=None: f"opened {target} in the default app",
        "launch_app": lambda name: f"opened {name}", "launch_game": lambda name: f"starting {name}",
        "open_best_match": lambda query: f"opened https://example.org/{query.replace(' ', '-')} in the default app",
        "search_site": lambda site, query: f"opened https://{site}.com/search?q={query.replace(' ', '+')} in the default app",
        "discord": lambda action, who="", text="": (f"opened '{who}' in Discord; now showing: @{who} - Discord"
                                                    if action == "open" else "sent to @Alex" if action == "send"
                                                    else "Discord is showing: @Alex - Discord"),
        "press_keys": lambda keys, app="": f"pressed {keys}", "focus_window": lambda query: f"focused {query}",
        "type_text": lambda text, submit=False: "typed and pressed Enter" if submit else "typed",
        "see_screen": lambda question="": "Window: brave-browser — Cats\nI see: an orange cat asleep on a keyboard",
        "read_screen": lambda: "kitty — error: target not found: libfoo",
        "screen": lambda question="": ("Focused window: kitty — ~ - fish\nText on it:\n$ paru -S libfoo\nerror: target "
                                       "not found: libfoo" if not re.search(r"game|picture|image|look|see|cat|colou?r",
                                                                          question, re.I) else
                                       "Focused window: brave-browser — Cats\nI see: an orange cat asleep on a keyboard"),
        "trash": lambda path, inside=False: f"moved the items in {path} to the trash (the folder stays)" if inside else f"moved {path} to trash",
        "move_file": lambda src, dst: f"moved {src} to {dst}",
        "tidy_downloads": lambda apply=False: ("Plan for ~/Downloads: 12 to Images, 5 to Documents, 3 to Installers. "
                                               "Ask the user if they want it done (then apply=true).") if not apply
                                              else "moved 20 files: 12 to Images, 5 to Documents, 3 to Installers",
        "shell_panel": lambda command: "disabled" if "status" in command else "ok",
        "run_shell": None,  # real for read-only commands, faked otherwise (see below)
        "timer": lambda action, minutes=None, label="", at=None: (f"timer set for {minutes} minutes" if action == "set"
                                                                 else "no timers running"),
        "clip": lambda seconds=None: f"saved the last {seconds or 30} seconds to ~/Videos/Clips/Roblox.mp4",
        "phone": lambda action, text="": "ringing iPhone" if action == "ring" else "ok",
        "browser_video": lambda action, value=None: ("Video: Cooking pasta by Chef Mio (10:22)\nTranscript: today we make "
                                                     "carbonara with eggs, pecorino and guanciale, no cream"
                                                     if action == "about" else f"{action} done. Now: Cooking pasta at 1:02/10:22, playing, speed 1x"),
        "browser_page": lambda: "Title: niri 26.04 released\nURL: https://github.com/niri-wm/niri/releases\n\nniri 26.04 adds "
                                "blur, new animations and better screencasting.",
        "browser_tabs": lambda action, query="": ("- YouTube (youtube.com) [current]\n- GitHub (github.com)"
                                                   if action == "list" else f"{action} done"),
        "browser_click": lambda target: f"clicked {target}", "browser_scroll": lambda direction: f"scrolled {direction}",
        "browser_history": lambda query, days=7: f"- 2026-10-18 20:11: {query.title()} recipe — https://example.org/{query}",
        "install_updates": lambda: "Opened a terminal running 'paru -Syu'.", "notify": lambda title, body="": "ok",
        "niri_action": lambda action, arg="": "ok", "clipboard": lambda text=None: "https://github.com/niri-wm/niri",
        "find_files": lambda query="", kind="", days=None: (f"- ~/Documents/{(query or 'scan').replace(' ', '_')}"
                                                         f"{'.pdf' if kind in ('pdf', 'document', '') else '.png' if kind == 'image' else '.mp4'} (1 days ago)\n"
                                                         f"- ~/Downloads/{(query or kind or 'file').replace(' ', '-')}-2.pdf (3 days ago)"),
    "list_files": lambda path: "Screenshots/\nWallpapers/\nroblox-win.png\ncat.jpg",
    "remember": lambda fact: "remembered", "read_file": lambda path: "- buy milk\n- fix the niri config\n- clip the roblox win", "check_install": lambda: "looks safe: official repo package",
        "read_link": lambda url: "The page says niri 26.04 adds blur.", "read_clipboard_link": lambda: "niri release notes",
        "terminal_text": lambda: "$ paru -S foo\nerror: target not found: foo", "write_file": lambda path, content: "wrote",
        "install_packages": lambda packages: "Opened a terminal to install them.",
    }
    SAFE_SHELL = __import__("re").compile(r"^\s*(ls|cat|df|du|free|uptime|ip|ps|pacman -Q|uname|lsblk|nproc|whoami|"
                                          r"date|hostname|wc|systemctl (is-active|status)|nvidia-smi|sensors|pgrep)\b")
    for name, t in a.tools.tools.items():
        if name in REAL:
            continue
        if name == "run_shell":
            real = t.fn
            async def fn(command, _real=real):
                if SAFE_SHELL.match(command) and not __import__("re").search(r"[;&>]|\brm\b|\bkill", command):
                    return await _real(command=command)
                return "exit 0\ndone (no output)"
            t.fn = fn
            continue
        f = STUB.get(name) or (lambda **k: "ok")
        async def fn(_f=f, **k): return _f(**k)
        t.fn = fn
    async def right_now():  # the same pretend desktop every run, so grades don't depend on what's open
        return ("Right now — focused window: brave-origin — YouTube; Discord chat open: Alex; "
                "media playing: Lofi Girl — lofi hip hop radio.")
    a._right_now = right_now

    async def no_claude(query, context):
        raise D.ClaudeError("Claude is off at school")
    a.claude.respond = no_claude

    async def yes(summary):
        return True
    a.tools.confirm = yes


if __name__ == "__main__":
    asyncio.run(main())
