"""Read the focused terminal and judge whether a package install command looks safe.

Terminal text comes from kitty's remote control (read-only password, see docs/README.md),
falling back to the clipboard, which usually still holds what was just pasted.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
from pathlib import Path

import httpx

KITTY_CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kitty" / "kitty.conf"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))

INSTALL_RE = re.compile(
    r"(?:^|[\s;&|$#>])(?:sudo\s+)?(paru|yay|pikaur|trizen|aura|pacman)\s+(-S\w*|-U\w*)\s+([^\n;&|]+)")
CLONE_RE = re.compile(r"git\s+clone\s+(?:\S+\s+)*https?://aur\.archlinux\.org/([\w@.+-]+?)(?:\.git)?(?:\s|$)")
PIPE_SH_RE = re.compile(r"(curl|wget)\s[^|\n]*\|\s*(sudo\s+)?(ba|z|fi)?sh\b")

# (pattern, why it matters) — checked against PKGBUILD and .install files
RED_FLAGS = [
    (r"(curl|wget)\s[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh", "downloads and runs a script at build/install time"),
    (r"base64\s+(-d|--decode)", "decodes hidden base64 data"),
    (r"\beval\b", "uses eval (can hide what actually runs)"),
    (r"python[0-9.]*\s+-c\s+['\"][^'\"]*(exec|urllib|socket|base64)", "runs inline Python that fetches or executes code"),
    (r"discord(app)?\.com/api/webhooks", "contains a Discord webhook (common for stealing data)"),
    (r"(pastebin\.com|transfer\.sh|0x0\.st|termbin\.com|hastebin|ngrok)", "fetches from a paste/file-drop site"),
    (r"https?://\d{1,3}(\.\d{1,3}){3}", "downloads from a raw IP address"),
    (r"\b(nc|ncat|socat)\s+-", "opens network connections with netcat/socat"),
    (r"chmod\s+[ugo]*\+?[0-7]*s|chmod\s+[24][0-7]{3}", "sets setuid/setgid bits"),
    (r"(\.bashrc|\.zshrc|\.profile|/etc/profile|config\.fish|/etc/ld\.so\.preload)", "modifies shell startup or loader files"),
    (r"systemctl\s+(--user\s+)?enable", "enables a service automatically"),
    (r"crontab|/etc/cron", "installs a cron job"),
    (r"\.ssh/|id_rsa|id_ed25519|\.mozilla|\.config/(BraveSoftware|google-chrome|chromium)", "touches SSH keys or browser data"),
    (r"[A-Za-z0-9+/]{200,}={0,2}", "contains a long encoded blob"),
]


def _kitty_password() -> str | None:
    try:
        m = re.search(r'^\s*remote_control_password\s+"([^"]+)"\s+.*\bget-text\b', KITTY_CONF.read_text(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


async def read_terminal() -> tuple[str, str]:
    """Returns (text, source)."""
    from .tools import sh

    code, out = await sh("niri", "msg", "-j", "focused-window", limit=None)
    win = json.loads(out) if code == 0 and out.startswith("{") else {}
    if win.get("app_id") == "kitty" and win.get("pid"):
        sock = RUNTIME / f"kitty-{win['pid']}"
        pw = _kitty_password()
        if sock.exists() and pw:
            code, text = await sh("kitty", "@", "--to", f"unix:{sock}", "--password", pw, "get-text",
                                  "--match", "state:focused", "--extent", "screen", limit=None)
            if code == 0 and text.strip():
                return "\n".join(l for l in text.splitlines() if l.strip())[-4000:], "kitty"
    code, clip = await sh("wl-paste", "--no-newline", limit=None)
    if code == 0 and clip.strip():
        return clip[-4000:], "clipboard"
    return "", ""


def _packages(text: str) -> tuple[str, list[str], str | None]:
    """Finds the last install command. Returns (command, packages, kind)."""
    last = None
    for m in INSTALL_RE.finditer(text):
        last = m
    clone = None
    for m in CLONE_RE.finditer(text):
        clone = m
    if clone and (not last or clone.start() > last.start()):
        return clone.group(0).strip(), [clone.group(1)], "aur"
    if not last:
        return "", [], None
    tool, flags, rest = last.groups()
    try:
        words = shlex.split(rest)
    except ValueError:
        words = rest.split()
    pkgs = [w.split("/")[-1] for w in words if not w.startswith("-") and re.fullmatch(r"[\w@.+/-]+", w)]
    if "U" in flags:
        return last.group(0).strip(), pkgs, "file"
    return last.group(0).strip(), pkgs[:6], "aur" if tool != "pacman" else "repo"


async def _scan_aur(client: httpx.AsyncClient, info: dict) -> list[str]:
    base = info.get("PackageBase") or info["Name"]
    url = "https://aur.archlinux.org/cgit/aur.git/plain/{}?h=" + base
    pkgbuild = (await client.get(url.format("PKGBUILD"))).text
    files = {"PKGBUILD": pkgbuild}
    if m := re.search(r"^\s*install=['\"]?([\w.+-]+)", pkgbuild, re.M):
        files[m.group(1)] = (await client.get(url.format(m.group(1)))).text
    hits = []
    for fname, body in files.items():
        for pat, why in RED_FLAGS:
            if m := re.search(pat, body, re.I):
                snippet = body[max(0, m.start() - 30): m.end() + 30].replace("\n", " ").strip()
                hits.append(f"{fname} {why}: …{snippet[:110]}…")
    hosts = sorted({h for h in re.findall(r"https?://([\w.-]+)", pkgbuild)} - {"aur.archlinux.org"})
    if hosts:
        hits.append("downloads from: " + ", ".join(hosts[:6]))
    return hits


async def assess_install(text: str, src: str) -> str:
    from .tools import sh

    if m := PIPE_SH_RE.search(text):
        return (f"HIGH RISK: the {'terminal' if src == 'kitty' else src} contains '{m.group(0)}', which downloads a script and runs it "
                "immediately. Only do that if you fully trust the site; better to download and read it first.")
    cmd, pkgs, kind = _packages(text)
    if not pkgs:
        return f"I didn't find a package install command in the {'terminal' if src == 'kitty' else src}. Last lines were:\n" + text[-400:]
    if kind == "file":
        return (f"'{cmd}' installs a local package file, which pacman can't verify against a repository. "
                "Only install it if you built it yourself or trust where it came from.")

    report, worst = [], "LOW"
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for name in pkgs:
            code, repo = await sh("pacman", "-Si", name)
            if code == 0:
                r = re.search(r"^Repository\s*:\s*(\S+)", repo, re.M)
                report.append(f"{name}: official repo package ({r.group(1) if r else 'repo'}), signed by Arch/CachyOS "
                              "developers. Low risk.")
                continue
            res = (await client.get("https://aur.archlinux.org/rpc/v5/info", params={"arg[]": name})).json()
            if not res.get("results"):
                report.append(f"{name}: not in the repos or the AUR. Check the spelling; typo-squatted names are a "
                              "common trick.")
                worst = max(worst, "MEDIUM", key=rank.get)
                continue
            p = res["results"][0]
            now = dt.datetime.now().timestamp()
            age_days = (now - p["FirstSubmitted"]) / 86400
            changed_days = (now - p["LastModified"]) / 86400
            flags = []
            if not p.get("Maintainer"):
                flags.append("orphaned (no maintainer)")
            if p["NumVotes"] < 10:
                flags.append(f"only {p['NumVotes']} votes")
            if age_days < 30:
                flags.append(f"brand new ({int(age_days)} days old)")
            if changed_days < 3 and p["NumVotes"] < 50:
                flags.append("changed in the last few days")
            if p.get("OutOfDate"):
                flags.append("flagged out of date")
            if name.endswith("-bin"):
                flags.append("prebuilt binary (you trust upstream's build)")
            scan = await _scan_aur(client, p)
            red = [s for s in scan if not s.startswith("downloads from")]
            level = "HIGH" if red else ("MEDIUM" if [f for f in flags if "binary" not in f] else "LOW")
            worst = max(worst, level, key=rank.get)
            report.append(f"{name} (AUR): {level} risk. Maintainer {p.get('Maintainer') or 'none'}, {p['NumVotes']} votes, "
                          f"updated {int(changed_days)} days ago. " + ("Concerns: " + "; ".join(flags) + ". " if flags else "")
                          + ("PKGBUILD red flags: " + " | ".join(red) + ". " if red else "No PKGBUILD red flags. ")
                          + next((s + "." for s in scan if s.startswith("downloads from")), ""))
    return (f"Command ({src}): {cmd}\nOverall: {worst} risk.\n" + "\n".join(report) +
            "\nNote: automated checks can't prove a package is safe; for AUR packages, read the PKGBUILD diff "
            "paru shows you before confirming.")
