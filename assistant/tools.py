"""Desktop tools, shared by the fast-path, Ollama and Claude (via MCP).

Every call goes through `ToolBox.call`, which applies the permission gate
and writes the audit log. Outputs are truncated to keep token use low.
"""
from __future__ import annotations

import asyncio
import configparser
import datetime as dt
import json
import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .config import AUDIT_LOG
from .pkgcheck import assess_install, read_terminal

log = logging.getLogger("tools")
MAX_OUT = 1500

ConfirmFn = Callable[[str], Awaitable[bool]]


async def sh(*argv: str, timeout: float = 15, input_: bytes | None = None, shell: bool = False,
             limit: int | None = MAX_OUT) -> tuple[int, str]:
    if shell:
        proc = await asyncio.create_subprocess_shell(
            argv[0], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE if input_ else asyncio.subprocess.DEVNULL, cwd=Path.home())
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE if input_ else asyncio.subprocess.DEVNULL, cwd=Path.home())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input_), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s"
    except asyncio.CancelledError:
        proc.kill()
        raise
    text = out.decode(errors="replace").strip()
    if limit and len(text) > limit:
        text = text[:limit] + f"\n…[{len(text) - limit} more chars truncated]"
    return proc.returncode or 0, text


# ── .desktop application index ──────────────────────────────────────────────
def _desktop_dirs() -> list[Path]:
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs = [Path.home() / ".local/share/applications"] + [Path(d) / "applications" for d in data_dirs]
    dirs += [Path.home() / ".local/share/flatpak/exports/share/applications",
             Path("/var/lib/flatpak/exports/share/applications")]
    return [d for d in dirs if d.is_dir()]


def app_index() -> dict[str, dict]:
    apps: dict[str, dict] = {}
    for d in _desktop_dirs():
        for f in d.glob("*.desktop"):
            if f.name in apps:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false") == "true" or e.get("Type") != "Application" or "Exec" not in e:
                continue
            apps[f.name] = {"name": e.get("Name", f.stem), "generic": e.get("GenericName", ""),
                            "keywords": e.get("Keywords", ""), "exec": e["Exec"], "id": f.name}
    return apps


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_app(query: str) -> dict | None:
    q = query.lower().strip()
    q = re.sub(r"^(the|my)\s+|\s+(app|application|program|browser)$", "", q)
    best, score = None, 0
    for app in app_index().values():
        name = app["name"].lower()
        s = 0
        if name == q:
            s = 100
        elif name.startswith(q):
            s = 80
        elif q in name:
            s = 60
        elif q in app["id"].lower():
            s = 50
        elif q == app["generic"].lower() or q in [k.strip().lower() for k in app["keywords"].split(";")]:
            s = 30
        if s > score:
            best, score = app, s
    if best is None and len(_norm(q)) >= 4:  # misheard names: "frosttrap" → Froststrap
        import difflib

        apps = list(app_index().values())
        names = {_norm(a["name"]): a for a in apps}
        match = difflib.get_close_matches(_norm(q), list(names), n=1, cutoff=0.75)
        best = names[match[0]] if match else None
    return best


SITES = {"youtube": "https://www.youtube.com", "github": "https://github.com", "reddit": "https://www.reddit.com",
         "twitch": "https://www.twitch.tv", "gmail": "https://mail.google.com", "google": "https://www.google.com",
         "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.de", "wikipedia": "https://en.wikipedia.org",
         "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "arch wiki": "https://wiki.archlinux.org",
         "aur": "https://aur.archlinux.org", "twitter": "https://x.com", "x": "https://x.com",
         "instagram": "https://www.instagram.com", "spotify": "https://open.spotify.com",
         "whatsapp": "https://web.whatsapp.com", "protondb": "https://www.protondb.com",
         # the user's own favourites (from their browser history)
         "tiktok": "https://www.tiktok.com", "roblox": "https://www.roblox.com", "modrinth": "https://modrinth.com",
         "pinterest": "https://www.pinterest.com", "youtube music": "https://music.youtube.com",
         "grok": "https://grok.com", "netlify": "https://app.netlify.com", "proton mail": "https://mail.proton.me",
         "proton": "https://mail.proton.me", "aniworld": "https://aniworld.to", "booth": "https://booth.pm"}


def to_url(target: str) -> str | None:
    """'youtube' / 'youtube.com' / 'https://…' → URL, else None."""
    t = target.strip().strip("<>\"'").rstrip("/.")
    if re.match(r"^(https?|mailto):", t):
        return t
    key = re.sub(r"^(the |my )|( website| site| page| homepage)$", "", t.lower()).strip()
    if key in SITES:
        return SITES[key]
    if re.fullmatch(r"(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(/\S*)?", t.lower()):
        return "https://" + t


def _exec_argv(exec_line: str) -> list[str]:
    argv = [a for a in shlex.split(exec_line) if not re.fullmatch(r"%[fFuUdDnNickvm]", a)]
    return [re.sub(r"%[fFuUdDnNickvm]", "", a) for a in argv]


# site search pages for 'search X on <site>'
def wants_video(said: str) -> bool:
    """Did the user ask for a video to play, not just to look something up? 'open brave and look up X' isn't."""
    t = re.sub(r"\bopen(?: up)? (?:brave(?: origins?)?|(?:the |my |a )?browser|youtube|(?:a |new )*tabs?)\b", "", said.lower())
    return bool(re.search(r"\b(play|watch|listen to|put on|pull up|bring up|start|open(?: up)?|show me|(?:the |first |top )"
                          r"(?:video|one|result))\b", t))


SEARCH = {"youtube": "https://www.youtube.com/results?search_query=", "amazon": "https://www.amazon.de/s?k=",
          "google": "https://www.google.com/search?q=", "reddit": "https://www.reddit.com/search/?q=",
          "github": "https://github.com/search?q=", "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
          "twitch": "https://www.twitch.tv/search?term=", "steam": "https://store.steampowered.com/search/?term=",
          "the aur": "https://aur.archlinux.org/packages?K=", "aur": "https://aur.archlinux.org/packages?K=",
          "duckduckgo": "https://duckduckgo.com/?q=", "brave": "https://search.brave.com/search?q=",
          "tiktok": "https://www.tiktok.com/search?q=", "modrinth": "https://modrinth.com/mods?q=",
          "pinterest": "https://www.pinterest.com/search/pins/?q=", "roblox": "https://www.roblox.com/discover/?Keyword=",
          "youtube music": "https://music.youtube.com/search?q="}


def _count(folder: Path, stop: int) -> int:
    """Files and folders inside, counting no further than `stop` (big trees would take a while)."""
    n = 0
    try:
        for _ in folder.rglob("*"):
            n += 1
            if n >= stop:
                break
    except OSError:
        pass
    return n


def _coerce(value, spec: dict):
    """The model sometimes sends numbers and flags as text (arch_news count="3" crashed on 2026-10-27)."""
    kind = spec.get("type")
    if isinstance(value, str):
        v = value.strip().lower()
        if kind == "integer" and re.fullmatch(r"[+-]?\d+(?:\.0*)?", v):
            return int(float(v))
        if kind == "number" and re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)", v):
            return int(v) if v.lstrip("+-").isdigit() else float(v)
        if kind == "boolean" and v in ("true", "false", "yes", "no"):
            return v in ("true", "yes")
    elif kind == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ── tool definitions ────────────────────────────────────────────────────────
@dataclass
class Tool:
    name: str
    description: str
    params: dict  # JSON-schema "properties"
    required: list[str]
    fn: Callable[..., Awaitable[str]]
    risk: str = "safe"  # safe | confirm | shell

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": self.required}


class ToolBox:
    def __init__(self, sec: dict, confirm: ConfirmFn, remember: Callable[[str], None] | None = None,
                 lookup: dict | None = None):
        self.sec = sec
        self.lookup = lookup or {}
        self.on_tool: Callable[[str, dict], Awaitable[None]] | None = None  # e.g. show "Searching…" bubble
        self.on_result: Callable[[str, dict, str], None] | None = None     # e.g. session memory
        self.knowledge = None  # assistant.knowledge.Knowledge, set by the daemon
        self.timers = None  # assistant.timers.Timers, set by the daemon
        self.clipper = None  # assistant.clips.Clipper, set by the daemon
        self.launchers: dict[str, list[str]] = {}  # [games] in the config
        self.gaming = False  # set by the daemon; some tools stay off the GPU during games
        self.said = ""  # the user's words for the current request (set by the daemon; "" = replay/routine)
        self.vision: dict = {}  # [brain] config, for see_screen's local vision model
        self.run_steps: Callable[[list[str]], Awaitable[str]] | None = None  # routines, set by the daemon
        self.confirm = confirm
        self.remember_fn = remember
        self.roots = [Path(os.path.expanduser(r)).resolve() for r in sec.get("file_roots", [])]
        self.tools: dict[str, Tool] = {}
        self._register()

    # ── gate ────────────────────────────────────────────────────────────────
    def _audit(self, name: str, args: dict, decision: str, result: str = ""):
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps({"t": dt.datetime.now().isoformat(timespec="seconds"), "tool": name,
                                "args": args, "decision": decision, "result": result[:200]}) + "\n")

    def _shell_policy(self, cmd: str) -> str:
        if any(re.search(p, cmd) for p in self.sec.get("shell_deny_patterns", [])):
            return "deny"
        if self._deletes_main_folder(cmd):
            return "deny"
        if any(re.search(p, cmd) for p in self.sec.get("shell_confirm_patterns", [])):
            return "confirm"
        # every command in a pipeline / chain must be allow-listed ('a|b' inside quotes is an argument)
        unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cmd)
        if re.search(r"\$\(|`", unquoted):  # command substitution hides what really runs
            return "confirm"
        words = [seg.strip().split()[0] for seg in re.split(r"\|\||&&|[|;&]", unquoted) if seg.strip()]
        allow = set(self.sec.get("shell_allow", []))
        return "allow" if words and all(w in allow for w in words) else "confirm"

    def protected(self, p: str | Path) -> bool:
        """Folders that must never be deleted or moved as a whole: home, the allowed roots (Downloads,
        Documents…), dotfolders right under home, anything above them and everything outside home."""
        path = Path(os.path.expanduser(str(p))).resolve()
        home = Path.home().resolve()
        if path == home or home not in path.parents:
            return True
        if path in self.roots or any(path in r.parents for r in self.roots):
            return True
        return path.parent == home and path.name.startswith(".")  # ~/.config, ~/.local, ~/.ssh …

    MAX_VOICE_DELETE = 500  # files; bigger deletions are for the file manager, not a spoken "yes"

    def _trash_check(self, path: str, inside: bool) -> str:
        """'' if trash may ask the user, else why it's refused."""
        p = Path(os.path.expanduser(path or "")).resolve()
        home = Path.home().resolve()
        if not inside and self.protected(p):
            return (f"refused: {path} is a protected folder and is never deleted as a whole. To delete what's in it, "
                    f"use trash(path=\"{path}\", inside=true); or name the files.")
        if inside and (p == home or home not in p.parents or (p.parent == home and p.name.startswith("."))):
            return f"refused: {path} can never be emptied."
        if inside and p.is_dir() and not any(p.iterdir()):
            return f"{p.name} is already empty, nothing to delete"
        n = _count(p, self.MAX_VOICE_DELETE + 1) if p.is_dir() else 1
        if n > self.MAX_VOICE_DELETE:
            return (f"refused: that's {n} files, too many to delete by voice. Tell the user to do it in the file "
                    "manager if they really want to.")
        return ""

    def _deletes_main_folder(self, cmd: str) -> bool:
        """`rm -r ~/Downloads`, `gio trash ~`, `rm -rf *` (runs in home), `mv ~/Documents x`, `find ~ -delete`…"""
        home = Path.home()
        for seg in re.split(r"\|\||&&|[|;&]", cmd):
            try:
                words = shlex.split(seg)
            except ValueError:
                words = seg.split()
            if not words:
                continue
            prog = Path(words[0]).name
            if prog in ("sudo", "doas") and len(words) > 1:
                words, prog = words[1:], Path(words[1]).name
            if prog == "gio" and words[1:2] == ["trash"]:
                words, prog = words[1:], "trash"
            deleting = prog in ("rm", "rmdir", "trash", "trash-put", "shred", "unlink", "mv") or \
                (prog == "find" and "-delete" in words)
            if not deleting:
                continue
            args = [w for w in words[1:] if not w.startswith("-")]
            if prog == "mv":
                args = args[:-1]  # the last one is where things go; that may be a main folder
            for w in args:
                w = re.sub(r"^(\$HOME|\$\{HOME\})", "~", w)
                path = Path(os.path.expanduser(w))
                path = path if path.is_absolute() else home / path  # her shell runs in home
                if "*" in w or "?" in w:
                    # a wildcard right in home / outside home matches main folders; inside one it's just its files
                    if path.parent.resolve() == home.resolve() or home.resolve() not in path.parent.resolve().parents:
                        return True
                elif self.protected(path):
                    return True
                elif path.is_dir() and _count(path, self.MAX_VOICE_DELETE + 1) > self.MAX_VOICE_DELETE:
                    return True  # too big to delete on a spoken "yes"
        return False

    def _describe(self, name: str, args: dict) -> str:
        """What the confirmation says, in plain words."""
        home = str(Path.home())
        short = lambda x: str(Path(os.path.expanduser(str(x))).resolve()).replace(home, "~")
        try:
            if name == "trash":
                path = Path(os.path.expanduser(args["path"])).resolve()
                if args.get("inside"):
                    items = sorted(i.name for i in path.iterdir())
                    shown = ", ".join(items[:4]) + (f" and {len(items) - 4} more" if len(items) > 4 else "")
                    return f"Empty {short(path)}: move its {len(items)} items to the trash ({shown}). The folder stays"
                if path.is_dir():
                    n = sum(1 for _ in path.rglob("*"))
                    return f"Move the WHOLE folder {short(path)} ({n} files and folders inside) to the trash"
                return f"Move the file {short(path)} to the trash"
            if name == "move_file":
                return f"Move {short(args['src'])} to {short(args['dst'])}"
        except (OSError, KeyError):
            pass
        return args.get("command") or f"{name} {json.dumps(args, ensure_ascii=False)}"

    def _check_path(self, p: str) -> Path:
        path = Path(os.path.expanduser(p)).resolve()
        if not any(path == r or r in path.parents for r in self.roots):
            raise PermissionError(f"{path} is outside the allowed folders ({', '.join(map(str, self.roots))})")
        return path

    async def call(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool {name}"
        allowed = set(tool.params)
        args = {k: _coerce(v, tool.params[k]) for k, v in (args or {}).items() if k in allowed}
        missing = [k for k in tool.required if args.get(k) in (None, "")]
        bad = [k for k, v in args.items() if "enum" in tool.params[k] and v not in tool.params[k]["enum"]]
        if missing or bad:  # tell the model, so it can fix the call instead of us guessing
            k = (missing or bad)[0]
            return (f"error: {name} needs '{k}'" if missing else f"error: {k}={args[k]!r} is not allowed") + (
                f"; one of: {', '.join(map(str, tool.params[k]['enum']))}" if "enum" in tool.params[k] else "")
        decision = "allow"
        if tool.risk == "confirm":
            decision = "confirm"
        elif tool.risk == "shell":
            decision = self._shell_policy(args.get("command", ""))
        if decision == "deny":
            self._audit(name, args, "deny")
            return "refused: this command is blocked by the security policy"
        if name == "trash" and (refusal := self._trash_check(args.get("path", ""), bool(args.get("inside")))):
            self._audit(name, args, "deny")
            return refusal
        if name == "move_file" and self.protected(args.get("src", "")):
            self._audit(name, args, "deny")
            what = args.get("path") or args.get("src")
            return (f"refused: {what} is a protected folder and is never deleted or moved as a whole. To delete what's "
                    f"in it, use trash(path=\"{what}\", inside=true); or name the files.")
        if decision == "confirm":
            summary = self._describe(name, args)
            ok = await self.confirm(summary)
            if not ok:
                self._audit(name, args, "declined")
                return "the user declined this action"
            decision = "confirmed"
        if self.on_tool:
            try:
                await self.on_tool(name, args)
            except Exception:
                pass
        try:
            result = await tool.fn(**args)
        except Exception as e:
            result = f"error: {e}"
        self._audit(name, args, decision, result)
        if self.on_result:
            self.on_result(name, args, result)
        return result

    def specs(self) -> list[Tool]:
        return list(self.tools.values())

    def _add(self, name, description, params, required, risk="safe"):
        def deco(fn):
            self.tools[name] = Tool(name, description, params, required, fn, risk)
            return fn
        return deco

    # ── tools ───────────────────────────────────────────────────────────────
    def _register(self):
        async def open_terminal(title: str, command: str) -> str:
            """Runs an interactive command in a new kitty window so the user sees it and answers prompts."""
            script = f"{command}; echo; read -r -p 'Done. Press Enter to close this window.' _"
            code, out = await sh("niri", "msg", "action", "spawn", "--", "kitty", "--title", title,
                                 "bash", "-c", script)
            return "opened" if code == 0 else f"failed: {out}"

        S = {"type": "string"}
        I = {"type": "integer"}

        @self._add("launch_app", "Open an installed application by name (e.g. 'firefox', 'files', 'steam').",
                   {"name": S}, ["name"])
        async def launch_app(name: str) -> str:
            app = find_app(name)
            if not app:  # "open roblox" is a game
                from .games import find_game

                if find_game(name, self.launchers):
                    return await self.tools["launch_game"].fn(name=name)
                return f"no installed app matches '{name}'"
            # spawn through niri so the app is not a child of the assistant
            code, out = await sh("niri", "msg", "action", "spawn", "--", *_exec_argv(app["exec"]))
            return f"launched {app['name']}" if code == 0 else f"failed: {out}"

        @self._add("list_windows", "List open windows (id, app, title, workspace).", {}, [])
        async def list_windows() -> str:
            code, out = await sh("niri", "msg", "-j", "windows", limit=None)
            if code:
                return out
            wins = json.loads(out)
            listing = "\n".join(f"{w['id']}: {w.get('app_id')} — {(w.get('title') or '')[:60]} (ws {w.get('workspace_id')})"
                             + (" [focused]" if w.get("is_focused") else "") for w in wins)
            return listing[:MAX_OUT] or "no windows"

        @self._add("focus_window", "Focus a window whose title or app id contains the query.", {"query": S}, ["query"])
        async def focus_window(query: str) -> str:
            code, out = await sh("niri", "msg", "-j", "windows", limit=None)
            q = query.lower()
            for w in json.loads(out) if code == 0 else []:
                if q in (w.get("title") or "").lower() or q in (w.get("app_id") or "").lower():
                    await sh("niri", "msg", "action", "focus-window", "--id", str(w["id"]))
                    return f"focused {w.get('app_id')}"
            return f"no window matches '{query}'"

        niri_actions = ["focus-workspace", "move-window-to-workspace", "close-window", "toggle-overview",
                        "fullscreen-window", "maximize-column", "toggle-window-floating", "focus-monitor-left",
                        "focus-monitor-right", "move-window-to-monitor-left", "move-window-to-monitor-right",
                        "focus-column-left", "focus-column-right", "screenshot", "screenshot-screen",
                        "screenshot-window", "switch-preset-column-width", "center-column", "power-off-monitors"]

        @self._add("niri_action", "Run a niri window-manager action. 'arg' is e.g. the workspace number. "
                   f"Allowed actions: {', '.join(niri_actions)}",
                   {"action": {"type": "string", "enum": niri_actions}, "arg": S}, ["action"])
        async def niri_action(action: str, arg: str = "") -> str:
            if action not in niri_actions:
                return "action not allowed"
            argv = ["niri", "msg", "action", action] + ([arg] if arg else [])
            code, out = await sh(*argv)
            return "done" if code == 0 else out

        @self._add("volume", "Set or change speaker volume. mode: set|up|down|mute. value: percent.",
                   {"mode": {"type": "string", "enum": ["set", "up", "down", "mute"]}, "value": I}, ["mode"])
        async def volume(mode: str, value: int | None = None) -> str:
            argv = {"set": ["volume-set", str(value if value is not None else 50)],
                    "up": ["volume-up"] + ([str(value)] if value else []),
                    "down": ["volume-down"] + ([str(value)] if value else []),
                    "mute": ["volume-mute"]}[mode]
            code, out = await sh("noctalia", "msg", *argv)
            if code:  # Noctalia not running: talk to PipeWire directly
                wp = {"set": f"{(value or 50) / 100:.2f}", "up": f"{value or 5}%+", "down": f"{value or 5}%-"}
                code, out = await (sh("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle") if mode == "mute"
                                   else sh("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", wp[mode]))
            return "ok" if code == 0 else out

        @self._add("media", "Control music/video playback.",
                   {"action": {"type": "string", "enum": ["play-pause", "next", "previous", "stop", "status"]}},
                   ["action"])
        async def media(action: str) -> str:
            from . import riff

            # Riff is the music player: while it plays, "pause"/"next" mean Riff, not some paused video
            if riff.connected() and action != "stop" and (await riff.call("status")).get("result", {}).get("playing"):
                return await self.tools["riff"].fn(action={"play-pause": "pause", "previous": "previous"}.get(action, action))
            if action == "status":
                return (await sh("playerctl", "metadata", "--format", "{{status}}: {{artist}} - {{title}}"))[1]
            return (await sh("playerctl", action))[1] or "ok"

        riff_ops = ["play", "playlist", "open_playlist", "playlists", "liked", "wave", "search", "pause", "resume",
                    "next", "previous", "like", "unlike", "shuffle_on", "shuffle_off", "status"]

        @self._add("riff", "Riff, the user's music player app (use it for songs and music, YouTube only when they "
                   "say YouTube/video). action: play (query = song/artist words; searches and plays the best "
                   "match), playlist (query = playlist name, shuffle=true to shuffle it), open_playlist (show it "
                   "without playing), playlists (list them), liked (their liked songs), wave (My Wave radio), "
                   "search (show results only), pause, resume, next, previous, like/unlike (the current song), "
                   "shuffle_on, shuffle_off, status (what's playing).",
                   {"action": {"type": "string", "enum": riff_ops}, "query": S, "shuffle": {"type": "boolean"}},
                   ["action"])
        async def riff_(action: str, query: str = "", shuffle: bool = False) -> str:
            import difflib

            from . import riff

            if not riff.connected():
                if action not in ("play", "playlist", "open_playlist", "playlists", "liked", "wave", "search"):
                    return "Riff isn't open"
                if not find_app("riff"):
                    return "Riff isn't installed"
                await self.tools["launch_app"].fn(name="riff")
                for _ in range(60):  # it takes a few seconds until the plugin is up
                    await asyncio.sleep(0.5)
                    if riff.connected():
                        break
                else:
                    return riff.NOT_CONNECTED
                await asyncio.sleep(1)  # let riff finish building its views

            async def rcall(name: str, **args) -> dict:
                r = await riff.call(name, **args)
                if "error" in r:
                    raise RuntimeError(r["error"])
                return r.get("result") or {}

            def song(t: dict | None) -> str:
                return f"“{t['title']}” by {t['artist']}" if t and t.get("artist") else f"“{(t or {}).get('title', '?')}”"

            try:
                if action in ("play", "search"):
                    if not query.strip():
                        if action == "play":
                            return await riff_("resume")
                        return "what should I search for?"
                    r = await rcall(action, query=query)
                    if action == "search":
                        return "showing Riff results: " + "; ".join(r.get("results", []))
                    others = "; ".join(r.get("others", []))
                    return f"playing {song(r)} in Riff" + (f". Other results: {others}" if others else "")
                if action in ("playlist", "open_playlist", "playlists"):
                    lists = (await rcall("playlists")).get("playlists", [])
                    names = ", ".join(f"{p['name']} ({p['count']} songs)" for p in lists) or "none"
                    if action == "playlists":
                        return f"Riff playlists: {names}"
                    want = re.sub(r"\b(my|the|playlists?|in riff|on riff)\b", " ", query.lower()).strip()
                    key = lambda s: re.sub(r"\W+", "", s.lower())
                    scored = [(1.0 if key(want) and key(want) == key(p["name"]) else
                               0.9 if key(want) and (key(want) in key(p["name"]) or key(p["name"]) in key(want)) else
                               difflib.SequenceMatcher(None, key(want), key(p["name"])).ratio(), p) for p in lists]
                    score, hit = max(scored, key=lambda x: x[0], default=(0, None))
                    if not hit or score < 0.6:
                        return f"no Riff playlist called '{query}'. Playlists: {names}"
                    r = await rcall("playlist", id=hit["id"], play=action == "playlist", shuffle=shuffle)
                    if action == "open_playlist":
                        await self.tools["focus_window"].fn(query="riff")
                        return f"opened the playlist {r['name']} ({r['count']} songs) in Riff"
                    return (f"playing the playlist {r['name']}{' on shuffle' if shuffle else ''}, "
                            f"starting with {song(r.get('track'))}")
                if action == "liked":
                    r = await rcall("liked", shuffle=shuffle)
                    return f"playing your {r['count']} liked songs{' on shuffle' if shuffle else ''}, starting with {song(r.get('track'))}"
                if action == "wave":
                    return f"playing My Wave, starting with {song(await rcall('wave'))}"
                if action == "status":
                    s = await rcall("status")
                    if not s.get("track"):
                        return "Riff is open but nothing is playing"
                    return (f"Riff is {'playing' if s['playing'] else 'paused on'} {song(s['track'])}, "
                            f"{s['time'] // 60}:{s['time'] % 60:02d} of {s['duration'] // 60}:{s['duration'] % 60:02d}"
                            f"{', liked' if s.get('liked') else ''}{', shuffle on' if s.get('shuffle') else ''}")
                s = await rcall("control", op=action)
                if action in ("like", "unlike"):
                    return f"{'liked' if action == 'like' else 'unliked'} {song(s.get('track'))}"
                if action in ("shuffle_on", "shuffle_off"):
                    return f"shuffle {'on' if s.get('shuffle') else 'off'}"
                if action in ("next", "previous"):
                    return f"now playing {song(s.get('track'))}"
                return f"{'playing' if s.get('playing') else 'paused'} {song(s.get('track'))}"
            except RuntimeError as e:
                return f"Riff: {e}"

        @self._add("shell_panel", "Control the Noctalia desktop shell: open a panel, toggle night light, do-not-disturb, "
                   "wifi, bluetooth, change wallpaper, or theme. command is a `noctalia msg` subcommand like "
                   "'panel-toggle control-center', 'nightlight-toggle', 'notification-dnd-toggle', 'wallpaper-random', "
                   "'theme-mode-toggle', 'caffeine-toggle', 'bluetooth-toggle', 'wifi-toggle', "
                   "'notification-dnd-set on' (prefer -set over -toggle when the user says on/off), "
                   "'power-set performance'. To *check* a state: 'bluetooth-status', 'wifi-status', "
                   "'notification-dnd-status'.",
                   {"command": S}, ["command"])
        async def shell_panel(command: str) -> str:
            argv = shlex.split(command)
            if not argv or argv[0] in {"session", "plugins", "dpms-off", "network-toggle"}:
                return "that Noctalia command needs to be run through run_shell"
            code, out = await sh("noctalia", "msg", *argv)
            return out or ("ok" if code == 0 else "failed")

        @self._add("notify", "Show a desktop notification. Only when the user explicitly asks for a notification "
                   "right now; for later reminders use timer. Never to report results (you answer by voice).", {"title": S, "body": S}, ["title"])
        async def notify(title: str, body: str = "") -> str:
            await sh("notify-send", "-a", "Assistant", title, body)
            return "shown"

        @self._add("open", "Open a URL in the browser, or a file/folder in its default app. 'app' optionally "
                   "names the program to use, e.g. 'Brave Origin', 'Firefox'. Only open URLs that appeared in the "
                   "conversation or tool results; never guess one.", {"target": S, "app": S}, ["target"])
        async def open_(target: str, app: str | None = None) -> str:
            url = to_url(target)
            if url:
                target = url
            else:
                target = str(self._check_path(target))
            argv, used = ["xdg-open", target], "the default app"
            if app and not re.search(r"\b(default|my)\b", app, re.I):
                found = find_app(app)
                if found:
                    argv, used = _exec_argv(found["exec"]) + [target], found["name"]
                else:
                    used = f"the default browser ({app} is not installed)"
            code, out = await sh("niri", "msg", "action", "spawn", "--", *argv)
            return f"opened {target} in {used}" if code == 0 else f"failed: {out}"

        @self._add("type_text", "Type text into the focused window as if from the keyboard (in the browser: into the "
                   "page's search box/field). submit=true presses Enter afterwards ('…and press enter', 'search "
                   "for').", {"text": S, "submit": {"type": "boolean"}}, ["text"])
        async def type_text(text: str, submit: bool = False) -> str:
            from .screen import focused_window

            app = ((await focused_window()) or {}).get("app_id") or ""
            if re.search(r"brave|chrom|firefox", app, re.I) and "browser_type" in self.tools:
                try:
                    return await self.tools["browser_type"].fn(text=text, submit=submit)
                except RuntimeError as e:  # extension not connected, no field: type with the keyboard
                    log.info("browser typing failed (%s), using wtype", e)
            out = (await sh("wtype", "--", text))[1]
            if submit and not out:
                out = (await sh("wtype", "-k", "Return"))[1]
            return out or ("typed and pressed Enter" if submit else "typed")

        @self._add("clipboard", "Read or set the clipboard. Omit text to read.", {"text": S}, [])
        async def clipboard(text: str | None = None) -> str:
            if text is None:
                return (await sh("wl-paste", "--no-newline"))[1] or "(clipboard empty)"
            await sh("wl-copy", input_=text.encode())
            return "copied"

        @self._add("list_files", "What's in one folder the user named ('my clips folder' → ~/Videos/Clips). To look for "
                   "files by name, kind or date anywhere, use find_files.", {"path": S}, ["path"])
        async def list_files(path: str) -> str:
            p = self._check_path(path)
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:80]
            return "\n".join(f"{i.name}/" if i.is_dir() else i.name for i in items) or "(empty)"

        @self._add("read_file", "Read a text file (first 4000 chars).", {"path": S}, ["path"])
        async def read_file(path: str) -> str:
            return self._check_path(path).read_text(errors="replace")[:4000]

        @self._add("write_file", "Create or overwrite a text file.", {"path": S, "content": S},
                   ["path", "content"], risk="confirm")
        async def write_file(path: str, content: str) -> str:
            p = self._check_path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"wrote {len(content)} chars to {p}"

        @self._add("trash", "Move a file or folder to the trash (recoverable). inside=true empties a folder (its files "
                   "go to the trash, the folder itself stays): use that for 'delete everything in Downloads'. Main "
                   "folders (home, Downloads, Documents, …) can never be trashed themselves.",
                   {"path": S, "inside": {"type": "boolean"}}, ["path"], risk="confirm")
        async def trash(path: str, inside: bool = False) -> str:
            p = self._check_path(path)
            if inside:
                if not p.is_dir():
                    return f"error: {p} is not a folder"
                items = [str(i) for i in p.iterdir()]
                if not items:
                    return f"{p.name} is already empty"
                code, out = await sh("gio", "trash", "--", *items, timeout=60)
                return f"error: {out}" if code else f"moved {len(items)} items from {p.name} to the trash (the folder stays)"
            code, out = await sh("gio", "trash", "--", str(p))
            return f"error: {out}" if code else f"moved {p.name} to trash"

        @self._add("move_file", "Move or rename a file inside the allowed folders.", {"src": S, "dst": S},
                   ["src", "dst"], risk="confirm")
        async def move_file(src: str, dst: str) -> str:
            s, d = self._check_path(src), self._check_path(dst)
            shutil.move(s, d)
            return f"moved to {d}"

        @self._add("run_shell", "Run a bash command on the user's Arch Linux PC (home folder). Use it for anything "
                   "no other tool covers: counting things, processes, services, network, disks, killing an "
                   "app, git, settings via CLI tools. Harmless read-only commands run directly, anything else asks "
                   "the user first; sudo commands open in a terminal for the password. Output is truncated. Not for "
                   "finding the user's files (find_files) or what's in a folder (list_files).",
                   {"command": S}, ["command"], risk="shell")
        async def run_shell(command: str) -> str:
            # plain `rm files` (already confirmed by the gate) goes to the trash instead, so it can be undone
            if (m := re.fullmatch(r"\s*rm\s+((?:-[a-zA-Z]+\s+)*)(.+)", command)) and not re.search(r"[;&|<>`$]", m[2]):
                return (await sh("gio", "trash", "--", *shlex.split(m[2]), timeout=30))[1] or \
                    "moved to the trash (recoverable)"
            if re.search(r"\bsudo\b", command):  # needs a password prompt → the user's terminal
                r = await open_terminal("Mio: " + command[:40], command)
                return (f"Opened a terminal running '{command}'; the user types their password there. It has not "
                        "finished yet.") if r == "opened" else r
            code, out = await sh(command, shell=True, timeout=20)
            return f"exit {code}\n{out}" if code else (out or "done (no output)")

        @self._add("system_info", "Numbers right now: CPU load, RAM, GPU (usage, VRAM, temperature), disk, uptime, "
                   "and which apps use the most RAM. For 'is something wrong / broken' use system_health.", {}, [])
        async def system_info() -> str:
            mem = (await sh("free", "-h"))[1].splitlines()[1].split()  # Mem: total used free shared cache available
            ram_line = f"RAM: {mem[1]} total, {mem[2]} used, {mem[6]} available (free for apps)" if len(mem) > 6 else \
                "RAM: " + " ".join(mem)
            parts = [(await sh("uptime"))[1], ram_line,
                     "Disk (home): " + (await sh("df", "-h", "--output=pcent,avail", str(Path.home())))[1].splitlines()[-1],
                     "GPU (usage, VRAM used, °C): " + (await sh(
                         "nvidia-smi", "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
                         "--format=csv,noheader"))[1]]
            ram: dict[str, int] = {}  # per app, all its processes together (Brave has dozens)
            for line in (await sh("ps", "-eo", "rss=,comm=", limit=None))[1].splitlines():
                kb, _, name = line.strip().partition(" ")
                if kb.isdigit():
                    name = re.sub(r"^(brave|chrome|firefox|electron|steam|discord)\w*.*", r"\1", name.strip(), flags=re.I)
                    ram[name] = ram.get(name, 0) + int(kb)
            top = sorted(ram.items(), key=lambda kv: -kv[1])[:8]
            parts.append("RAM by app: " + ", ".join(f"{n} {kb / 1048576:.1f} GB" for n, kb in top))
            return "\n".join(parts)

        feeds = {"arch": "https://archlinux.org/feeds/news/",
                 "cachyos": "https://cachyos.org/rss.xml",
                 "aur_new": "https://aur.archlinux.org/rss/",
                 "aur_updated": "https://aur.archlinux.org/rss/modified"}

        @self._add("arch_news", "Latest items from official feeds. source: 'arch' = Arch news announcements "
                   "(manual interventions, important changes), 'cachyos' = CachyOS release blog, "
                   "'aur_new' = newly added AUR packages, 'aur_updated' = recently updated AUR packages.",
                   {"source": {"type": "string", "enum": list(feeds)}, "count": I}, ["source"])
        async def arch_news(source: str = "arch", count: int = 5) -> str:
            import html
            import xml.etree.ElementTree as ET

            import httpx

            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(feeds.get(source, feeds["arch"]))
                r.raise_for_status()
            items = ET.fromstring(r.content).iter("item")
            out = []
            for item in list(items)[: max(1, min(count or 5, 10))]:
                title = item.findtext("title", "").strip()
                date = item.findtext("pubDate", "")[:16]  # "Tue, 14 Oct 2026"
                desc = re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description", "")))
                desc = re.sub(r"\s+", " ", desc).strip()
                long = source in ("arch", "cachyos")
                out.append(f"- {date}: {title}" + (f" — {desc[:280]}" if desc and long else
                                                     f" — {desc[:100]}" if desc else ""))
            return "\n".join(out) or "no items"

        @self._add("install_updates", "Install all pending system updates (repos + AUR). Opens a new terminal "
                   "window running paru -Syu where the user reviews the list and types their password.", {}, [])
        async def install_updates() -> str:
            cmd = "paru -Syu" if shutil.which("paru") else ("yay -Syu" if shutil.which("yay") else "sudo pacman -Syu")
            r = await open_terminal("System update", cmd)
            return (f"Opened a terminal running '{cmd}'. The user still has to confirm and enter their password "
                    "there; nothing is installed until they do.") if r == "opened" else r

        @self._add("install_packages", "Install packages by name. Opens a new terminal running paru -S (repos and "
                   "AUR) where the user confirms. Run check_install first if any of them may be from the AUR.",
                   {"packages": {"type": "array", "items": S}}, ["packages"])
        async def install_packages(packages: list[str]) -> str:
            names = [p for p in packages if re.fullmatch(r"[a-z0-9@._+-]+", p.lower())]
            if not names:
                return "no valid package names"
            tool = "paru" if shutil.which("paru") else "sudo pacman"
            cmd = f"{tool} -S {' '.join(shlex.quote(n) for n in names)}"
            r = await open_terminal("Install packages", cmd)
            return (f"Opened a terminal running '{cmd}'. The user confirms there."
                    if r == "opened" else r)

        @self._add("system_updates", "Check for pending system/package updates: official repos (checkupdates) and "
                   "AUR (paru). Highlights kernel, NVIDIA, Mesa and systemd updates. Only checks, never installs "
                   "(that's install_updates).", {}, [])
        async def system_updates() -> str:
            (_, repo), (_, aur) = await asyncio.gather(sh("checkupdates", timeout=60, limit=None),
                                                       sh("paru", "-Qua", timeout=60, limit=None))
            repo_l = [l for l in repo.splitlines() if "->" in l]
            aur_l = [l for l in aur.splitlines() if "->" in l]
            key = re.compile(r"^(linux\S*|nvidia\S*|mesa|systemd|glibc|niri|noctalia\S*|pipewire|wireplumber)\s")
            notable = [l for l in repo_l + aur_l if key.match(l)]
            parts = [f"{len(repo_l)} repo updates, {len(aur_l)} AUR updates."]
            if notable:
                parts.append("Notable: " + "; ".join(notable[:8]))
            if repo_l and len(repo_l) <= 10:  # a few: name them all ("what update is waiting?")
                parts.append("Repo: " + "; ".join(repo_l))
            if aur_l:
                parts.append("AUR: " + "; ".join(aur_l[:10]))
            return "\n".join(parts)

        @self._add("package_info", "Info about a package: installed version, repo or AUR, description, and for "
                   "AUR packages votes, maintainer, out-of-date flag and last update (useful to judge trust).",
                   {"name": S}, ["name"])
        async def package_info(name: str) -> str:
            import httpx

            name = name.strip().lower()
            if not re.fullmatch(r"[a-z0-9@._+-]+", name):
                return "invalid package name"
            code, local = await sh("pacman", "-Qi", name)
            installed = re.search(r"^Version\s*:\s*(\S+)", local, re.M).group(1) if code == 0 else "not installed"
            code, repo = await sh("pacman", "-Si", name)
            if code == 0:
                f = dict(re.findall(r"^(Repository|Version|Description|URL)\s*:\s*(.+)$", repo, re.M))
                return (f"{name}: in repo {f.get('Repository')}, version {f.get('Version')}, installed {installed}. "
                        f"{f.get('Description', '')}")
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get("https://aur.archlinux.org/rpc/v5/info", params={"arg[]": name})
            res = r.json().get("results") or []
            if not res:
                return f"{name}: not found in the repos or the AUR"
            p = res[0]
            day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%d %b %Y") if ts else "never"
            return (f"{name} (AUR): upstream {p.get('URL') or 'n/a'}, AUR page "
                    f"https://aur.archlinux.org/packages/{name}, version {p['Version']}, installed {installed}, maintainer "
                    f"{p.get('Maintainer') or 'none (orphaned)'}, {p['NumVotes']} votes, popularity "
                    f"{p['Popularity']:.2f}, last updated {day(p['LastModified'])}, first submitted "
                    f"{day(p['FirstSubmitted'])}, out of date: {day(p['OutOfDate']) if p['OutOfDate'] else 'no'}. "
                    f"{p.get('Description', '')}")

        async def _wiki(base: str, query: str, section: str | None) -> str:
            import html

            import httpx

            async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                         headers={"User-Agent": "desktop-assistant/0.1 (personal use)"}) as client:
                r = await client.get(f"{base}/api.php", params={"action": "opensearch", "search": query,
                                                                  "limit": 1, "format": "json"})
                titles = r.json()[1]
                if not titles:  # misheard/misspelled ("Patsune Miku"): fuzzy title match, then full-text search
                    r = await client.get(f"{base}/api.php", params={"action": "opensearch", "search": query,
                                                                      "limit": 1, "format": "json", "profile": "fuzzy"})
                    titles = r.json()[1]
                if not titles:
                    r = await client.get(f"{base}/api.php", params={"action": "query", "list": "search",
                                                                      "srsearch": query, "srlimit": 1, "format": "json"})
                    titles = [h["title"] for h in r.json().get("query", {}).get("search", [])]
                if not titles:  # last try: a web search knows what was meant
                    from urllib.parse import unquote, urlparse

                    from . import web

                    host = urlparse(base).netloc
                    hits = await asyncio.to_thread(web.search, f"{query} site:{host}")
                    titles = [unquote(m[1]).replace("_", " ") for h in hits
                              if (m := re.search(rf"{re.escape(host)}/(?:wiki|title)/([^?#]+)", h.get("href", "")))]
                if not titles:
                    return f"no article found for '{query}'"
                title = titles[0]
                params = {"action": "parse", "page": title, "prop": "text|sections", "format": "json",
                          "formatversion": 2, "redirects": 1, "disabletoc": 1}
                sections = (await client.get(f"{base}/api.php", params=params | {"prop": "sections"})).json()
                heads = [s["line"] for s in sections.get("parse", {}).get("sections", [])]
                idx = "0"
                if section:
                    for s in sections.get("parse", {}).get("sections", []):
                        if section.lower() in s["line"].lower():
                            idx = s["index"]
                            break
                page = (await client.get(f"{base}/api.php", params=params | {"prop": "text", "section": idx})).json()
            body = page.get("parse", {}).get("text", "")
            body = re.sub(r"<(style|table|sup)[^>]*>.*?</\1>", "", body, flags=re.S)
            paras = re.findall(r"<(?:p|li|pre)[^>]*>(.*?)</(?:p|li|pre)>", body, re.S)
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", " ".join(paras)))).strip()
            return (f"{title}{' — ' + section if section else ''}: {text[:2500]}\n"
                    f"Sections: {', '.join(heads[:25])}")

        @self._add("arch_wiki", "Look up an ArchWiki article (the best source for Arch/CachyOS how-tos and fixes). "
                   "Returns the intro and the section list; pass 'section' to read a specific section.",
                   {"query": S, "section": S}, ["query"])
        async def arch_wiki(query: str, section: str | None = None) -> str:
            return await _wiki("https://wiki.archlinux.org", query, section)

        @self._add("wikipedia", "Look up a topic on English Wikipedia (people, places, history, science...).",
                   {"query": S, "section": S}, ["query"])
        async def wikipedia(query: str, section: str | None = None) -> str:
            if not section:  # the REST summary is the cleanest intro
                import httpx
                from urllib.parse import quote

                async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                             headers={"User-Agent": "desktop-assistant/0.1 (personal use)"}) as c:
                    titles = (await c.get("https://en.wikipedia.org/w/api.php", params={
                        "action": "opensearch", "search": query, "limit": 1, "format": "json"})).json()[1]
                    if titles:
                        d = (await c.get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                                         + quote(titles[0].replace(" ", "_")))).json()
                        if d.get("extract"):
                            return f"{d['title']}: {d['extract'][:2500]}"
            return await _wiki("https://en.wikipedia.org/w", query, section)

        @self._add("weather", "Current weather and 3-day forecast (today, tomorrow, day after). Only pass "
                   "'location' if the user named a city; otherwise leave it out.", {"location": S}, [])
        async def weather(location: str | None = None) -> str:
            import httpx

            from urllib.parse import quote

            if location and re.search(r"\b(your|my|home|here|current|configured|local|user)\b", location, re.I):
                location = None  # small models like to invent placeholders
            if location and location.lower() == getattr(self, "_auto_area", "").lower():
                location = None  # the model echoed the IP-guessed town; a bare name can resolve elsewhere
            loc = (location or self.lookup.get("weather_location", "")).strip()
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"https://wttr.in/{quote(loc)}",
                                     params={"format": "j1"}, headers={"Accept-Language": "en"})
                r.raise_for_status()
                d = r.json()
            c, area = d["current_condition"][0], d["nearest_area"][0]["areaName"][0]["value"]
            if not loc:
                self._auto_area = area
            out = [f"{area} now: {c['weatherDesc'][0]['value']}, {c['temp_C']}°C (feels {c['FeelsLikeC']}°C), "
                   f"wind {c['windspeedKmph']} km/h, humidity {c['humidity']}%."]
            for i, day in enumerate(d["weather"][:3]):
                label = ["Today", "Tomorrow", dt.date.fromisoformat(day["date"]).strftime("%A")][i]
                rain = max(int(h.get("chanceofrain", 0)) for h in day["hourly"])
                desc = day["hourly"][4]["weatherDesc"][0]["value"]
                out.append(f"{label}: {desc}, {day['mintempC']}–{day['maxtempC']}°C, rain chance {rain}%")
            return "\n".join(out)

        @self._add("launch_game", "Start a game: Roblox (via Froststrap/Sober) or any installed Steam game by name.",
                   {"name": S}, ["name"])
        async def launch_game(name: str) -> str:
            from .games import find_game

            hit = find_game(name, self.launchers)
            if not hit:
                if find_app(name):  # "start discord" asked as a game
                    return await self.tools["launch_app"].fn(name=name)
                return f"no installed game matches '{name}'"
            title, argv = hit
            code, out = await sh("niri", "msg", "action", "spawn", "--", *argv)
            return f"starting {title}" if code == 0 else f"failed: {out}"

        @self._add("steam_library", "Open the Steam library, or list installed Steam games when list=true.",
                   {"list": {"type": "boolean"}}, [])
        async def steam_library(list: bool = False) -> str:
            from .games import steam_games

            if list:
                return "Installed Steam games: " + (", ".join(sorted(steam_games())) or "none")
            code, out = await sh("niri", "msg", "action", "spawn", "--", "steam", "steam://open/games")
            return "opened the Steam library" if code == 0 else f"failed: {out}"

        @self._add("game_news", "Latest patch notes / official update announcements for a game (any Steam game by "
                   "name; for Roblox: when it last updated and whether Sober has an update). Summarize the notes "
                   "briefly.", {"game": S, "count": I}, ["game"])
        async def game_news(game: str, count: int = 2) -> str:
            from . import gamenews

            return await gamenews.patch_notes(game, count or 2)

        @self._add("game_price", "Steam prices and sales. game = a game's name to get its price/discount; "
                   "'wishlist' = which wishlist games are on sale; 'deals' = the current top Steam deals.",
                   {"game": S}, ["game"])
        async def game_price(game: str) -> str:
            from . import gamenews

            key = game.lower().strip()
            if "wishlist" in key:
                return await gamenews.wishlist_sales()
            if key in ("deals", "sales", "specials", "sale", "steam sale", "top deals"):
                return await gamenews.top_deals()
            return await gamenews.price(game)

        @self._add("clip", "Save a clip of the last moments of gameplay (instant replay, like 'clip that'). "
                   "seconds: how far back, default 30.", {"seconds": I}, [])
        async def clip(seconds: int | None = None) -> str:
            if self.clipper is None or not self.clipper.available():
                return "clipping isn't set up (gpu-screen-recorder is not installed)"
            if not self.clipper.running():
                return "I only keep a replay while a game is running, so there's nothing to clip right now"
            path, info = await self.clipper.save(seconds)
            return f"saved the last {info} to {path}" if path else f"couldn't save the clip: {info}"

        async def phone_id() -> tuple[str | None, str]:
            code, out = await sh("kdeconnect-cli", "--list-available", "--id-name-only", limit=None)
            dev_re = re.compile(r"^([0-9A-Za-z_-]{16,})\s+(.+)$", re.M)  # "<id> <name>"; skips "0 devices found"
            if m := dev_re.search(out):
                return m.group(1), m.group(2).strip()
            code, out = await sh("kdeconnect-cli", "--list-devices", "--id-name-only", limit=None)
            return None, ("your phone is paired but not reachable right now. Open the KDE Connect app on it and "
                          "check it's on the same Wi-Fi" if dev_re.search(out) else
                          "no phone is paired with KDE Connect yet")

        @self._add("phone", "Control the user's phone via KDE Connect. action: ring (find my phone), ping (send a "
                   "message), battery, send_link (open a URL on the phone), send_text (to the phone's clipboard), "
                   "send_file (a file path).",
                   {"action": {"type": "string", "enum": ["ring", "ping", "battery", "send_link", "send_text",
                                                          "send_file"]}, "text": S}, ["action"])
        async def phone(action: str, text: str = "") -> str:
            dev, name = await phone_id()
            if not dev:
                return name
            if action == "ring":
                code, out = await sh("kdeconnect-cli", "-d", dev, "--ring")
                return f"ringing {name}" if code == 0 else out
            if action == "ping":
                code, out = await sh("kdeconnect-cli", "-d", dev, "--ping-msg", text or "Hi from Mio!")
                return f"sent to {name}" if code == 0 else out
            if action == "battery":
                path = f"/modules/kdeconnect/devices/{dev}/battery"
                _, charge = await sh("busctl", "--user", "get-property", "org.kde.kdeconnect", path,
                                     "org.kde.kdeconnect.device.battery", "charge")
                _, charging = await sh("busctl", "--user", "get-property", "org.kde.kdeconnect", path,
                                       "org.kde.kdeconnect.device.battery", "isCharging")
                pct = charge.split()[-1] if charge.strip() else "-1"
                if not pct.lstrip("-").isdigit() or int(pct) < 0:  # the phone hasn't reported its battery
                    return (f"{name} isn't sharing its battery level. In the KDE Connect app on the phone, open this "
                            "PC and turn on the Battery plugin, then keep the app open for a moment")
                return f"{name} battery: {pct}%" + (", charging" if charging.strip().endswith("true") else "")
            if action == "send_link":
                url = to_url(text) or text
                code, out = await sh("kdeconnect-cli", "-d", dev, "--share", url)
                return f"sent {url} to {name}" if code == 0 else out
            if action == "send_text":
                code, out = await sh("kdeconnect-cli", "-d", dev, "--share-text", text)
                return f"sent the text to {name}" if code == 0 else out
            if action == "send_file":
                path = self._check_path(text)
                code, out = await sh("kdeconnect-cli", "-d", dev, "--share", str(path))
                return f"sent {path.name} to {name}" if code == 0 else out
            return "unknown phone action"

        @self._add("search_my_setup", "Search the user's own configs and notes (niri keybinds, kitty, fish, Mio's "
                   "settings and manual). Use for 'my shortcut/keybind/config', 'how do I change your voice'.",
                   {"query": S}, ["query"])
        async def search_my_setup(query: str) -> str:
            if self.knowledge is None:
                return "no knowledge index configured"
            return self.knowledge.search(query)

        @self._add("world_clock", "Current time and date at a place (country or city), or here if omitted.",
                   {"place": S}, [])
        async def world_clock(place: str | None = None) -> str:
            from . import clock

            return clock.describe(place or None, want_time=True, want_date=True) or f"unknown place '{place}'"

        @self._add("web_research", "Search the web and read the top pages (free, local). Returns condensed "
                   "extracts from several sites; answer from them and mention which sites. Use for anything "
                   "current or factual you are not sure about.", {"query": S}, ["query"])
        async def web_research(query: str) -> str:
            from . import web

            return await web.research(query)

        @self._add("read_link", "Read the main text of a web page by URL.", {"url": S}, ["url"])
        async def read_link(url: str) -> str:
            from . import web

            if not re.match(r"^https?://", url):
                return "not a web link"
            return await web.read_url(url)

        @self._add("read_clipboard_link", "Read the web page whose link is in the clipboard. Only use when the "
                   "user explicitly mentions the clipboard or 'the link I copied'.", {}, [])
        async def read_clipboard_link() -> str:
            from . import web

            url = web.first_url((await sh("wl-paste", "--no-newline", limit=None))[1])
            if not url:
                return "there is no link in the clipboard"
            return await web.read_url(url)

        # ── browser (via the Mio extension in Brave) ──────────────────────────
        async def bcall(action: str, **args):
            from . import browser

            r = await browser.call(action, **args)
            if "error" in r:
                raise RuntimeError(r["error"])
            return r.get("result") or {}

        async def gated(action: str, what: str, **args) -> dict:
            """Runs a browser action; if the page says it's risky ("Buy now", sending a form), asks first."""
            r = await bcall(action, **args)
            if isinstance(r, dict) and r.get("needs_confirm"):
                if not await self.confirm(f"{what} “{r['needs_confirm']}” in the browser"):
                    return {"declined": True}
                r = await bcall(action, **args, confirmed=True)
            return r

        @self._add("youtube_play", "Find a YouTube video by search words (title, channel, topic) and open the "
                   "top result in the browser so it plays. For 'play/open/put on X on YouTube', 'search X on YouTube "
                   "and open the first video'.", {"query": S}, ["query"])
        async def youtube_play(query: str) -> str:
            if self.said and not wants_video(self.said):  # "look up X (on youtube)" means results, not a video
                from urllib.parse import quote_plus

                r = await open_(SEARCH["youtube"] + quote_plus(query))
                return (f"showing YouTube search results for '{query}' (no video started: the user didn't ask to "
                        f"play one)" if r.startswith("opened") else r)
            code, out = await sh("yt-dlp", "--flat-playlist", "--print", "%(id)s|%(title)s|%(channel)s",
                                 f"ytsearch3:{query}", timeout=25)
            hits = [l.split("|", 2) for l in out.splitlines() if l.count("|") >= 2 and len(l.split("|")[0]) == 11]
            if not hits:
                return f"no YouTube results for '{query}'"
            vid, title, channel = hits[0]
            r = await open_(f"https://www.youtube.com/watch?v={vid}")
            if not r.startswith("opened"):
                return r
            others = "; ".join(f"{t} ({c})" for _, t, c in hits[1:])
            return f"playing “{title}” by {channel}" + (f". Other results: {others}" if others else "")

        @self._add("open_best_match", "Open the best web match for a description, e.g. 'niri github page', "
                   "'arch wiki bluetooth', 'roblox status page'. Use this instead of guessing a URL.", {"query": S},
                   ["query"])
        async def open_best_match(query: str) -> str:
            from . import web

            hits = await asyncio.to_thread(web.search, query)
            if not hits:
                return f"nothing found for '{query}'"
            r = await open_(hits[0]["href"])
            return f"{r} ({hits[0].get('title', '')})" if r.startswith("opened") else r

        @self._add("search_site", "Open a search results page in the browser. site: " + ", ".join(
                   k for k in SEARCH if k != "the aur") + ". Use when the user wants to *see* results on a site.",
                   {"site": {"type": "string", "enum": [k for k in SEARCH if k != "the aur"]}, "query": S},
                   ["site", "query"])
        async def search_site(site: str, query: str) -> str:
            from urllib.parse import quote_plus

            base = SEARCH.get(site.lower().strip())
            if not base:
                return f"unknown site {site}; known: {', '.join(SEARCH)}"
            return await open_(base + quote_plus(query))

        @self._add("browser_page", "Read the web page open in the user's browser (title, URL, selected text, main "
                   "text). Use for 'this page/article/site', 'summarize this', 'what does it say'.", {}, [])
        async def browser_page() -> str:
            p = await bcall("page")
            parts = [f"Title: {p.get('title')}", f"URL: {p.get('url')}"]
            if p.get("selection"):
                parts.append(f"Selected text: {p['selection']}")
            return "\n".join(parts) + "\n\n" + (p.get("text") or p.get("description") or "(no text)")[:5000]

        video_ops = ["info", "about", "play", "pause", "toggle", "skip_ad", "speed", "seek", "seek_to", "next",
                     "mute", "unmute"]

        @self._add("browser_video", "Control or describe the YouTube video in the browser. action: info (title, "
                   "time), about (its transcript, to say what it's about), play, pause, skip_ad, speed (value = "
                   "rate like 1.5), seek (value = seconds, negative = back), seek_to (value = seconds), next.",
                   {"action": {"type": "string", "enum": video_ops}, "value": {"type": "number"}}, ["action"])
        async def browser_video(action: str, value: float | None = None) -> str:
            if action == "about":
                from . import browser

                info = await bcall("video", op="info")
                text = await browser.transcript(info.get("url", ""))
                head = f"Video: {info.get('title')} by {info.get('channel')} ({info.get('duration')})"
                return head + ("\nTranscript: " + text if text else "\n(no subtitles available)")
            r = await bcall("video", op=action, value=value)
            if r.get("error"):
                return r["error"]
            return (f"{action} done. Now: {r.get('title')} at {r.get('time')}/{r.get('duration')}, "
                    f"{'paused' if r.get('paused') else 'playing'}, speed {r.get('speed')}x")

        @self._add("browser_tabs", "Browser tabs. action: list, switch (to the tab matching query), close (tabs "
                   "matching query; query 'this' = current tab), reopen (the last closed tab).",
                   {"action": {"type": "string", "enum": ["list", "switch", "close", "reopen"]}, "query": S},
                   ["action"])
        async def browser_tabs(action: str, query: str = "") -> str:
            op = {"switch": "focus"}.get(action, action)
            r = await gated("tabs", "close", op=op, query=query) if op == "close" else await bcall("tabs", op=op, query=query)
            if isinstance(r, list):
                return "\n".join(f"- {t['title']} ({t['host']})" + (" [current]" if t["active"] else "")
                                 + (" [playing]" if t["audible"] else "") for t in r) or "no tabs"
            if r.get("declined"):
                return "the user declined"
            if op == "focus":
                return f"switched to {r['focused']}" if r.get("focused") else f"no tab matches '{query}'"
            if op == "close":
                return f"closed {r.get('closed', 0)} tab(s)"
            return f"reopened {r.get('reopened')}"

        @self._add("browser_click", "Click something on the browser page: a link/button by its text, or 'first "
                   "result', 'second video', 'last link'. Buttons like buy/pay/delete/send ask the user first. "
                   "wait = seconds to keep looking while the page is still loading.",
                   {"target": S, "wait": {"type": "number"}}, ["target"])
        async def browser_click(target: str, wait: float = 0) -> str:
            r = await gated("click", "click", target=target, wait=min(max(wait or 0, 0), 15))
            if r.get("declined"):
                return "the user declined"
            return r.get("error") or f"clicked {r.get('clicked')}"

        @self._add("browser_type", "Type text into the browser page's focused field or search box; submit=true "
                   "presses enter (sending a non-search form asks the user first). Never for passwords.",
                   {"text": S, "submit": {"type": "boolean"}}, ["text"])
        async def browser_type(text: str, submit: bool = False) -> str:
            r = await gated("type", "send", text=text, submit=submit)
            if r.get("declined"):
                return "the user declined"
            return r.get("error") or f"typed into {r.get('field')}" + (" and submitted" if r.get("submitted") else "")

        @self._add("browser_scroll", "Scroll the browser page.",
                   {"direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]}}, ["direction"])
        async def browser_scroll(direction: str) -> str:
            await bcall("scroll", direction=direction)
            return f"scrolled {direction}"

        @self._add("browser_history", "Search the browser history (title/URL words), e.g. 'that recipe I looked "
                   "at yesterday' → query 'recipe', days 2.", {"query": S, "days": I}, ["query"])
        async def browser_history(query: str, days: int = 7) -> str:
            items = await bcall("history", query=query, days=days or 7)
            return "\n".join(f"- {i['last'].replace('T', ' ')}: {i['title']} — {i['url']}" for i in items) \
                or f"nothing matching '{query}' in the last {days} days"

        @self._add("read_screen", "Read the text in the user's focused window (screenshot + local OCR). Use when "
                   "the user says 'this', 'on my screen', 'this window/error'. For web pages prefer browser_page.", {}, [])
        async def read_screen() -> str:
            from . import screen

            path, win = await screen.capture()
            head = f"Focused window: {win.get('app_id')} — {win.get('title')}\n"
            if win.get("app_id") == "kitty":  # exact text beats OCR for terminals
                text, src = await read_terminal()
                if src == "kitty":
                    return head + text[-3500:]
            if not path:
                return head + "(screenshot failed)"
            text = await screen.ocr(path)
            return head + (text[:6000] or "(no readable text found)")

        @self._add("look_at_screen", "Get an image of the user's focused window to look at (layout, pictures, "
                   "charts, UI). Prefer read_screen when only the text matters.", {}, [])
        async def look_at_screen() -> str:
            from . import screen

            path, win = await screen.capture()
            if not path:
                return "screenshot failed"
            img = await screen.for_vision(path)
            return f"__image__:{img}\nFocused window: {win.get('app_id')} — {win.get('title')}"

        @self._add("terminal_text","Read the text currently visible in the focused terminal (kitty), e.g. to explain "
                   "an error or see what the user typed. Falls back to the clipboard.", {}, [])
        async def terminal_text() -> str:
            text, src = await read_terminal()
            return f"[{src}]\n{text[-3000:]}" if text else "could not read the terminal or clipboard"

        @self._add("check_install", "Safety check for the package install command the user typed or pasted in the "
                   "focused terminal (paru/yay/pacman/makepkg/curl|sh). Reads the terminal, identifies the packages, "
                   "checks AUR metadata and scans PKGBUILDs for red flags. Use whenever the user asks if an "
                   "install is safe.", {}, [])
        async def check_install() -> str:
            text, src = await read_terminal()
            if not text:
                return "I couldn't read the terminal or the clipboard."
            return await assess_install(text, src)

        @self._add("system_health","Is anything wrong with the PC: failed services, errors since boot, too hot, "
                   "disk almost full, low memory. For plain numbers use system_info.", {}, [])
        async def system_health() -> str:
            results = await asyncio.gather(
                sh("systemctl", "--failed", "--no-legend", "--plain"),
                sh("systemctl", "--user", "--failed", "--no-legend", "--plain"),
                sh("bash", "-c", "journalctl -b -p err --no-pager -o cat | sort | uniq -c | sort -rn | head -5"),
                sh("sensors", "-A"),
                sh("df", "-h", "--output=target,pcent,avail", "-x", "tmpfs", "-x", "devtmpfs", "-x", "efivarfs"),
                sh("free", "-h"),
            )
            failed, ufailed, errs, temps, disk, mem = (r[1] for r in results)
            temp_lines = [l.strip() for l in temps.splitlines() if re.search(r"(Tctl|Package|Composite|edge|temp1)", l)]
            return "\n".join([
                f"Failed system services: {failed or 'none'}",
                f"Failed user services: {ufailed or 'none'}",
                f"Most frequent errors this boot:\n{errs or 'none'}",
                "Temperatures: " + ("; ".join(temp_lines[:6]) or "n/a"),
                f"Disks:\n{disk}",
                f"Memory: {mem.splitlines()[1] if mem else 'n/a'}",
            ])

        @self._add("timer", "Timers and reminders. action set: give 'minutes' (from now) or 'at' (a clock time like "
                   "'18:30', '6 pm', 'tomorrow 9:00'), plus 'label' (what to remind about). action list: what's "
                   "running and how long is left. action cancel: 'label' to pick one, 'all', or empty for the next.",
                   {"action": {"type": "string", "enum": ["set", "list", "cancel"]}, "minutes": {"type": "number"},
                    "at": S, "label": S}, ["action"])
        async def timer(action: str, minutes: float | None = None, at: str = "", label: str = "") -> str:
            from . import timers as tm

            if self.timers is None:
                return "timers are not available"
            if action == "list":
                return self.timers.summary()
            if action == "cancel":
                gone = self.timers.cancel(label)
                return (f"cancelled {len(gone)}: " + "; ".join(self.timers.describe(i) for i in gone)) if gone \
                    else "nothing to cancel"
            now = dt.datetime.now()
            if at:
                when = tm.parse_clock(at if re.match(r"\s*(at|tomorrow|tonight)\b", at) else f"at {at}", now)
                if not when:
                    return f"couldn't understand the time '{at}'"
                item = self.timers.add(when, label, "reminder")
                return f"reminder set {tm.say_clock(when, now)}" + (f" to {label}" if label else "")
            if not minutes or minutes <= 0:
                return "need 'minutes' or 'at'"
            secs = round(minutes * 60)
            item = self.timers.add(now + dt.timedelta(seconds=secs), label, "reminder" if label else "timer", secs)
            return f"{item['kind']} set for {tm.say_duration(secs)}" + (f" to {label}" if label else "")

        from .actions import register
        register(self)

        @self._add("remember","Save a short long-term fact about the user (preferences, names, habits).",
                   {"fact": S}, ["fact"])
        async def remember(fact: str) -> str:
            if self.remember_fn:
                self.remember_fn(fact)
            return "remembered"
