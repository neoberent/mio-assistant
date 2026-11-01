"""Evidence collectors for the virus scan (assistant/virusscan.py): where a file comes from, what starts on its own,
what is running, and whether system programs still match their packages.

Every check here is structural (where/how something runs, does it match its package), never "the name looks
odd": unknown programs like Orbit are normal and only end up in the inventory. Content is judged by ClamAV and
the YARA rule set, not here.

`Ctx.root` lets tests point the system paths (/etc, /usr, /proc …) at a fake tree.
"""
from __future__ import annotations

import asyncio
import configparser
import hashlib
import os
import re
import shlex
import shutil
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import PROJECT_DIR, XDG_DATA

TEMP_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
LEVELS = {"note": 0, "suspicious": 1, "danger": 2}

# hidden folders right under home that apps normally use; a program hidden anywhere else is one hint (never alone)
KNOWN_DOT = {".local", ".config", ".cache", ".var", ".steam", ".mozilla", ".cargo", ".rustup", ".npm", ".nvm",
             ".bun", ".deno", ".pyenv", ".pixi", ".conda", ".ollama", ".ssh", ".gnupg", ".pki", ".java", ".gradle",
             ".m2", ".vscode", ".vscode-oss", ".wine", ".claude", ".codex", ".icons", ".themes", ".fonts", ".zsh",
             ".oh-my-zsh", ".tmux", ".vim", ".nv", ".dotnet", ".android", ".nuget", ".gem", ".yarn", ".pnpm-store",
             ".bundle", ".terminfo", ".mono", ".electron", ".node-gyp", ".texlive", ".julia", ".ghcup", ".opam",
             ".kube", ".docker", ".minikube", ".thunderbird", ".pulse", ".dbus", ".gradle", ".sdkman", ".lmstudio",
             ".rembg", ".cmake", ".triton", ".vscode-oss-shared", ".package-manager", ".tizen", ".sdb", ".swt",
             ".u2net", ".keras", ".nv", ".cupy", ".jupyter", ".ipython", ".matplotlib", ".streamlit", ".gemini"}

# folders full of programs nobody signs (games, Windows apps in Wine, language package caches): normal there
CONTAINERS = [re.compile(p) for p in (
    r"/\.local/share/Steam/", r"/\.steam/", r"/steamapps/", r"/compatdata/", r"/drive_c/", r"/\.wine/",
    r"/\.var/app/", r"/flatpak/", r"/node_modules/", r"/\.cargo/", r"/\.rustup/", r"/\.npm/", r"/\.nvm/",
    r"/\.bun/", r"/go/pkg/", r"/\.cache/uv/", r"/\.local/share/uv/", r"/\.cache/pip/", r"/\.pixi/",
    r"/\.ollama/", r"/\.claude/", r"/\.local/share/claude/", r"/\.cache/huggingface/", r"/site-packages/",
    r"/\.venv/", r"/venv/", r"/\.conda/", r"/miniconda3/", r"/anaconda3/", r"/\.local/share/lutris/",
    r"/Games/", r"/\.local/share/bottles/", r"/\.cache/yay/", r"/\.cache/paru/")]

# names of system daemons / kernel threads: a program NOT from a package that calls itself this is hiding
SYSTEM_NAMES = re.compile(r"^(?:kworker|kthreadd|ksoftirqd|kswapd|migration|rcu_|watchdogd|systemd|dbus-daemon|"
                          r"dbus-broker|sshd|crond?|atd|udevd|polkitd|NetworkManager|pipewire|wireplumber|Xwayland|"
                          r"gvfsd|at-spi|xdg-desktop-portal|irqbalance|kauditd)", re.I)
DOWNLOAD_RUN = re.compile(r"(curl|wget)\s[^|\n;]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")


@dataclass
class Finding:
    level: str            # note | suspicious | danger
    kind: str             # signature | yara | integrity | startup | process | system
    title: str            # plain words, spoken
    path: str = ""
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    fix: str = "none"     # quarantine | disable_unit | reinstall | none
    package: str = ""
    pid: int = 0
    unit: str = ""
    sha256: str = ""
    id: str = ""
    target: str = ""      # the program a startup entry runs: quarantined together with the entry

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ctx:
    home: Path = field(default_factory=Path.home)
    root: str = "/"                       # fake system tree for tests
    owned: dict[str, str] | None = None   # path → package (pacman -Ql), filled by load_owned()
    trust: list[dict] = field(default_factory=list)
    uid: int = field(default_factory=os.getuid)

    def sys(self, path: str) -> Path:
        """A system path (/etc/…) on disk; home paths stay as they are."""
        p = str(path)
        if self.root == "/" or p.startswith(str(self.home)):
            return Path(p)
        return Path(self.root) / p.lstrip("/")


async def run(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def load_owned(ctx: Ctx):
    if ctx.owned is not None:
        return
    code, out, _ = await run("pacman", "-Ql", timeout=120)
    owned = {}
    for line in out.splitlines():
        pkg, _, path = line.partition(" ")
        if path and not path.endswith("/"):
            owned[path] = pkg
    ctx.owned = owned


def sha256(path: str | Path, limit: int = 512 << 20) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            read = 0
            while chunk := f.read(1 << 20):
                h.update(chunk)
                read += len(chunk)
                if read > limit:
                    break
        return h.hexdigest()
    except OSError:
        return ""


def trusted(ctx: Ctx, path: str, digest: str = "") -> bool:
    for t in ctx.trust:
        if t.get("path") == path and (not t.get("sha256") or not digest or t["sha256"] == digest):
            return True
    return False


def provenance(ctx: Ctx, path: str) -> str:
    """package | mio | flatpak | container | temp | missing | unknown"""
    if not path:
        return "missing"
    p = os.path.normpath(path)
    if owned_by(ctx, p):
        return "package"
    if p.startswith(TEMP_DIRS):
        return "temp"
    if p.startswith(str(PROJECT_DIR)) or p.startswith(str(XDG_DATA / "assistant")):
        return "mio"
    if "/flatpak/" in p or p.startswith("/app/"):
        return "flatpak"
    if any(c.search(p) for c in CONTAINERS) or _in_venv(p):
        return "container"
    if not ctx.sys(p).exists():
        return "missing"
    return "unknown"


def owned_by(ctx: Ctx, path: str) -> str:
    """The package that installed this file. /usr/sbin, /bin, /lib are links to /usr/bin and /usr/lib on Arch, so
    pacman lists /usr/bin/ldconfig, not /usr/sbin/ldconfig: compare the real path too."""
    owned = ctx.owned or {}
    if path in owned:
        return owned[path]
    if ctx.root == "/":
        try:
            real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        except OSError:
            return ""
        return owned.get(real, "")
    return ""


def _in_venv(p: str) -> bool:
    d = Path(p).parent
    for _ in range(6):
        if (d / "pyvenv.cfg").exists():
            return True
        if d.parent == d:
            break
        d = d.parent
    return False


def hidden_odd(ctx: Ctx, path: str) -> bool:
    """Lives in a hidden folder that isn't one apps normally use (~/.x/…, ~/.config/.y/…)."""
    try:
        rel = Path(path).relative_to(ctx.home)
    except ValueError:
        return False
    parts = rel.parts[:-1]
    if not parts:
        return False
    if parts[0].startswith(".") and parts[0] not in KNOWN_DOT:
        return True
    return any(x.startswith(".") and x not in (".git",) for x in parts[1:])


def is_program(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head in (b"\x7fELF",) or head[:2] in (b"MZ", b"#!")


# ── commands that start on their own ────────────────────────────────────────
def command_target(ctx: Ctx, command: str) -> str:
    """The program a startup command runs: skips env/nice/sh -c wrappers."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    while argv:
        a = argv[0]
        if re.fullmatch(r"\w+=.*", a) or Path(a).name in ("env", "nice", "ionice", "nohup", "setsid", "exec",
                                                         "systemd-cat", "flatpak-spawn"):
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[1:]
            continue
        if Path(a).name in ("sh", "bash", "zsh", "dash", "fish") and len(argv) > 2 and argv[1] == "-c":
            return command_target(ctx, argv[2])
        break
    if not argv:
        return ""
    prog = os.path.expanduser(argv[0].replace("$HOME", str(ctx.home)).replace("%h", str(ctx.home)))
    if not prog.startswith("/"):
        for d in ("/usr/bin", "/usr/local/bin", str(ctx.home / ".local/bin"), "/bin", "/usr/sbin"):
            if ctx.sys(f"{d}/{prog}").exists():
                return f"{d}/{prog}"
        return shutil.which(prog) or ""
    return prog


def judge_command(ctx: Ctx, command: str, where: str) -> tuple[str, list[str], str]:
    """(level, evidence, target) for something that starts on its own. Only evidence counts:
    runs from a temp folder, downloads-and-runs, a non-package program hiding under a system name in a hidden
    folder. An unknown program alone is fine (it goes in the inventory)."""
    target = command_target(ctx, command)
    prov = provenance(ctx, target)
    ev: list[str] = []
    level = "note"
    if prov == "temp":
        ev.append(f"starts a program from the temp folder {target}")
        level = "danger"
    if DOWNLOAD_RUN.search(command):
        ev.append("downloads something from the internet and runs it, every time it starts")
        level = "danger"
    if prov == "unknown":
        name = Path(target).name
        hints = []
        if hidden_odd(ctx, target):
            hints.append("lives in a hidden folder")
        if SYSTEM_NAMES.match(name):
            hints.append(f"calls itself '{name}' like a system program, but isn't one")
        if len(hints) == 2:
            level = max(level, "danger", key=LEVELS.get)
        elif hints:
            level = max(level, "suspicious", key=LEVELS.get) if "system program" in hints[0] else level
        ev += hints
    if prov == "missing" and target:
        ev.append(f"points to {target}, which doesn't exist")
    return level, ev, target


def _unit_execs(text: str) -> list[str]:
    return [re.sub(r"^[-@:+!]+", "", m.strip()) for m in
            re.findall(r"^[ \t]*Exec(?:Start|StartPre|StartPost|Stop|Reload)[ \t]*=[ \t]*(.*)$", text, re.M) if m.strip()]
    # [ \t], not \s: an empty "ExecStart=" (the reset line every override starts with) must not swallow the next line


def startup_items(ctx: Ctx) -> list[dict]:
    """Everything that starts by itself: {source, command, kind, unit?}. Package-owned files are left to the
    integrity check."""
    items: list[dict] = []
    h = ctx.home
    owned = ctx.owned or {}

    def mine(p: Path, sys_path: str) -> bool:
        return sys_path not in owned

    unit_dirs = [(h / ".config/systemd/user", "user"), (h / ".local/share/systemd/user", "user"),
                 ("/etc/systemd/user", "user"), ("/etc/systemd/system", "system"), ("/usr/local/lib/systemd/system", "system")]
    for d, scope in unit_dirs:
        disk = ctx.sys(str(d))
        if not disk.is_dir():
            continue
        for f in disk.rglob("*"):
            dropin = f.suffix == ".conf" and f.parent.name.endswith((".service.d", ".socket.d", ".path.d"))
            if not f.is_file() or (f.suffix not in (".service", ".timer", ".path", ".socket") and not dropin):
                continue
            sys_path = str(d) + "/" + str(f.relative_to(disk))
            if not mine(f, sys_path):
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for cmd in _unit_execs(text):
                unit = f.parent.name[:-2] if dropin else f.name
                items.append({"source": sys_path, "command": cmd, "kind": f"{scope} service" + (" override" if dropin else ""),
                              "unit": unit, "scope": scope, "dropin": dropin})
    for d in (h / ".config/autostart", "/etc/xdg/autostart"):
        disk = ctx.sys(str(d))
        if not disk.is_dir():
            continue
        for f in disk.glob("*.desktop"):
            sys_path = f"{d}/{f.name}"
            if not mine(f, sys_path):
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except (KeyError, configparser.Error, OSError, UnicodeDecodeError):
                continue
            if e.get("Hidden", "").lower() == "true" or not e.get("Exec"):
                continue
            items.append({"source": sys_path, "command": e["Exec"], "kind": "autostart entry",
                          "name": e.get("Name", f.stem)})
    cron = [ctx.sys("/etc/crontab")] + [p for d in ("/etc/cron.d", "/var/spool/cron") if ctx.sys(d).is_dir()
                                        for p in ctx.sys(d).iterdir()]
    for f in cron:
        sys_path = "/" + str(f.relative_to(ctx.root)) if ctx.root != "/" else str(f)
        if not f.is_file() or not mine(f, sys_path):
            continue
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or re.match(r"\w+=", line):
                continue
            has_user = "/etc/" in sys_path and "spool" not in sys_path
            if line.startswith("@"):  # @reboot [user] command
                parts = line.split(None, 2 if has_user else 1)
                if len(parts) == (3 if has_user else 2):
                    items.append({"source": sys_path, "command": parts[-1], "kind": "scheduled task"})
                continue
            parts = line.split(None, 6 if has_user else 5)
            if len(parts) >= 6:
                items.append({"source": sys_path, "command": parts[-1], "kind": "scheduled task"})
    niri = h / ".config/niri"
    if niri.is_dir():
        for f in niri.rglob("*.kdl"):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"^\s*spawn-(?:sh-)?at-startup\s+(.+)$", text, re.M):
                args = re.findall(r'"((?:[^"\\]|\\.)*)"', m[1])
                cmd = args[0] if "spawn-sh" in m[0] and args else " ".join(shlex.quote(a) for a in args)
                if cmd:
                    items.append({"source": str(f), "command": cmd, "kind": "niri startup"})
    return items


def shell_startup_files(ctx: Ctx) -> list[Path]:
    h = ctx.home
    files = [h / n for n in (".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile",
                             ".zshenv", ".zlogin", ".xprofile", ".xinitrc")]
    files += sorted((h / ".config/fish").glob("**/*.fish")) if (h / ".config/fish").is_dir() else []
    pd = ctx.sys("/etc/profile.d")
    files += [p for p in pd.glob("*") if p.is_file() and f"/etc/profile.d/{p.name}" not in (ctx.owned or {})] \
        if pd.is_dir() else []
    return [f for f in files if f.is_file()]


def startup_findings(ctx: Ctx) -> tuple[list[Finding], list[dict], list[Path]]:
    """(findings, inventory of unknown startup programs, program files to give the content scanners)"""
    findings, inventory, targets = [], [], []
    for it in startup_items(ctx):
        level, ev, target = judge_command(ctx, it["command"], it["source"])
        if target and provenance(ctx, target) in ("unknown", "temp") and ctx.sys(target).is_file():
            targets.append(ctx.sys(target))
            inventory.append({"path": target, "why": it["kind"], "name": it.get("name") or Path(target).name})
        if level == "note" and not ev:
            continue
        # a bad override on a real service (pipewire!): remove only the override, never disable the service itself
        unit_user = it.get("scope") == "user" and it["source"].startswith(str(ctx.home)) and not it.get("dropin")
        fix = "disable_unit" if unit_user else "quarantine" if it["source"].startswith(str(ctx.home)) else "none"
        name = it.get("name") or re.sub(r"\.(service|timer|path|socket|desktop)$", "", Path(it["source"]).name)
        own_target = target if target and provenance(ctx, target) in ("unknown", "temp") else ""
        findings.append(Finding(level, "startup", f"the {it['kind']} '{name}'", it["source"],
                                f"runs: {it['command'][:200]}", ev, fix,
                                unit="" if it.get("dropin") else it.get("unit", ""), target=own_target))
    for f in shell_startup_files(ctx):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            if DOWNLOAD_RUN.search(s) and not re.match(r"^(alias|function|\w+\s*\(\))", s):
                findings.append(Finding("suspicious", "startup", f"a line in your {f.name}", str(f),
                                        f"line: {s[:200]}", ["downloads something and runs it every time a "
                                                             "terminal opens"], "none"))
            if re.search(r"\bexport\s+LD_PRELOAD=", s):
                findings.append(Finding("suspicious", "startup", f"a line in your {f.name}", str(f), f"line: {s[:200]}",
                                        ["forces a library into every program you start (LD_PRELOAD)"], "none"))
    return findings, inventory, targets


def system_hooks(ctx: Ctx) -> list[Finding]:
    """ld.so.preload, SSH keys, kernel modules outside packages."""
    out: list[Finding] = []
    pre = ctx.sys("/etc/ld.so.preload")
    if pre.is_file():
        libs = [l.strip() for l in pre.read_text(errors="replace").splitlines() if l.strip() and not l.startswith("#")]
        if libs:
            foreign = [l for l in libs if provenance(ctx, l) != "package"]
            out.append(Finding("danger" if foreign else "suspicious", "system",
                               "a library forced into every program on the PC", "/etc/ld.so.preload",
                               "contains: " + ", ".join(libs),
                               ["is set up through /etc/ld.so.preload, a classic rootkit trick normal desktops don't use"] +
                               ([f"isn't from any package ({', '.join(foreign)})"] if foreign else []), "none"))
    keys = ctx.home / ".ssh/authorized_keys"
    if keys.is_file():
        n = [l for l in keys.read_text(errors="replace").splitlines() if l.strip() and not l.startswith("#")]
        if n:
            out.append(Finding("note", "system", f"{len(n)} SSH keys that may log in to this PC", str(keys),
                               "keys: " + "; ".join(l.split()[-1] if len(l.split()) > 2 else "(no name)" for l in n)[:300],
                               ["only matters if the SSH server runs; check you know every key"]))
    out += kernel_modules(ctx)
    return out


def kernel_modules(ctx: Ctx) -> list[Finding]:
    try:
        rel = os.uname().release
        loaded = [l.split()[0] for l in ctx.sys("/proc/modules").read_text().splitlines() if l.strip()]
        dep = ctx.sys(f"/usr/lib/modules/{rel}/modules.dep").read_text()
    except OSError:
        return []
    known: dict[str, str] = {}
    for line in dep.splitlines():
        path = line.split(":", 1)[0]
        name = re.sub(r"\.ko(\.\w+)?$", "", Path(path).name).replace("-", "_")
        known[name] = path if path.startswith("/") else f"/usr/lib/modules/{rel}/{path}"
    out = []
    for m in loaded:
        path = known.get(m.replace("-", "_"))
        if path is None:
            out.append(Finding("suspicious", "system", f"the kernel module {m}", m,
                               "loaded in the kernel but not installed in the normal module folder",
                               ["is loaded from outside the normal module folder, which rootkits do"]))
        elif "/updates/" not in path and "/extramodules" not in path and ctx.owned and path not in ctx.owned:
            out.append(Finding("suspicious", "system", f"the kernel module {m}", path,
                               "not from any package and not built by DKMS", ["isn't from any package or DKMS"]))
    return out


# ── more places that hijack things ──────────────────────────────────────────
SENSITIVE = {"sudo", "su", "doas", "pkexec", "passwd", "ssh", "scp", "sftp", "gpg", "login", "chsh", "run0",
             "systemctl", "pacman", "paru", "yay"}  # a fake one of these sees your password or runs as you everywhere


def env_findings(ctx: Ctx) -> list[Finding]:
    """LD_PRELOAD forced into the whole session (environment.d, /etc/environment)."""
    out = []
    files = sorted((ctx.home / ".config/environment.d").glob("*.conf")) if (ctx.home / ".config/environment.d").is_dir() else []
    files += [ctx.sys("/etc/environment")] + (sorted(ctx.sys("/etc/environment.d").glob("*.conf"))
                                               if ctx.sys("/etc/environment.d").is_dir() else [])
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            m = re.match(r"\s*(?:export\s+)?LD_PRELOAD\s*=\s*['\"]?([^'\"\n]*)", line)
            if not m or not m[1].strip():
                continue
            libs = [x for x in re.split(r"[:\s]+", m[1]) if x]
            bad = [x for x in libs if provenance(ctx, x) in ("unknown", "temp", "missing")]
            out.append(Finding("danger" if bad else "suspicious", "startup", f"a library forced into your whole session",
                               str(f), f"line: {line.strip()[:200]}",
                               ["forces a library into every program you start (LD_PRELOAD)"] +
                               ([f"isn't from any package ({', '.join(bad)})"] if bad else []),
                               "quarantine" if str(f).startswith(str(ctx.home)) else "none"))
    return out


def menu_findings(ctx: Ctx) -> list[Finding]:
    """App-menu entries that download-and-run or start something from a temp folder (a fake 'Brave' shortcut)."""
    out = []
    d = ctx.home / ".local/share/applications"
    if not d.is_dir():
        return out
    for f in d.rglob("*.desktop"):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
            e = cp["Desktop Entry"]
        except (KeyError, configparser.Error, OSError, UnicodeDecodeError):
            continue
        cmd = e.get("Exec", "")
        if not cmd:
            continue
        level, ev, target = judge_command(ctx, cmd, str(f))
        ev = [x for x in ev if "hidden folder" not in x and "system program" not in x and "doesn't exist" not in x]
        if ev and level != "note":
            out.append(Finding(level, "startup", f"the app menu entry '{e.get('Name', f.stem)}'", str(f),
                               f"runs: {cmd[:200]}", ev, "quarantine"))
    return out


def path_hijack(ctx: Ctx) -> list[Finding]:
    """A program named like sudo/ssh/passwd in a folder that comes before /usr/bin in PATH receives what you type
    into it (your password). Also aliases/functions that swap those commands for a program in home or /tmp."""
    out = []
    path = os.environ.get("PATH", "") if ctx.root == "/" else f"{ctx.home}/.local/bin:{ctx.home}/bin:/usr/bin"
    for d in path.split(":"):
        if not d or d.startswith(("/usr/bin", "/bin", "/usr/sbin", "/sbin")):
            break  # only folders searched BEFORE the system ones can shadow it
        dp = Path(d)
        for name in SENSITIVE:
            f = dp / name
            if f.is_file() and provenance(ctx, str(f)) in ("unknown", "temp"):
                out.append(Finding("danger", "startup", f"a fake '{name}' command", str(f),
                                   f"{f} is found before the real /usr/bin/{name}",
                                   [f"is used instead of the real {name}, so it sees what you type into it "
                                    "(like your password)"], "quarantine"))
    for f in shell_startup_files(ctx):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^\s*(?:alias\s+(\w+)=['\"]?([^'\"\n]*)|(?:function\s+)?(\w+)\s*\(\)\s*\{([^}]*))", text, re.M):
            name, body = (m[1], m[2]) if m[1] else (m[3], m[4])
            if name not in SENSITIVE:
                continue
            paths = re.findall(r"(?:~|\$HOME|/home/|/tmp/|/dev/shm/|/var/tmp/)[\w./-]*", body)
            if paths:
                out.append(Finding("suspicious", "startup", f"your '{name}' command is redirected", str(f),
                                   f"in {f.name}: {m[0].strip()[:200]}",
                                   [f"swaps the real {name} for {paths[0]}, which could see your password"], "none"))
    return out


DISCORD_CLEAN = "module.exports = require('./core.asar');"
DISCORD_MODS = re.compile(r"betterdiscord|vencord|equicord|replugged|powercord|openasar|shelter|moonlight|"
                          r"kernel|goosemod|bdcompat", re.I)


def discord_core(ctx: Ctx) -> list[Finding]:
    """Token stealers add their code to Discord's core module so it runs whenever Discord starts. The clean file is a
    single known line; client mods (BetterDiscord, Vencord …) change it on purpose and are only noted."""
    out = []
    for f in ctx.home.glob(".config/discord*/*/modules/discord_desktop_core*/discord_desktop_core/index.js"):
        try:
            text = f.read_text(errors="replace").strip()
        except OSError:
            continue
        if text == DISCORD_CLEAN:
            continue
        mod = DISCORD_MODS.search(text)
        if mod and len(text) < 600:
            out.append(Finding("note", "startup", f"Discord mod {mod[0]} is installed", str(f), text[:200],
                               ["a Discord client mod changed the core module (fine if you installed it)"]))
            continue
        out.append(Finding("danger", "startup", "Discord's core module was changed", str(f),
                           f"{len(text)} characters instead of one line",
                           ["has extra code added, which token stealers do to run every time Discord starts"],
                           "restore_discord"))
    return out


def extensions(ctx: Ctx) -> list[dict]:
    """Browser extensions (Chromium family + Firefox) for the report. From outside the store = noted."""
    out = []
    for man in ctx.home.glob(".config/*/*/*/Extensions/*/*/manifest.json"):
        out.append(_ext(man))
    for man in ctx.home.glob(".config/*/*/Extensions/*/*/manifest.json"):
        out.append(_ext(man))
    for xpi in ctx.home.glob(".mozilla/firefox/*/extensions/*.xpi"):
        out.append({"browser": "Firefox", "name": xpi.stem, "id": xpi.stem, "store": True, "path": str(xpi)})
    seen, uniq = set(), []
    for e in out:
        if e and (e["browser"], e["id"]) not in seen:
            seen.add((e["browser"], e["id"]))
            uniq.append(e)
    return uniq


def _ext(man: Path) -> dict:
    import json

    try:
        m = json.loads(man.read_text(errors="replace"))
    except (OSError, ValueError):
        return {}
    name = m.get("name", "?")
    if name.startswith("__MSG_"):
        key = name[6:-2]
        for loc in (m.get("default_locale", "en"), "en", "en_US"):
            try:
                msgs = json.loads((man.parent / "_locales" / loc / "messages.json").read_text(errors="replace"))
                hit = next((v for k, v in msgs.items() if k.lower() == key.lower()), None)
                if hit:
                    name = hit.get("message", name)
                    break
            except (OSError, ValueError):
                continue
    browser = man.parts[man.parts.index(".config") + 1]
    browser = {"BraveSoftware": "Brave", "chromium": "Chromium", "google-chrome": "Chrome", "vivaldi": "Vivaldi",
               "microsoft-edge": "Edge", "opera": "Opera"}.get(browser, browser)
    store = any(u in m.get("update_url", "") for u in ("clients2.google.com", "edge.microsoft.com"))
    return {"browser": browser, "name": name, "id": man.parent.parent.name, "store": store, "path": str(man.parent),
            "perms": sorted(set(map(str, m.get("permissions", []) + m.get("host_permissions", []))))[:8]}


def listening(ctx: Ctx) -> dict[str, list[str]]:
    """{program path: ['tcp 0.0.0.0:51413', …]} for programs listening on the network."""
    if ctx.root != "/":
        return {}
    import subprocess

    try:
        out = subprocess.run(["ss", "-tulpnH"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    ports: dict[str, list[str]] = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 6:
            continue
        local = cols[4]
        if re.match(r"^(127\.|\[::1\]|::1)", local):
            continue  # only reachable from this PC
        for pid in re.findall(r"pid=(\d+)", line):
            try:
                exe = os.readlink(f"/proc/{pid}/exe").removesuffix(" (deleted)")
            except OSError:
                continue
            ports.setdefault(exe, [])
            entry = f"{cols[0]} {local}"
            if entry not in ports[exe]:
                ports[exe].append(entry)
    return ports


def setuid_finding(ctx: Ctx, path: str, st: os.stat_result) -> Finding | None:
    """A setuid program no package installed: runs with its owner's rights (root = full control). Classic backdoor."""
    if not (st.st_mode & stat.S_ISUID) or owned_by(ctx, path):
        return None
    root = st.st_uid == 0
    return Finding("danger" if root else "suspicious", "system", f"the setuid program {Path(path).name}", path,
                   f"owner uid {st.st_uid}, mode {oct(stat.S_IMODE(st.st_mode))}",
                   ["runs with root rights for anyone and isn't from any package (a classic backdoor)" if root else
                    "has the setuid bit set, which normal files in home never need"], "quarantine")


# ── running programs ────────────────────────────────────────────────────────
def processes(ctx: Ctx) -> tuple[list[Finding], list[dict], list[Path]]:
    proc = ctx.sys("/proc")
    findings, inventory, targets, seen = [], [], [], set()
    if not proc.is_dir():
        return findings, inventory, targets
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        try:
            cmdline = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            comm = (d / "comm").read_text().strip()
        except OSError:
            continue
        try:
            exe = os.readlink(d / "exe")
        except OSError:
            exe = ""  # kernel thread, or another user's process
        if exe and cmdline.startswith("[") and cmdline.endswith("]"):
            findings.append(Finding("danger", "process", f"a program pretending to be the kernel thread {cmdline}",
                                    exe.removesuffix(" (deleted)"), f"pid {pid}, really runs {exe}",
                                    ["hides behind a kernel name, but real kernel threads have no program file"],
                                    "quarantine", pid=pid))
            continue
        if not exe:
            continue
        deleted = exe.endswith(" (deleted)")
        path = exe.removesuffix(" (deleted)")
        if path.startswith("/memfd:"):
            findings.append(Finding("suspicious", "process", f"the program {comm} running only from memory", "",
                                    f"pid {pid}: {cmdline[:200]}",
                                    ["has no file on disk, a trick some malware uses to hide"], "none", pid=pid))
            continue
        prov = provenance(ctx, path)
        if path.startswith(TEMP_DIRS):
            findings.append(Finding("danger", "process", f"the program {comm} running from a temp folder", path,
                                    f"pid {pid}: {cmdline[:200]}",
                                    ["runs from a temp folder, which normal programs don't"] +
                                    (["deleted its own file after starting"] if deleted else []),
                                    "quarantine" if not deleted else "none", pid=pid))
            continue
        if deleted:
            if prov == "package" or path in (ctx.owned or {}):
                continue  # updated while running: normal, a restart picks up the new version
            if updated_itself(path):
                continue  # app-1.0.167/x is gone but app-1.0.168/x is there: the app updated while running
            if prov in ("unknown", "missing") and not Path(path).exists():
                findings.append(Finding("suspicious", "process", f"the program {comm}, whose file was deleted", path,
                                        f"pid {pid}: {cmdline[:200]}",
                                        ["deleted its own file after starting, which malware does to hide"],
                                        "none", pid=pid))
            continue
        if prov == "unknown" and path not in seen:
            seen.add(path)
            inventory.append({"path": path, "why": "running now", "name": comm})
            targets.append(Path(path))
            if SYSTEM_NAMES.match(comm) and hidden_odd(ctx, path):
                findings.append(Finding("danger", "process", f"a program calling itself {comm}", path,
                                        f"pid {pid}: {cmdline[:200]}",
                                        [f"uses the system name '{comm}' but isn't the system program",
                                         "lives in a hidden folder"], "quarantine", pid=pid))
    findings += hidden_pids(ctx)
    return findings, inventory, targets


def updated_itself(path: str) -> bool:
    """Self-updating apps (Discord, Electron apps) replace their versioned folder while running."""
    p = Path(path)
    if Path(path).exists():
        return True  # replaced in place by an update
    gp = p.parent.parent
    try:
        return any((d / p.name).exists() for d in gp.iterdir() if d.is_dir() and d != p.parent)
    except OSError:
        return False


def hidden_pids(ctx: Ctx) -> list[Finding]:
    """A process the kernel knows (kill(pid, 0) works) that the /proc listing leaves out = hidden by a rootkit.
    Threads aren't listed either, so only Tgid == pid counts; twice, so a process that just started isn't one."""
    if ctx.root != "/":
        return []
    try:
        pid_max = int(Path("/proc/sys/kernel/pid_max").read_text())
    except OSError:
        return []

    def scan() -> set[int]:
        listed = {int(n) for n in os.listdir("/proc") if n.isdigit()}
        found = set()
        # PIDs are handed out in order, so a hidden process sits below the newest one (+ a margin): ~40k checks
        # instead of all 4 million possible PIDs (that took minutes inside the busy daemon)
        for pid in range(1, min(pid_max, max(listed, default=0) + 10000) + 1):
            if pid in listed:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            try:
                tgid = re.search(r"^Tgid:\s*(\d+)", Path(f"/proc/{pid}/status").read_text(), re.M)
            except OSError:
                continue  # gone already
            if tgid and int(tgid[1]) == pid:
                found.add(pid)
        return found

    first = scan()
    hidden = first & scan() if first else set()
    return [Finding("danger", "process", f"a hidden process ({pid})", "", f"pid {pid} exists but is hidden from the "
                    "process list", ["is hidden from the process list, which only rootkits do"]) for pid in sorted(hidden)]


# ── system integrity ────────────────────────────────────────────────────────
PROGRAM_DIRS = ("/usr/bin/", "/usr/sbin/", "/usr/lib/", "/usr/lib32/", "/usr/libexec/")


async def integrity(ctx: Ctx, qkk: str | None = None) -> tuple[list[Finding], dict]:
    """pacman -Qkk: a changed program or library is serious; changed settings and caches are normal."""
    if qkk is None:
        qkk = await pacman_qkk()
    changed: dict[str, dict] = {}
    unreadable = packages = 0
    for line in qkk.splitlines():
        if re.search(r": \d+ total files, \d+ altered files?$", line):
            packages += 1
            continue
        m = re.match(r"^(warning|backup file): ([^:]+): (/.+?) \((.+)\)$", line)
        if not m:
            continue
        kind, pkg, path, why = m.groups()
        if kind == "backup file":
            continue  # settings the user (or an update) changed
        if re.search(r"Permission denied|failed to calculate", why):
            unreadable += 1
            continue
        if re.search(r"SHA256 checksum mismatch|Size mismatch|File type mismatch", why):
            changed.setdefault(path, {"pkg": pkg, "why": set()})["why"].add(why)
        elif "Permissions mismatch" in why:
            try:
                st = ctx.sys(path).lstat()
            except OSError:
                continue
            if st.st_mode & stat.S_ISUID and st.st_uid == 0:  # became setuid-root (setgid utmp etc. is packaging)
                changed.setdefault(path, {"pkg": pkg, "why": set()})["why"].add("now runs as root for everyone (setuid)")
    findings = []
    for path, info in changed.items():
        disk = ctx.sys(path)
        program = (path.startswith(PROGRAM_DIRS) and not disk.is_symlink() and (
            is_program(disk) or re.search(r"\.so(\.\d+)*$", path))) or any("setuid" in w for w in info["why"])
        if program:
            findings.append(Finding("danger", "integrity", f"the system program {Path(path).name}", path,
                                    f"from the package {info['pkg']}: " + ", ".join(sorted(info["why"])),
                                    ["is different from the official version, like a trojaned program would be"],
                                    "reinstall", package=info["pkg"]))
        else:
            findings.append(Finding("note", "integrity", f"{path} changed", path,
                                    f"{info['pkg']}: " + ", ".join(sorted(info["why"])),
                                    ["is a data/cache file or link, normal after updates"]))
    return findings, {"packages": packages, "unreadable": unreadable, "altered": sorted(changed)}


async def pacman_qkk(parts: int = 6) -> str:
    """pacman -Qkk over all packages, split into groups checked side by side (idle priority): one long run took
    ~40 s, most of a quick scan."""
    _, names, _ = await run("pacman", "-Qq", timeout=60)
    pkgs = names.split()
    if not pkgs:
        return ""
    groups = [pkgs[i::parts] for i in range(parts)]
    results = await asyncio.gather(*(run("nice", "-n", "19", "ionice", "-c", "3", "pacman", "-Qkk", *g, timeout=900)
                                     for g in groups if g))
    return "\n".join(out + "\n" + err for _, out, err in results)


def unowned_programs(ctx: Ctx) -> tuple[list[Finding], list[Path]]:
    """Programs in system folders that no package installed. /usr/local and /opt are for that, so those are just
    content-scanned; in /usr/bin and /usr/lib they are suspicious."""
    findings, targets = [], []
    owned = ctx.owned or {}
    for d in ("/usr/bin", "/usr/sbin"):
        disk = ctx.sys(d)
        if not disk.is_dir() or disk.is_symlink():  # /usr/sbin → bin on Arch: same files
            continue
        for f in disk.iterdir():
            p = f"{d}/{f.name}"
            if owned_by(ctx, p) or f.is_symlink() or not f.is_file():
                continue
            targets.append(f)
            if is_program(f):
                findings.append(Finding("suspicious", "integrity", f"the program {f.name} in {d}", p,
                                        "no package installed it", ["isn't from any package, and programs in /usr/bin normally all are"], "none"))
    for d in ("/usr/local/bin", "/usr/local/sbin", "/usr/local/lib", "/opt"):
        disk = ctx.sys(d)
        if disk.is_dir():
            targets += [f for f in disk.rglob("*") if f.is_file() and not f.is_symlink() and
                        not owned_by(ctx, str(f)) and is_program(f)][:5000]
    return findings, targets


def desktop_inventory(ctx: Ctx) -> list[dict]:
    """Apps in the menu whose program isn't from a package (Orbit, AppImages, Wine shortcuts)."""
    out = []
    d = ctx.home / ".local/share/applications"
    if not d.is_dir():
        return out
    from .tools import _exec_argv

    for f in d.rglob("*.desktop"):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
            e = cp["Desktop Entry"]
            argv = _exec_argv(e.get("Exec", ""))
        except (KeyError, configparser.Error, OSError, UnicodeDecodeError, ValueError):
            continue
        target = command_target(ctx, " ".join(shlex.quote(a) for a in argv)) if argv else ""
        if target and provenance(ctx, target) == "unknown":
            out.append({"path": target, "why": "in your app menu", "name": e.get("Name", f.stem),
                        "about": e.get("Comment") or e.get("GenericName") or ""})
    return out


def file_mode_ok(path: Path, uid: int) -> bool:
    """Quarantine only touches the user's own regular files."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == uid
