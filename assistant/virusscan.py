"""Deep virus scan, the way incident responders do it: what starts on its own → what is running → do system
programs still match their packages → file contents (ClamAV signatures + the YARA Forge "core" rule set).
Design: docs/superpowers/specs/2026-11-01-mio-virus-scan-design.md. Structural checks live in scan_checks.py.

Nothing is ever deleted: "get rid of it" moves files into a locked quarantine that `restore` undoes, and only
the user's own files are touched (package files get a reinstall offer instead).

Tests: `python -m assistant.virusscan --home FAKE_HOME --root FAKE_ROOT [--qkk FILE] [--quick]` scans a fake tree.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import signal
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Awaitable, Callable

from . import scan_checks as sc
from .config import PROJECT_DIR, RUNTIME, STATE_DIR, XDG_DATA
from .scan_checks import LEVELS, TEMP_DIRS, Ctx, Finding

log = logging.getLogger("virusscan")

SCANS_DIR = STATE_DIR / "scans"
TRUST_FILE = STATE_DIR / "scan_trust.json"
QUARANTINE = XDG_DATA / "assistant" / "quarantine"
YARA_DIR = XDG_DATA / "assistant" / "yara"
CLAM_TMP = Path.home() / ".cache" / "mio-clamd-tmp"  # archives unpack here: on disk, /tmp is RAM
MAX_REMOVE = 20
YARA_FORGE = "https://api.github.com/repos/YARAHQ/yara-forge/releases/latest"
# a harmless marker so the scanner can prove its YARA path works end to end (tests plant it in a file)
SELFTEST_MARK = "MIO-SCANNER-" + "SELFTEST-1f0c3a9e"  # in parts: this file is no test file
SELFTEST_RULE = f'rule Mio_Selftest {{ meta: description = "Mio scanner self-test marker" score = 100 ' \
                f'strings: $a = "{SELFTEST_MARK}" condition: $a }}\n'


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _short(p: str) -> str:
    h = str(Path.home())
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def _die_with_parent():
    """In the child before exec: the kernel kills clamd if Mio dies (a crash or restart mid-scan would otherwise
    leave a 2.5 GB engine behind). nice/ionice exec straight into clamd, so the setting carries over."""
    import ctypes

    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


# ── ClamAV: the engine is started for the scan only, as the user, at idle priority ─────────────────────────
class Clam:
    def __init__(self, cfg: dict):
        self.bin = cfg.get("clamd") or shutil.which("clamd") or "/usr/bin/clamd"
        self.db = Path(cfg.get("clamav_db", "/var/lib/clamav"))
        self.certs = cfg.get("clamav_certs", "")
        self.threads = int(cfg.get("threads") or max(2, min(12, (os.cpu_count() or 4) - 4)))
        self.max_mb = int(cfg.get("max_file_mb", 400))
        self.pua = bool(cfg.get("detect_pua", False))
        self.cfg_scan_time = float(cfg.get("max_seconds_per_file", 60))
        self.sock = RUNTIME / f"mio-clamd-{os.getpid()}.sock"  # two scans never share one
        self.proc: asyncio.subprocess.Process | None = None

    def available(self) -> bool:
        return Path(self.bin).exists() and any(self.db.glob("*.c[vl]d"))

    def sig_age_days(self) -> float:
        files = list(self.db.glob("daily.c[vl]d"))
        return (time.time() - max(f.stat().st_mtime for f in files)) / 86400 if files else 999

    async def start(self):
        for old in RUNTIME.glob("mio-clamd-*.*"):  # left by a scan that was killed hard
            pid = re.search(r"-(\d+)\.", old.name)
            if pid and not Path(f"/proc/{pid[1]}").exists():
                old.unlink(missing_ok=True)
        CLAM_TMP.mkdir(parents=True, exist_ok=True)
        conf = RUNTIME / f"mio-clamd-{os.getpid()}.conf"
        lines = ["Foreground yes", f"LocalSocket {self.sock}", f"DatabaseDirectory {self.db}",
                 f"TemporaryDirectory {CLAM_TMP}", f"MaxThreads {self.threads}", "MaxQueue 200",
                 f"MaxFileSize {self.max_mb}M", f"MaxScanSize {self.max_mb}M", "IdleTimeout 600",
                 f"MaxScanTime {int(self.cfg_scan_time * 1000)}",  # one huge archive can't hog a thread
                 f"DetectPUA {'yes' if self.pua else 'no'}", "LogTime no", "ExitOnOOM yes",
                 # report (as notes) what it couldn't look into fully, instead of skipping it silently; a real
                 # detection still wins over these (HeuristicScanPrecedence stays off)
                 "AlertExceedsMax yes", "AlertEncryptedArchive yes", "AlertEncryptedDoc yes"]
        if self.certs:
            lines.append(f"CVDCertsDirectory {self.certs}")
        conf.write_text("\n".join(lines) + "\n")
        self.sock.unlink(missing_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "19", "ionice", "-c", "3", self.bin, f"--config-file={conf}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            preexec_fn=_die_with_parent)
        for _ in range(240):  # loading 3+ million signatures takes ~5-20 s
            if self.proc.returncode is not None:
                err = (await self.proc.stderr.read()).decode(errors="replace")[-300:]
                raise RuntimeError(f"ClamAV didn't start: {err.strip()}")
            if self.sock.exists():
                try:
                    r, w = await asyncio.open_unix_connection(str(self.sock))
                    w.write(b"zPING\0")
                    ok = await asyncio.wait_for(r.readuntil(b"\0"), 5)
                    w.close()
                    if ok.startswith(b"PONG"):
                        return
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                    pass
            await asyncio.sleep(0.5)
        raise RuntimeError("ClamAV took too long to start")

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                await asyncio.wait_for(self.proc.wait(), 10)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None
        self.sock.unlink(missing_ok=True)
        self.sock.with_suffix(".conf").unlink(missing_ok=True)
        shutil.rmtree(CLAM_TMP, ignore_errors=True)

    async def session(self):
        r, w = await asyncio.open_unix_connection(str(self.sock))
        w.write(b"zIDSESSION\0")
        await w.drain()
        return r, w

    @staticmethod
    async def scan_one(r, w, path: str) -> tuple[str, str]:
        """('ok'|'found'|'error', signature or message)"""
        w.write(b"zSCAN " + os.fsencode(path) + b"\0")
        await w.drain()
        reply = (await asyncio.wait_for(r.readuntil(b"\0"), 600)).rstrip(b"\0").decode(errors="replace")
        reply = reply.split(": ", 1)[1] if re.match(r"^\d+: ", reply) else reply
        if reply.endswith(" FOUND"):
            return "found", reply[:-6].rsplit(": ", 1)[-1]
        if reply.endswith(" ERROR"):
            return "error", reply[:-6]
        return "ok", ""


def clam_level(sig: str) -> tuple[str, str]:
    if sig.startswith("Heuristics.Limits.Exceeded"):
        return "note", "too big or complex for ClamAV to check completely (it checked what it could)"
    if sig.startswith("Heuristics.Encrypted"):
        return "note", "password-protected, so ClamAV couldn't look inside (malware is sometimes shipped like this)"
    if sig.startswith(("PUA.", "Heuristics.")):
        return "suspicious", f"ClamAV calls it {sig} (possibly unwanted or unusual, not a known virus)"
    if "Eicar" in sig:
        return "danger", f"ClamAV: {sig} (the standard antivirus test file, harmless but used to test scanners)"
    return "danger", f"ClamAV identifies it as {sig}"


# ── YARA Forge rules ────────────────────────────────────────────────────────
async def ensure_rules(max_age_days: float = 7) -> Path | None:
    """Compiled YARA Forge core rules (+ the self-test rule), refreshed weekly. None if yara isn't installed or
    there are no rules and no internet."""
    if not shutil.which("yara") or not shutil.which("yarac"):
        return None
    YARA_DIR.mkdir(parents=True, exist_ok=True)
    compiled = YARA_DIR / "rules.yarc"
    fresh = compiled.exists() and time.time() - compiled.stat().st_mtime < max_age_days * 86400
    if fresh:
        return compiled
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            rel = (await client.get(YARA_FORGE)).json()
            url = next(a["browser_download_url"] for a in rel["assets"] if a["name"] == "yara-forge-rules-core.zip")
            data = (await client.get(url)).content
        dl = YARA_DIR / "download.zip"
        dl.write_bytes(data)
        with zipfile.ZipFile(dl) as z:  # rules are data for yarac, extracted by name only
            name = next(n for n in z.namelist() if n.endswith(".yar"))
            (YARA_DIR / "core.yar").write_bytes(z.read(name))
        dl.unlink()
        (YARA_DIR / "VERSION").write_text(rel.get("tag_name", "?"))
    except Exception as e:
        log.warning("couldn't refresh YARA rules: %s", e)
        if compiled.exists():
            return compiled
        if not (YARA_DIR / "core.yar").exists():
            return None
    (YARA_DIR / "selftest.yar").write_text(SELFTEST_RULE)
    code, _, err = await sc.run("yarac", str(YARA_DIR / "core.yar"), str(YARA_DIR / "selftest.yar"),
                                str(compiled) + ".new", timeout=300)
    if code:
        log.warning("yarac failed: %s", err[-300:])
        return compiled if compiled.exists() else None
    os.replace(str(compiled) + ".new", compiled)
    return compiled


def _meta(s: str) -> dict:
    return {k: (v1 if v1 else v2) for k, v1, v2 in re.findall(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|([^,\]]+))', s)}


async def yara_scan(rules: Path, files: list[Path], threads: int) -> list[tuple[str, str, dict]]:
    """[(path, rule, meta)] — one call over a file list, idle priority."""
    if not files:
        return []
    lst = YARA_DIR / "scanlist.txt"
    lst.write_text("\n".join(str(f) for f in files) + "\n")
    proc = await asyncio.create_subprocess_exec(
        "nice", "-n", "19", "ionice", "-c", "3", "yara", "-C", "-m", "-f", "-p", str(threads), "-z", "50000000",
        "-a", "60", "--scan-list", str(rules), str(lst),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    lst.unlink(missing_ok=True)
    hits = []
    for line in out.decode(errors="replace").splitlines():
        m = re.match(r"^(\S+) \[(.*)\] (/.*)$", line)
        if m:
            hits.append((m[3], m[1], _meta(m[2])))
    return hits


def yara_level(rule: str, meta: dict) -> tuple[str, str]:
    desc = meta.get("description", "").strip() or rule
    if rule == "Mio_Selftest":
        return "danger", "it contains Mio's scanner self-test marker (a harmless test file)"
    try:
        score = int(meta.get("score", 75))
    except ValueError:
        score = 75
    weak = re.search(r"(^|_)(SUSP|HKTL|PUA|Hacktool|Generic|Heuristic|Webshell_Generic)", rule, re.I)
    level = "suspicious" if weak or score < 70 else "danger"
    return level, f"YARA rule {rule}: {desc}"


# ── walking files ───────────────────────────────────────────────────────────
def _skip_dirs(ctx: Ctx, clam: Clam | None) -> set[str]:
    s = {str(QUARANTINE), str(YARA_DIR), str(CLAM_TMP), "/proc", "/sys", "/.snapshots", str(ctx.home / ".snapshots")}
    if clam:
        s.add(str(clam.db))
    return s


def walk(roots: list[Path], skip: set[str], max_bytes: int):
    """Regular files only, symlinks never followed, unreadable folders skipped. Yields (path, st)."""
    stack = [str(r) for r in roots if r.exists()]
    seen = set()
    while stack:
        d = stack.pop()
        if d in skip or d in seen:
            continue
        seen.add(d)
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        if 0 < st.st_size <= max_bytes:
                            yield e.path, st
                except OSError:
                    continue


def quick_roots(h: Path) -> list[Path]:
    """Where Linux (and Wine) malware usually hides — the quick scan reads only these (plus everything that starts
    on its own, running programs and unowned system programs, which every scan checks)."""
    roots = [h / d for d in ("Downloads", "Desktop", ".local/bin", "bin", ".config/autostart", ".config/systemd",
                             ".local/share/applications", ".local/share/systemd")]
    roots += [Path(t) for t in TEMP_DIRS] + [Path("/usr/local")]
    try:  # hidden folders right in home that no normal app uses (~/.x11, ~/.cache2 …)
        roots += [d for d in h.iterdir() if d.name.startswith(".") and d.is_dir() and not d.is_symlink()
                  and d.name not in sc.KNOWN_DOT]
    except OSError:
        pass
    # browser extensions: malicious ones are among the most common real infections
    roots += [p for p in h.glob(".config/*/*/*/Extensions")]          # Brave, Chrome, Chromium, Vivaldi …
    roots += [p for p in h.glob(".config/*/*/Extensions")]
    roots += [p for p in h.glob(".mozilla/firefox/*/extensions")]
    # Discord: token stealers patch its core module so they run every time Discord starts
    roots += [p for p in h.glob(".config/discord*/*/modules/discord_desktop_core*")]
    # Windows malware in Wine starts from the Startup folders
    for prefix in [h / ".wine"] + list(h.glob(".cache/*/wine")) + list(h.glob("Games/*")) + \
            list(h.glob(".local/share/Steam/steamapps/compatdata/*/pfx")):
        roots += list(prefix.glob("drive_c/users/*/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"))
        roots += list(prefix.glob("drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs/StartUp"))
    return [r for r in dict.fromkeys(roots) if r.exists()]


def _top_dir(home: Path, path: str) -> str:
    """~/.cache/x/… → '~/.cache/x', /usr/local/… → '/usr/local'"""
    h = str(home)
    if path.startswith(h + "/"):
        parts = path[len(h) + 1:].split("/")
        return "~/" + "/".join(parts[:2 if parts[0].startswith(".") and len(parts) > 2 else 1])
    return "/".join(path.split("/")[:3])


def risky(ctx: Ctx, path: str, st: os.stat_result) -> bool:
    """Gets the YARA rules too: programs and scripts where malware usually sits."""
    h = str(ctx.home)
    hot = path.startswith(TEMP_DIRS) or any(path.startswith(f"{h}/{d}/") for d in
                                           ("Downloads", "Desktop", ".local/bin", "bin", ".config/autostart"))
    if not hot and not (st.st_mode & 0o111) and not path.endswith((".sh", ".py", ".js", ".exe", ".dll", ".ps1",
                                                                     ".bat", ".vbs", ".jar", ".scr", ".AppImage")):
        return False
    if "/drive_c/" in path and "/drive_c/windows/" not in path.lower() and \
            path.lower().endswith((".exe", ".dll", ".scr", ".bat", ".ps1", ".vbs", ".js", ".cpl", ".sys")):
        return True  # programs installed into Wine/Proton (not Wine's own Windows files)
    if not hot and sc.provenance(ctx, path) in ("container", "mio", "flatpak"):
        return False
    return hot or sc.hidden_odd(ctx, path) or sc.is_program(Path(path))


# ── the scan ────────────────────────────────────────────────────────────────
class ScanJob:
    def __init__(self, cfg: dict, quick: bool = False, paused: Callable[[], bool] = lambda: False,
                 ctx: Ctx | None = None, qkk: str | None = None, roots: list[Path] | None = None,
                 use_clam: bool = True):
        self.cfg, self.quick, self.paused = cfg, quick, paused
        self.ctx = ctx or Ctx()
        self.qkk, self.roots = qkk, roots
        self.clam = Clam(cfg) if use_clam else None
        self.phase, self.done, self.total = "starting", 0, 0
        self.started = time.time()
        self.cancelled = False
        self.report: dict | None = None
        self.paused_s, self._pausing = 0.0, False

    def status(self) -> str:
        mins = int((time.time() - self.started) // 60)
        if self.phase == "files" and self.total:
            pct = min(99, int(100 * self.done / self.total))
            if self.paused():
                return f"scanning files: {self.done:,} of {self.total:,} ({pct}%) — paused while you play, " \
                       "it carries on when you close the game"
            busy = time.time() - self.files_t - self.paused_s  # time spent paused doesn't count
            rate = self.done / max(1, busy)
            left = (self.total - self.done) / rate / 60 if rate else 0
            eta = f", about {max(1, round(left))} more minutes" if left >= 1 else ", almost done"
            return f"scanning files: {self.done:,} of {self.total:,} ({pct}%){eta}"
        return f"{self.phase} ({mins} min so far)"

    async def _wait_unpaused(self):
        while self.paused() and not self.cancelled:
            if not self._pausing:  # many workers wait at once: count the pause only once
                self._pausing = True
                t0 = time.time()
                while self.paused() and not self.cancelled:
                    await asyncio.sleep(2)
                self.paused_s += time.time() - t0
                self._pausing = False
            await asyncio.sleep(2)

    def _phase(self, name: str, rep: dict):
        now = time.time()
        if getattr(self, "_ph", None):
            rep["phases"][self._ph[0]] = round(now - self._ph[1], 1)
        self._ph = (name, now) if name else None
        if name:
            self.phase = name

    async def run(self) -> dict:
        ctx = self.ctx
        rep = {"t": _now(), "quick": self.quick, "findings": [], "inventory": [], "errors": [], "files": 0,
               "too_big": 0, "clamav": None, "yara": None, "packages": 0, "unreadable": 0, "trusted_skipped": 0,
               "phases": {}, "extensions": []}
        try:
            ctx.trust = load_trust()
            self._phase("reading which files belong to packages", rep)
            await sc.load_owned(ctx)
            found: list[Finding] = []

            self._phase("checking what starts on its own", rep)
            f, inv, targets = sc.startup_findings(ctx)
            found += f + sc.system_hooks(ctx) + sc.env_findings(ctx) + sc.menu_findings(ctx) + \
                sc.path_hijack(ctx) + sc.discord_core(ctx)
            rep["inventory"] += inv + sc.desktop_inventory(ctx)
            rep["extensions"] = sc.extensions(ctx)
            for e in rep["extensions"]:
                if not e["store"]:
                    found.append(Finding("note", "startup", f"the {e['browser']} extension {e['name']}", e["path"],
                                         f"id {e['id']}", ["was installed from outside the official store "
                                                           "(fine if you added it yourself)"]))

            self._phase("checking running programs", rep)
            f, inv, ptargets = await asyncio.to_thread(sc.processes, ctx)
            found += f
            ports = await asyncio.to_thread(sc.listening, ctx)
            for i in inv:
                i["ports"] = ports.get(i["path"], [])
            rep["inventory"] += inv
            targets += ptargets

            await self._wait_unpaused()
            self._phase("checking system programs against their packages", rep)
            f, stats = await sc.integrity(ctx, self.qkk)
            found += f
            self.altered = set(stats.pop("altered", []))
            rep.update(stats)
            f, utargets = sc.unowned_programs(ctx)
            found += f
            targets += utargets

            found += await self._files(rep, targets)
            self._phase("", rep)
            await self._lookup(found, rep)
            rep["findings"] = [x.to_dict() for x in self._merge(found, rep)]
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            if self.clam:
                await self.clam.stop()
        rep["duration_s"] = round(time.time() - self.started)
        self.report = rep
        save_report(rep)
        return rep

    async def _files(self, rep: dict, extra: list[Path]) -> list[Finding]:
        ctx, h = self.ctx, self.ctx.home
        max_bytes = (self.clam.max_mb if self.clam else 400) << 20
        if self.quick:
            max_bytes = min(max_bytes, 30 << 20)  # dropped malware is small; big archives are what take long
        if self.roots is not None:
            roots = self.roots
        elif self.quick:
            roots = quick_roots(h)
        else:
            roots = [h] + [Path(p) for p in (*TEMP_DIRS, "/opt", "/usr/local", "/etc", "/srv")]
        skip = _skip_dirs(ctx, self.clam)
        self._phase("counting files", rep)
        self.total = await asyncio.to_thread(lambda: sum(1 for _ in walk(roots, skip, max_bytes)))
        self.total += len(extra)
        self.done, self.files_t = 0, time.time()

        use_clam = bool(self.clam and self.clam.available())
        rep["clamav"] = {"used": use_clam, "sig_age_days": round(self.clam.sig_age_days(), 1) if use_clam else None}
        rules = await ensure_rules()
        rep["yara"] = {"used": bool(rules), "version": (YARA_DIR / "VERSION").read_text().strip()
                       if (YARA_DIR / "VERSION").exists() else ""}
        if use_clam:
            self._phase("starting ClamAV", rep)
            await self.clam.start()
        self._phase("files", rep)
        found: list[Finding] = []
        to_yara: list[Path] = list(dict.fromkeys(p for p in extra if p.is_file()))
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        slow: dict[str, float] = {}  # seconds of ClamAV time per top folder: where a long scan went

        async def worker():
            r = w = None
            while True:
                path = await queue.get()
                if path is None:
                    break
                await self._wait_unpaused()
                if self.cancelled:
                    break
                for attempt in (1, 2):
                    try:
                        if r is None:
                            r, w = await self.clam.session()
                        t0 = time.monotonic()
                        kind, msg = await Clam.scan_one(r, w, path)
                        spent = time.monotonic() - t0
                        top = _top_dir(ctx.home, path)
                        slow[top] = slow.get(top, 0.0) + spent
                        break
                    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError) as e:
                        r = w = None  # session timed out (e.g. a long pause): reconnect once
                        kind, msg = "error", str(e)
                if kind == "found":
                    level, why = clam_level(msg)
                    found.append(Finding(level, "signature", f"the file {Path(path).name}", path,
                                         f"in {_short(str(Path(path).parent))}", [why],
                                         "quarantine" if level != "note" else "none"))
                elif kind == "error":
                    rep["errors"].append(f"{_short(path)}: {msg[:120]}")
                self.done += 1
            if w is not None:
                try:
                    w.write(b"zEND\0")
                    w.close()
                except OSError:
                    pass

        workers = [asyncio.create_task(worker()) for _ in range(self.clam.threads)] if use_clam else []
        loop = asyncio.get_running_loop()
        gen = walk(roots, skip, max_bytes)
        owned, altered = (ctx.owned if ctx.root == "/" else None), getattr(self, "altered", set())
        rep["verified"] = 0
        while True:
            batch = await loop.run_in_executor(None, lambda: [x for _, x in zip(range(2000), gen)])
            if not batch:
                break
            for path, st in batch:
                if self.cancelled:
                    break
                rep["files"] += 1
                if st.st_mode & stat.S_ISUID and (f := sc.setuid_finding(ctx, path, st)):
                    found.append(f)
                if owned and path in owned and path not in altered:
                    rep["verified"] += 1  # checksum just matched its package: the same bytes as the official file
                    self.done += 1
                    continue
                if self.quick or risky(ctx, path, st):
                    to_yara.append(Path(path))
                if use_clam:
                    await queue.put(path)
                else:
                    self.done += 1
        for p in extra:
            if use_clam and p.is_file():
                await queue.put(str(p))
        for _ in workers:
            await queue.put(None)
        if workers:
            await asyncio.gather(*workers)
        self.done = self.total

        if rules and to_yara:
            self._phase(f"checking {len(to_yara):,} programs and scripts with YARA rules", rep)
            await self._wait_unpaused()
            for path, rule, meta in await yara_scan(rules, list(dict.fromkeys(to_yara)), self.clam.threads
                                                    if self.clam else 4):
                level, why = yara_level(rule, meta)
                found.append(Finding(level, "yara", f"the file {Path(path).name}", path,
                                     f"in {_short(str(Path(path).parent))}", [why], "quarantine"))
        rep["yara_files"] = len(set(to_yara))
        rep["clam_seconds_by_folder"] = dict(sorted(((k, round(v)) for k, v in slow.items()), key=lambda kv: -kv[1])[:15])
        return found

    async def _lookup(self, found: list[Finding], rep: dict):
        """What the web says about flagged programs we don't know — only explains, never decides."""
        names = {}
        for f in found:
            if f.level != "note" and f.path and f.kind == "process" and sc.provenance(self.ctx, f.path) == "unknown":
                names.setdefault(Path(f.path).name, []).append(f)
        if not names or self.cfg.get("web_lookup", True) is False:
            return
        try:
            from . import web
        except Exception:
            return
        for name, fs in list(names.items())[:5]:
            try:
                hits = await asyncio.wait_for(asyncio.to_thread(web.search, f'"{name}" linux program'), 10)
            except Exception:
                continue
            bodies = [h.get("body", "").strip()[:160] for h in hits[:2] if h.get("body", "").strip()]
            if bodies:
                note = f"a web search for '{name}' says (unverified): " + " / ".join(bodies)
                for f in fs:
                    f.evidence.append(note)

    def _merge(self, found: list[Finding], rep: dict) -> list[Finding]:
        by: dict[str, Finding] = {}
        out: list[Finding] = []
        for f in found:
            key = f.path or f.title
            if f.path and Path(f.path).exists():
                f.sha256 = f.sha256 or sc.sha256(self.ctx.sys(f.path))
            if f.level != "note" and sc.trusted(self.ctx, f.path, f.sha256):
                rep["trusted_skipped"] += 1
                continue
            if key in by:
                g = by[key]
                g.evidence += [e for e in f.evidence if e not in g.evidence]
                if LEVELS[f.level] > LEVELS[g.level]:
                    g.level, g.title = f.level, f.title
                g.pid = g.pid or f.pid
                g.unit = g.unit or f.unit
                if g.fix == "none":
                    g.fix = f.fix
                continue
            by[key] = f
            out.append(f)
        for f in out:  # only the user's own files can be quarantined
            if f.fix in ("quarantine", "disable_unit", "restore_discord") and not can_quarantine(self.ctx, f.path):
                f.fix = "none"
        out.sort(key=lambda f: -LEVELS[f.level])
        n = 0
        for f in out:
            if f.level != "note":
                n += 1
                f.id = str(n)
        flagged = {f.path for f in out if f.level != "note"} | {f.target for f in out if f.target}
        inv, seen = [], set(flagged)
        for i in rep["inventory"]:
            if i["path"] not in seen:
                seen.add(i["path"])
                inv.append(i)
        rep["inventory"] = inv
        return out


# ── quarantine / restore / trust ────────────────────────────────────────────
def can_quarantine(ctx: Ctx, path: str) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        real = str(p.resolve())
    except OSError:
        return False
    h = str(ctx.home)
    inside = real.startswith(h + "/") or real.startswith(tuple(t + "/" for t in TEMP_DIRS))
    forbidden = real.startswith((str(PROJECT_DIR) + "/", h + "/.ssh/", str(QUARANTINE)))
    return inside and not forbidden and real not in (ctx.owned or {}) and sc.file_mode_ok(p, ctx.uid)


async def quarantine(findings: list[dict], ctx: Ctx | None = None) -> list[str]:
    """Stop it, move it into the quarantine (locked, 0400) with a record for restore. Never deletes."""
    ctx = ctx or Ctx()
    await sc.load_owned(ctx)
    done = []
    for f in findings[:MAX_REMOVE]:
        path = f.get("path", "")
        if f.get("fix") == "restore_discord" and can_quarantine(ctx, path):
            box = Path(tempfile.mkdtemp(prefix=dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=_qdir()))
            shutil.copy2(path, box / "file")
            os.chmod(box / "file", 0o400)
            (box / "record.json").write_text(json.dumps({"path": path, "mode": 0o644, "sha256": sc.sha256(path),
                                                          "why": f.get("evidence", []), "title": f.get("title"),
                                                          "replaced": True, "t": _now()}, indent=1))
            Path(path).write_text(sc.DISCORD_CLEAN + "\n")
            done.append("repaired Discord's core module (restart Discord); the changed file is in quarantine")
            f["fix"] = "done"
            continue
        if f.get("fix") not in ("quarantine", "disable_unit") or not can_quarantine(ctx, path):
            done.append(f"left {_short(path) or f['title']} alone (not one of your own files)")
            continue
        if f.get("pid"):
            try:
                exe = os.readlink(f"/proc/{f['pid']}/exe").removesuffix(" (deleted)")
                if exe == path:
                    os.kill(f["pid"], signal.SIGTERM)
                    for _ in range(30):
                        await asyncio.sleep(0.1)
                        os.kill(f["pid"], 0)
                    os.kill(f["pid"], signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if f.get("fix") == "disable_unit" and f.get("unit"):
            await sc.run("systemctl", "--user", "disable", "--now", f["unit"], timeout=30)
        # a fresh box per file: two in one second never collide
        box = Path(tempfile.mkdtemp(prefix=dt.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=_qdir()))
        st = os.lstat(path)
        record = {"path": path, "mode": stat.S_IMODE(st.st_mode), "sha256": f.get("sha256") or sc.sha256(path),
                  "why": f.get("evidence", []), "title": f.get("title"), "unit": f.get("unit", ""),
                  "t": _now()}
        dest = box / "file"
        if dest.exists():  # never overwrite something already in quarantine
            raise RuntimeError(f"quarantine box {box} is not empty")
        shutil.move(path, dest)
        os.chmod(dest, 0o400)
        (box / "record.json").write_text(json.dumps(record, indent=1))
        if f.get("unit") or "/systemd/user/" in path:  # a removed unit or override takes effect now
            await sc.run("systemctl", "--user", "daemon-reload", timeout=30)
        done.append(f"quarantined {_short(path)}")
        f["fix"] = "done"
        tgt = f.get("target", "")
        if tgt and tgt != path and can_quarantine(ctx, tgt):
            await quarantine([{**f, "path": tgt, "target": "", "unit": "", "fix": "quarantine", "pid": 0,
                               "sha256": "", "title": f"the program of {f.get('title')}"}], ctx)
            done.append(f"quarantined its program {_short(tgt)}")
    return done


def _qdir() -> Path:
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    return QUARANTINE


def quarantined() -> list[tuple[Path, dict]]:
    out = []
    for box in sorted(QUARANTINE.glob("*/record.json")) if QUARANTINE.exists() else []:
        try:
            out.append((box.parent, json.loads(box.read_text())))
        except (OSError, json.JSONDecodeError):
            pass
    return out


def restore(name: str = "") -> str:
    items = quarantined()
    if not items:
        return "the quarantine is empty"
    if name and name not in ("last", "all"):
        items = [i for i in items if name.lower() in i[1]["path"].lower()] or []
    elif name != "all":
        items = items[-1:]
    if not items:
        return f"nothing called {name} in the quarantine"
    done = []
    for box, rec in items:
        dest = Path(rec["path"])
        if dest.exists() and not rec.get("replaced"):
            done.append(f"{_short(rec['path'])} exists again, left it in quarantine")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(box / "file", 0o600)
        shutil.move(box / "file", dest)
        os.chmod(dest, rec.get("mode", 0o644))
        shutil.rmtree(box, ignore_errors=True)
        add_trust(rec["path"], rec.get("sha256", ""), "restored by the user")
        done.append(f"restored {_short(rec['path'])}")
    return "; ".join(done)


def load_trust() -> list[dict]:
    try:
        return json.loads(TRUST_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def add_trust(path: str, digest: str, why: str = "the user said it's fine"):
    items = [t for t in load_trust() if t.get("path") != path]
    items.append({"path": path, "sha256": digest, "why": why, "t": _now()})
    TRUST_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRUST_FILE.write_text(json.dumps(items, indent=1))


# ── report + what she says ──────────────────────────────────────────────────
def save_report(rep: dict) -> Path:
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = rep["t"].replace(":", "").replace("-", "")[:13]
    (SCANS_DIR / f"{stamp}.json").write_text(json.dumps(rep, indent=1))
    (SCANS_DIR / "latest.json").write_text(json.dumps(rep, indent=1))
    md = SCANS_DIR / f"{stamp}.md"
    md.write_text(markdown(rep))
    for old in sorted(SCANS_DIR.glob("2*.md"))[:-30] + sorted(SCANS_DIR.glob("2*.json"))[:-30]:
        old.unlink(missing_ok=True)  # keep the last 30 scans
    rep["report_md"] = str(md)
    (SCANS_DIR / "latest.json").write_text(json.dumps(rep, indent=1))
    return md


def latest() -> dict | None:
    try:
        return json.loads((SCANS_DIR / "latest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def update_latest(rep: dict):
    (SCANS_DIR / "latest.json").write_text(json.dumps(rep, indent=1))


def markdown(rep: dict) -> str:
    L = [f"# Mio virus scan — {rep['t'].replace('T', ' ')}", "",
         f"{'Quick' if rep['quick'] else 'Deep'} scan, " + (f"{rep.get('duration_s', 0)} s. " if rep.get('duration_s', 0)
                                                             < 180 else f"{round(rep['duration_s'] / 60)} min. ") +
         f"{rep['files']:,} files read ({rep.get('verified', 0):,} of them package files whose checksums matched, not re-scanned); ClamAV: {'yes' if (rep.get('clamav') or {}).get('used') else 'NOT USED'}"
         f"{' (signatures %s days old)' % rep['clamav']['sig_age_days'] if (rep.get('clamav') or {}).get('used') else ''}; "
         f"YARA Forge core {(rep.get('yara') or {}).get('version', '')} on {rep.get('yara_files', 0):,} programs/scripts; "
         f"{rep['packages']:,} packages verified ({rep['unreadable']} files need root to check).", ""]
    blind = [f for f in rep["findings"] if f["level"] == "note" and f["kind"] == "signature"]
    for level, head in (("danger", "Dangerous"), ("suspicious", "Looks odd"), ("note", "Notes (fine, for the record)")):
        fs = [f for f in rep["findings"] if f["level"] == level and f not in blind]
        if not fs:
            continue
        L += [f"## {head}", ""]
        for f in fs:
            L.append(f"- **{('#' + f['id'] + ' ') if f['id'] else ''}{f['title']}** — `{f['path']}`")
            if f.get("detail"):
                L.append(f"  - {f['detail']}")
            L += [f"  - {e}" for e in f.get("evidence", [])]
            fix = {"quarantine": "can be quarantined", "disable_unit": "can be disabled + quarantined",
                   "reinstall": f"reinstall {f.get('package')}", "done": "QUARANTINED"}.get(f.get("fix"))
            if fix:
                L.append(f"  - action: {fix}")
        L.append("")
    if blind:
        L += [f"## ClamAV couldn't fully check {len(blind)} files", "",
              "Too big/complex (it checked what it could) or password-protected archives:", ""]
        L += [f"- `{_short(f['path'])}` — {f['evidence'][0].split(',')[0]}" for f in blind[:40]]
        L += ([f"- … and {len(blind) - 40} more"] if len(blind) > 40 else []) + [""]
    if rep["inventory"]:
        L += ["## Programs not from your package manager (no signs of harm)", ""]
        L += [f"- {i['name']} — `{i['path']}` ({i['why']}{', ' + i['about'] if i.get('about') else ''}"
              f"{'; listens on ' + ', '.join(i['ports'][:3]) if i.get('ports') else ''})" for i in rep["inventory"]]
        L.append("")
    if rep.get("extensions"):
        L += ["## Browser extensions", ""]
        L += [f"- {e['browser']}: {e['name']}{'' if e['store'] else ' — NOT from the store'}"
              f"{' (' + ', '.join(e.get('perms', [])[:5]) + ')' if e.get('perms') else ''}" for e in rep["extensions"]]
        L.append("")
    if rep.get("phases"):
        L += ["## Timing", ""] + [f"- {k}: {v:.0f} s" for k, v in rep["phases"].items()] + [""]
    if rep["trusted_skipped"]:
        L.append(f"{rep['trusted_skipped']} things you said are fine were skipped.\n")
    if rep["errors"]:
        L += ["## Couldn't read", ""] + [f"- {e}" for e in rep["errors"][:50]] + [""]
    return "\n".join(L)


PLATFORM = {"win": "Windows", "unix": "Linux", "linux": "Linux", "elf": "Linux", "js": "JavaScript",
            "html": "web page", "doc": "Office document", "xls": "Excel", "pdf": "PDF", "andr": "Android",
            "osx": "Mac", "macos": "Mac", "php": "PHP", "java": "Java", "py": "Python", "sh": "shell script",
            "multios": "multi-system", "img": "image", "email": "email", "txt": "text"}
CATEGORY = {"trojan": "trojan", "malware": "malware", "exploit": "exploit", "coinminer": "crypto miner",
            "miner": "crypto miner", "ransomware": "ransomware", "downloader": "downloader", "dropper": "dropper",
            "spyware": "spyware", "worm": "worm", "virus": "virus", "adware": "adware", "phishing": "phishing page",
            "backdoor": "backdoor", "rootkit": "rootkit", "keylogger": "keylogger", "infostealer": "info stealer",
            "packed": "packed program", "tool": "hacking tool", "joke": "joke program", "spam": "spam"}


def say_signature(sig: str) -> str:
    if "eicar" in sig.lower():
        return "it's an antivirus test file, harmless"
    parts = sig.lower().split(".")
    plat = next((PLATFORM[x] for x in parts if x in PLATFORM), "")
    cat = next((CATEGORY[x] for x in parts if x in CATEGORY), "")
    if cat:
        what = f"{plat} {cat}".strip()
        return f"ClamAV says it's {'an' if what[0].lower() in 'aeiou' else 'a'} {what}"
    return f"ClamAV says it's {sig}"


def _folder(path: str) -> str:
    parent = Path(path).parent
    rel = _short(str(parent))
    return "your home folder" if rel == "~" else rel.removeprefix("~/")


def _plain(f: dict) -> str:
    """One spoken line: what and why, no long paths."""
    ev = f.get("evidence") or [""]
    if f["kind"] == "signature":
        m = re.search(r"ClamAV(?: identifies it as|:| calls it) (\S+)", ev[0])
        return f"a file called {Path(f['path']).name} in {_folder(f['path'])}, " + say_signature(m[1] if m else "")
    if f["kind"] == "yara":
        why = "it's Mio's harmless scanner test file" if "self-test" in ev[0] else \
            "it matches a known malware pattern"
        return f"a file called {Path(f['path']).name} in {_folder(f['path'])}, {why}"
    why = " and ".join(e for e in ev[:2] if e and not e.startswith(("a web search", "points to")))
    if not why:
        return f["title"]
    return f"{f['title']}, which {why}" if not why.startswith("it") else f"{f['title']}: {why}"


def summary(rep: dict) -> tuple[str, str]:
    """(what she says, offer) — offer is 'quarantine', 'reinstall', or ''."""
    danger = [f for f in rep["findings"] if f["level"] == "danger"]
    odd = [f for f in rep["findings"] if f["level"] == "suspicious"]
    clam = (rep.get("clamav") or {}).get("used")
    age = (rep.get("clamav") or {}).get("sig_age_days") or 0
    stale = f" Heads up: my virus signatures are {int(age)} days old, they normally update daily." if age > 7 else ""
    checked = (f"I checked {rep['files']:,} files" + ("" if clam else " (without ClamAV, it isn't installed)") +
               f", {rep['packages']:,} system packages and everything that starts on its own.")
    unknown = rep["inventory"]
    inv_line = ""
    if unknown and not danger:
        names = list(dict.fromkeys(i["name"] for i in unknown))
        inv_line = (f" {len(names)} program{'s are' if len(names) > 1 else ' is'} not from your package manager, "
                    f"like {', '.join(names[:2])}, but nothing bad is in {'them' if len(names) > 1 else 'it'}.")
    if not danger and not odd:
        return f"Your PC looks clean! {checked}{inv_line}{stale}", ""
    fixable = [f for f in danger + odd if f.get("fix") in ("quarantine", "disable_unit", "restore_discord")]
    reinstall = sorted({f["package"] for f in danger if f.get("fix") == "reinstall"})
    parts = []
    if danger:
        parts.append(f"I found {len(danger)} thing{'s' if len(danger) > 1 else ''} that look{'' if len(danger) > 1 else 's'} "
                     f"dangerous: " + "; ".join(_plain(f) for f in danger[:3]) +
                     (f"; and {len(danger) - 3} more in the report" if len(danger) > 3 else "") + ".")
    if odd:
        parts.append(f"{'Also, ' if danger else 'Nothing dangerous, but '}{len(odd)} thing{'s look' if len(odd) > 1 else ' looks'} "
                     f"a bit odd: " + "; ".join(_plain(f) for f in odd[:2]) +
                     (f"; and {len(odd) - 2} more" if len(odd) > 2 else "") + ".")
    if fixable:
        total = len(danger) + len(odd)
        if len(fixable) == 1:
            ask = "Should I put it in quarantine? I can always restore it." if total == 1 else \
                f"I can put {_plain(fixable[0]).split(',')[0]} in quarantine, the rest is a system thing. Should I?"
        elif len(fixable) == total:
            ask = f"Should I put {'both' if total == 2 else f'all {total}'} in quarantine? I can always restore them."
        else:
            ask = (f"{len(fixable)} of them are your own files I can put in quarantine; the others are system "
                   "things, the details are in the report. Should I quarantine those " + f"{len(fixable)}?")
        parts.append(ask)
        return " ".join(parts), "quarantine"
    if reinstall:
        parts.append(f"I'd reinstall {', '.join(reinstall)} to get the official version back. Want me to?")
        return " ".join(parts), "reinstall"
    parts.append("I can't remove that by myself, it's a system thing. The details are in the scan report.")
    return " ".join(parts), ""


def offered(rep: dict, ids: list[str] | None = None) -> list[dict]:
    fs = [f for f in rep["findings"] if f["level"] != "note" and f.get("fix") in ("quarantine", "disable_unit",
                                                                                  "restore_discord")]
    fs = [f for f in fs if Path(f["path"]).exists()]  # already gone (deleted by hand since the scan)
    return [f for f in fs if f["id"] in ids] if ids else fs


# ── the daemon's handle ─────────────────────────────────────────────────────
class Scanner:
    """One scan at a time, in the background. `on_done(text)` is how she tells the user."""

    def __init__(self, cfg: dict, on_done: Callable[[str], Awaitable[None]] | None = None,
                 paused: Callable[[], bool] = lambda: False):
        self.cfg, self.on_done, self.paused = cfg, on_done, paused
        self.job: ScanJob | None = None
        self.task: asyncio.Task | None = None
        self.offer: dict | None = None  # {"kind": quarantine|reinstall|install_clamav, "t": …}: what "yes" means

    def offering(self, minutes: float = 30) -> str:
        if self.offer and time.time() - self.offer["t"] < minutes * 60:
            return self.offer["kind"]
        return ""

    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    def clam_ready(self) -> bool:
        return Clam(self.cfg).available()

    def start(self, quick: bool = False) -> str:
        if self.running():
            return "a scan is already running: " + self.job.status()
        self.job = ScanJob(self.cfg, quick, self.paused)
        self.task = asyncio.create_task(self._run())
        return "started"

    async def _run(self):
        try:
            rep = await self.job.run()
            text, offer = summary(rep)
            self.offer = {"kind": offer, "t": time.time()} if offer else None
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("scan failed")
            text = f"Sorry, the virus scan stopped with an error: {e}"
        if self.on_done:
            await self.on_done(text)

    def stop(self) -> str:
        if not self.running():
            return "no scan is running"
        self.job.cancelled = True
        self.task.cancel()
        return "stopped the scan"

    def status(self) -> str:
        if self.running():
            return self.job.status()
        rep = latest()
        return f"no scan running; the last one was {rep['t'].replace('T', ' at ')[:19]}" if rep else "no scan yet"


CLAMAV_INSTALL = ("sudo pacman -S --needed clamav && sudo freshclam && "
                  "sudo systemctl enable --now clamav-freshclam-once.timer")


def register(box):
    """Tools for the local model and Claude. The fast path (brain/scanpath.py) handles the usual phrasings."""
    S = {"type": "string"}

    @box._add("virus_scan", "Virus / malware scan of the whole PC (ClamAV signatures, YARA rules, system "
              "integrity, autostart, running programs). It runs in the background and Mio announces the result. "
              "action: start (deep, default for 'check my pc for viruses'), quick, status (progress), stop, "
              "results (what the last scan found, numbered), restore (put a quarantined file back; items = part "
              "of its name or 'last'), trust (items = numbers the user says are fine, never warned about again). "
              "Removing things is the quarantine tool.",
              {"action": {"type": "string", "enum": ["start", "quick", "status", "stop", "results", "restore",
                                                     "trust"]}, "items": S}, ["action"])
    async def virus_scan(action: str, items: str = "") -> str:
        sc_ = box.scanner
        if sc_ is None:
            return "the scanner isn't available"
        if action in ("start", "quick"):
            if not sc_.clam_ready() and action == "start" and items != "without clamav":
                sc_.offer = {"kind": "install_clamav", "t": time.time()}
                return ("ClamAV (the virus signature scanner) isn't installed. Ask the user if you should install "
                        "it; without it the scan still checks everything else.")
            r = sc_.start(quick=action == "quick")
            return ("started the scan in the background; you'll announce the result when it's done"
                    if r == "started" else r)
        if action == "status":
            return sc_.status()
        if action == "stop":
            return sc_.stop()
        rep = latest()
        if action == "restore":
            return restore(items or "last")
        if not rep:
            return "there's no scan yet"
        if action == "trust":
            ids = re.findall(r"\d+", items)
            fs = offered(rep, ids) if ids else [f for f in rep["findings"] if f["level"] != "note"]
            for f in fs:
                add_trust(f["path"], f.get("sha256", ""))
            sc_.offer = None
            return f"trusted {len(fs)}: " + ", ".join(_short(f["path"]) for f in fs) if fs else "nothing to trust"
        text, _ = summary(rep)
        listed = [f"#{f['id']} [{f['level']}] {_short(f['path'])}: {'; '.join(f['evidence'][:2])}"
                  for f in rep["findings"] if f["id"]]
        return f"scan of {rep['t'][:16]}: {text}\n" + "\n".join(listed) + f"\nfull report: {rep.get('report_md', '')}"

    @box._add("quarantine", "Remove what the last virus scan found by moving it into Mio's quarantine (locked, "
              "can be restored; nothing is deleted). items = finding numbers like '1,3', or 'all'.",
              {"items": S}, [], risk="confirm")
    async def quarantine_(items: str = "all") -> str:
        rep = latest()
        if not rep:
            return "there's no scan to act on"
        ids = re.findall(r"\d+", items or "")
        fs = offered(rep, ids or None)
        if not fs:
            return "nothing from the last scan can be quarantined"
        done = await quarantine(fs)
        update_latest(rep)
        if box.scanner:
            box.scanner.offer = None
        return "; ".join(done)


# ── tests: scan a fake tree ─────────────────────────────────────────────────
def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--root", default="/")
    ap.add_argument("--qkk", help="file with pacman -Qkk output to use instead of running it")
    ap.add_argument("--owned", help="file with 'pkg /path' lines (pacman -Ql) for the fake root")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-clamav", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    owned = None
    if a.owned:
        owned = {p: k for k, _, p in (l.partition(" ") for l in Path(a.owned).read_text().splitlines()) if p}
    ctx = Ctx(home=Path(a.home), root=a.root, owned=owned)
    global SCANS_DIR
    SCANS_DIR = Path(a.home) / ".mio-scan-results"  # keep the real scan history clean
    job = ScanJob({}, a.quick, ctx=ctx, qkk=Path(a.qkk).read_text() if a.qkk else None,
                  roots=[Path(a.home)] + ([Path(a.root)] if a.root != "/" else []), use_clam=not a.no_clamav)
    rep = asyncio.run(job.run())
    print(json.dumps({"summary": summary(rep)[0], "findings": [(f["level"], f["kind"], f["path"], f["evidence"])
                                                               for f in rep["findings"]],
                      "inventory": [i["path"] for i in rep["inventory"]], "files": rep["files"]}, indent=1))


if __name__ == "__main__":
    _main()
