"""More ways to act on the PC: keyboard shortcuts, Discord, finding/tidying files, local eyes, routines.

Registered into the ToolBox by `register(box)` (end of ToolBox._register). Same gate and audit log as every
other tool; anything that sends something or moves many files asks first.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import re
import time
from pathlib import Path

import httpx

from .config import STATE_DIR

S = {"type": "string"}
I = {"type": "integer"}
ROUTINES_FILE = STATE_DIR / "routines.json"

# spoken key names → xkb keysyms for wtype
KEYS = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "space",
        "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "up": "Up", "down": "Down", "left": "Left",
        "right": "Right", "home": "Home", "end": "End", "pageup": "Prior", "page up": "Prior", "pagedown": "Next",
        "page down": "Next", "plus": "plus", "minus": "minus", "comma": "comma", "period": "period",
        "slash": "slash"}
MODS = {"ctrl": "ctrl", "control": "ctrl", "strg": "ctrl", "shift": "shift", "alt": "alt", "super": "logo",
        "win": "logo", "windows": "logo", "meta": "logo", "mod": "logo", "logo": "logo"}

KINDS = {"Images": "jpg jpeg png gif webp bmp svg heic avif", "Videos": "mp4 mkv webm mov avi m4v",
         "Audio": "mp3 flac wav ogg opus m4a aac", "Documents": "pdf doc docx odt txt md rtf xls xlsx ods ppt pptx odp csv epub",
         "Archives": "zip rar 7z tar gz xz zst bz2 tgz", "Installers": "pkg.tar.zst deb rpm appimage flatpakref exe msi iso",
         "Code": "py js ts json toml yaml yml sh c cpp rs go html css"}
EXT = {e: k for k, v in KINDS.items() for e in v.split()}
# questions about how something looks, not what it says: the screen tool also uses the eyes for these
VISUAL = re.compile(r"\b(picture|image|photo|pic|look(s|ing)?|see|game|colou?rs?|button|icon|logo|who is|"
                    r"person|character|map|chart|graph|layout|design|meme|cat|dog|animal|describe|showing)\b", re.I)


def _kind(p: Path) -> str:
    name = p.name.lower()
    for e in sorted(EXT, key=len, reverse=True):  # "pkg.tar.zst" before "zst"
        if name.endswith("." + e):
            return EXT[e]
    return "Other"


def _ago(t: float) -> str:
    d = time.time() - t
    return "just now" if d < 120 else f"{int(d // 60)} min ago" if d < 3600 else f"{int(d // 3600)} h ago" \
        if d < 86400 else f"{int(d // 86400)} days ago"


def register(box):
    from .tools import sh

    @box._add("press_keys", "Press keyboard keys/shortcuts, e.g. 'ctrl+k', 'enter', 'ctrl+shift+t', 'alt+f4'; "
              "several combos separated by spaces are pressed in order ('ctrl+a backspace'). app = the window to "
              "focus first (e.g. 'kitty', 'brave', 'discord'); without it the keys go to the focused window.",
              {"keys": S, "app": S}, ["keys"])
    async def press_keys(keys: str, app: str = "") -> str:
        if app:
            r = await box.tools["focus_window"].fn(query=app)
            if not r.startswith("focused"):
                return f"error: {r}; open it first"
            await asyncio.sleep(0.3)
        for combo in keys.strip().lower().split():
            parts = [p.strip() for p in re.split(r"\s*\+\s*", combo) if p.strip()]
            if not parts:
                continue
            mods = [MODS[p] for p in parts[:-1] if p in MODS]
            key = KEYS.get(parts[-1], parts[-1] if len(parts[-1]) == 1 or re.fullmatch(r"f\d{1,2}", parts[-1])
                           else parts[-1])
            key = key.upper() if re.fullmatch(r"f\d{1,2}", key) else key
            argv = ["wtype"]
            for m in mods:
                argv += ["-M", m]
            argv += ["-k", key]
            for m in reversed(mods):
                argv += ["-m", m]
            code, out = await sh(*argv)
            if code:
                return f"error: couldn't press {combo}: {out}"
            await asyncio.sleep(0.15)
        return f"pressed {keys}"

    async def _discord_window() -> dict | None:
        code, out = await sh("niri", "msg", "-j", "windows", limit=None)
        for w in json.loads(out) if code == 0 else []:
            if "discord" in (w.get("app_id") or "").lower() or (w.get("title") or "").endswith("Discord"):
                return w
        return None

    @box._add("discord", "Discord desktop app. action: open (go to a DM, server or channel by name with Discord's "
              "quick switcher; 'who' = the name), send (type 'text' into the open chat and send it; the user "
              "confirms first), read (the chat title that's open).",
              {"action": {"type": "string", "enum": ["open", "send", "read"]}, "who": S, "text": S}, ["action"])
    async def discord(action: str, who: str = "", text: str = "") -> str:
        w = await _discord_window()
        if w is None:
            if action != "open":
                return "Discord isn't open"
            await box.tools["launch_app"].fn(name="discord")
            for _ in range(40):  # it takes a few seconds to start
                await asyncio.sleep(0.5)
                if w := await _discord_window():
                    break
            else:
                return "started Discord, but its window didn't show up yet"
            await asyncio.sleep(2)
        await sh("niri", "msg", "action", "focus-window", "--id", str(w["id"]))
        await asyncio.sleep(0.3)
        if action == "read":
            return f"Discord is showing: {w.get('title')}"
        if action == "open":
            if not who:
                return "focused Discord"
            await press_keys("ctrl+k")
            await asyncio.sleep(0.4)
            await sh("wtype", "--", who)
            await asyncio.sleep(0.7)
            await press_keys("enter")
            await asyncio.sleep(0.6)
            w = await _discord_window() or w
            return f"opened '{who}' in Discord; now showing: {w.get('title')}"
        if not text:
            return "error: send needs 'text'"
        chat = re.sub(r"\s*[-|]\s*Discord$", "", w.get("title") or "the open chat")
        if not await box.confirm(f"Send “{text}” to {chat} on Discord"):
            return "the user declined"
        await press_keys("escape")  # closes popups; the message box keeps focus
        await sh("wtype", "--", text)
        await asyncio.sleep(0.2)
        await press_keys("enter")
        return f"sent to {chat}"

    @box._add("find_files", "Find files in the user's home, newest first. query = the name words the user said "
              "('tax', 'resume', 'screenshot'); kind = image, video, audio, document, pdf, archive, installer or code; "
              "days = only changed in the last N days (only if they said when). E.g. 'my tax pdf' → query 'tax', "
              "kind 'pdf'.",
              {"query": S, "kind": S, "days": I}, [])
    async def find_files(query: str = "", kind: str = "", days: int | None = None) -> str:
        argv = ["fd", "--type", "f", "--absolute-path", "--exclude", ".*", "--exclude", "node_modules",
                "--exclude", ".venv", "--exclude", "venv", "--exclude", "__pycache__", "--max-results", "2000"]
        k = kind.lower().rstrip("s")
        exts = ["pdf"] if k == "pdf" else next((v.split() for n, v in KINDS.items() if n.lower().rstrip("s") == k), [])
        for e in exts:
            argv += ["-e", e]
        if days:
            argv += ["--changed-within", f"{days}d"]
        words = [w for w in re.findall(r"\w+", query.lower()) if w not in ("the", "a", "my", "file", "files")]
        argv += [".*".join(map(re.escape, words)) if words else ".", str(Path.home())]
        code, out = await sh(*argv, timeout=20, limit=None)
        paths = [Path(p) for p in out.splitlines() if p.strip()]
        if not paths and days:  # "the one I downloaded recently" is often older than the model guessed
            newest = await find_files(query, kind, None)
            if not newest.startswith("no files"):
                return f"nothing from the last {days} days; the newest matches are:\n{newest}"
        if not paths:
            return f"no files found for '{query or kind}'" + (f" in the last {days} days" if days else "")
        paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        home = str(Path.home())
        return "\n".join(f"- {str(p).replace(home, '~')} ({_ago(p.stat().st_mtime)})" for p in paths[:10])

    @box._add("tidy_downloads", "Sort ~/Downloads into folders by kind (Images, Videos, Documents, Archives, "
              "Installers, ...). apply=false (default) only shows the plan; apply=true moves the files after the user "
              "confirms. Files from the last hour are left alone.", {"apply": {"type": "boolean"}}, [])
    async def tidy_downloads(apply: bool = False) -> str:
        root = Path.home() / "Downloads"
        plan: dict[str, list[Path]] = {}
        for p in root.iterdir():
            if p.is_file() and not p.name.startswith(".") and time.time() - p.stat().st_mtime > 3600 and \
                    not p.name.endswith((".part", ".crdownload")):
                plan.setdefault(_kind(p), []).append(p)
        if not plan:
            return "Downloads is already tidy (no loose files older than an hour)"
        summary = ", ".join(f"{len(v)} to {k}" for k, v in sorted(plan.items(), key=lambda kv: -len(kv[1])))
        if not apply:
            return f"Plan for ~/Downloads: {summary}. Ask the user if they want it done (then apply=true)."
        if not await box.confirm(f"Tidy Downloads: {summary}"):
            return "the user declined"
        moved = 0
        for k, files in plan.items():
            (root / k).mkdir(exist_ok=True)
            for p in files:
                dst = root / k / p.name
                if dst.exists():
                    dst = dst.with_name(f"{dst.stem} ({dt.datetime.now():%Y-%m-%d %H%M%S}){dst.suffix}")
                p.rename(dst)
                moved += 1
        return f"moved {moved} files: {summary}"

    async def _eyes(path: Path, question: str) -> str:
        """Ask the small local vision model about a screenshot; '' + error text when it can't."""
        img = await __import__("assistant.screen", fromlist=["for_vision"]).for_vision(path)
        cfg = box.vision
        prompt = (question or "Describe what is on this screen.") + \
            " Answer in 1-3 short sentences. Quote any important text exactly."
        brain, eyes = cfg.get("ollama_model", "qwen3.5:9b"), cfg.get("vision_model", "qwen3.5:9b")
        async with httpx.AsyncClient(base_url=cfg.get("ollama_url", "http://127.0.0.1:11434"), timeout=90) as c:
            if eyes != brain:  # a separate vision model: make room, else it runs half on the CPU (5x slower)
                code, free = await sh("nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits")
                if code or int(free.split()[0]) < 4500:
                    await c.post("/api/generate", json={"model": brain, "keep_alive": 0})
            r = await c.post("/api/chat", json={
                "model": eyes, "stream": False, **({"think": False} if eyes == brain else {}),
                "keep_alive": cfg.get("ollama_keep_alive", "5m") if eyes == brain else "30s",
                "messages": [{"role": "user", "content": prompt,
                              "images": [base64.b64encode(img.read_bytes()).decode()]}],
                "options": {"num_predict": 200, "temperature": 0.2}})
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    @box._add("see_screen", "Look at the focused window with Mio's own (local) eyes and answer a question about "
              "it: pictures, layouts, what's shown, which button to press. For plain text prefer read_screen.",
              {"question": S}, [])
    async def see_screen(question: str = "") -> str:
        from . import screen

        if box.gaming:
            return "error: my local eyes are off during games (they'd take GPU memory); use look_at_screen"
        path, win = await screen.capture()
        if not path:
            return "screenshot failed"
        try:
            answer = await _eyes(path, question)
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
            return f"error: local vision failed ({e}); use look_at_screen"
        return f"Window: {win.get('app_id')} — {win.get('title')}\nI see: {answer or '(nothing)'}"

    @box._add("screen", "Everything about the user's screen: what's in the focused window, 'this', 'this error', "
              "'what am I looking at', my terminal, a picture, a game. Reads its text (exact text for the "
              "terminal) and, for anything visual, looks at it with Mio's eyes. question = what the user wants "
              "to know. For a web page or YouTube video in the browser use browser_page / browser_video instead.",
              {"question": S}, [])
    async def screen_tool(question: str = "") -> str:
        from . import screen
        from .pkgcheck import read_terminal

        path, win = await screen.capture()
        app = win.get("app_id") or ""
        out = f"Focused window: {app} — {win.get('title')}"
        text = ""
        if app == "kitty":  # exact text beats OCR for terminals
            text, src = await read_terminal()
            text = text[-3500:] if src == "kitty" else ""
        if not text and path:
            text = (await screen.ocr(path))[:5000]
        if text.strip():
            out += f"\nText on it:\n{text}"
        # pictures, games, layouts — or hardly any text to go on: use the eyes too
        if path and (VISUAL.search(question) or len(text.strip()) < 80):
            if box.gaming:
                out += "\n(my eyes are off during games)"
            else:
                try:
                    out += f"\nI see: {await _eyes(path, question) or '(nothing)'}"
                except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
                    out += f"\n(looking failed: {e})"
        return out if path or text else "error: I couldn't take a screenshot"

    def _load() -> dict:
        try:
            return json.loads(ROUTINES_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @box._add("routine", "The user's routines: a name that runs several requests in order. action: save (name + "
              "steps, each a short request like 'open discord'), run (name), list, delete (name).",
              {"action": {"type": "string", "enum": ["save", "run", "list", "delete"]}, "name": S,
               "steps": {"type": "array", "items": S}}, ["action"])
    async def routine(action: str, name: str = "", steps: list[str] | None = None) -> str:
        items = _load()
        key = re.sub(r"[^\w ]", "", name.lower()).strip()
        if action == "list":
            return "; ".join(f"{k}: {', '.join(v)}" for k, v in items.items()) or "no routines yet"
        if not key:
            return "error: routine needs 'name'"
        if action == "save":
            steps = [s.strip() for s in steps or [] if s.strip()]
            if not steps:
                return "error: save needs 'steps'"
            items[key] = steps
            ROUTINES_FILE.write_text(json.dumps(items, indent=1, ensure_ascii=False))
            return f"saved routine '{key}': {', '.join(steps)}"
        if key not in items:
            return f"no routine called '{key}'" + (f"; there are: {', '.join(items)}" if items else "")
        if action == "delete":
            items.pop(key)
            ROUTINES_FILE.write_text(json.dumps(items, indent=1, ensure_ascii=False))
            return f"deleted routine '{key}'"
        if box.run_steps is None:
            return "error: routines can only run inside the assistant"
        return await box.run_steps(items[key])


def find_routine(text: str) -> list[str] | None:
    """'gaming time' / 'start gaming time' / 'run the goodnight routine' → its steps."""
    t = re.sub(r"[^\w ]", "", text.lower()).strip()
    t = re.sub(r"^(hey )?mio ", "", t)
    t = re.sub(r"^(please |can you |could you )?(start |run |do |begin )?(the |my )?", "", t)
    t = re.sub(r" (routine|mode|please)$", "", t)
    return _load_all().get(t)


def _load_all() -> dict:
    try:
        return json.loads(ROUTINES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
