"""Fast path for the virus scan: "check my pc for viruses", "how's the scan going", and the answers to her own
questions ("Should I put it in quarantine?" → "yes"). Her offer lives on the scanner, not in session memory,
so a "yes" still works after she hid in between.
"""
from __future__ import annotations

import re

from .. import virusscan as vs

THREAT = re.compile(r"\b(virus(?:es)?|malware|trojans?|spyware|rootkits?|keyloggers?|ransomware|"
                    r"anti-?virus|virus ?scan)\b|\b(?:is|am) (?:my (?:pc|computer|system|laptop)|i) "
                    r"(?:infected|hacked|compromised)\b|\b(?:got|been) hacked\b|"
                    r"\b(?:something|anything) (?:bad|malicious|suspicious|dangerous|sketchy|shady|evil)\b.{0,30}"
                    r"\b(?:on|in) (?:my|this) (?:pc|computer|system|laptop)\b|\bspying on me\b")
SCAN_WORD = re.compile(r"\b(?:the|your|a|that|virus|malware) scan\b|\bscanning\b")
STATUS = re.compile(r"\b(how'?s|how is|status|progress|still|done yet|finished yet|how long|how far|going)\b")
STOP = re.compile(r"\b(stop|cancel|abort|end)\b")
RESULTS = re.compile(r"\b(what did (?:you|it|the scan) find|results?|what (?:was|is) (?:found|wrong)|"
                     r"what did you see|tell me (?:again|more)|which (?:ones?|files?))\b")
REPORT = re.compile(r"\b(open|show)\b.*\breport\b")
RESTORE = re.compile(r"\b(restore|bring back|put back|un-?quarantine)\b")
YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|do it|go ahead|go for it|of course|absolutely)"
                 r"(?:[ ,]+(?:please|do it|go ahead|thanks?|thank you|sure|yes))*$|"
                 r"^(?:(?:yes|yeah|ok(?:ay)?),? )?(?:please )?(?:quarantine|remove|get rid of|delete|kill|disable|"
                 r"install|reinstall|clean) (?:it|them|those|that|all of them|everything|it all|clamav)(?: please)?$")
NO = re.compile(r"^(?:no|nope|nah|don'?t|leave (?:it|them)|keep (?:it|them)|not now|no thanks?|never ?mind)\b")
TRUST = re.compile(r"\b(?:it'?s|they'?re|that'?s|those are|this is) (?:fine|safe|mine|okay|ok|legit|normal)\b|"
                   r"\btrust (?:it|them|that|those)\b|\bi know (?:it|that|them|what (?:it|that) is)\b")
# a scan is for THEIR pc: "check for viruses", "does my pc have a virus" — not "what is a virus"
INTENT = re.compile(r"\b(check|scan|look(?:ing)? for|search for|find|hunt|detect|do i have|have i got|got|is there|"
                    r"are there|any|infected|hacked|compromised|clean|run|start|do)\b")
MYPC = re.compile(r"\bmy (?:pc|computer|system|laptop|machine|files|linux|desktop)\b|\bthis (?:pc|computer)\b|"
                  r"\b(?:am|is) i\b|\bi (?:have|got)\b|\bspying on me\b")
NOT_A_SCAN = re.compile(r"^(?:play|put on|listen|queue|shuffle|open|launch|search|google|look up|youtube|watch)\b")
GENERAL = re.compile(r"^(?:what|how|why|who|when|where|explain|tell me about|define)\b")
ANYWAY = re.compile(r"\b(?:scan )?(?:anyway|without it|without clamav)\b|^(?:no|nope|nah)\b")


async def handle(t: str, tools):
    scanner = tools.scanner
    if scanner is None:
        return None
    r = await _handle(t, tools, scanner)
    if r is None and scanner.offering():
        scanner.offer = None  # they moved on: a later "yes"/"no" belongs to something else
    return r


async def _handle(t: str, tools, scanner):
    from .fastpath import Result

    offer = scanner.offering()
    words = len(t.split())

    # answers to her question
    if offer and words <= 8:
        if offer == "install_clamav":
            if YES.search(t) and not ANYWAY.search(t):
                scanner.offer = None
                r = await tools.call_confirmed("run_shell", {"command": vs.CLAMAV_INSTALL})
                return Result("Opening a terminal to install ClamAV. Type your password there, and when it's done "
                              "just ask me again to check for viruses!" if "Opened" in r else
                              "Sorry, I couldn't open the terminal.")
            if ANYWAY.search(t):
                scanner.offer = None
                return Result(_started(await tools.call("virus_scan", {"action": "start",
                                                                       "items": "without clamav"}), False))
        if offer in ("quarantine", "reinstall"):
            if TRUST.search(t):
                r = await tools.call("virus_scan", {"action": "trust"})
                return Result("Okay, I trust " + ("it" if r.startswith("trusted 1:") else "them") +
                              " now and won't warn you about it again." if r.startswith("trusted") else
                              "There was nothing to trust.")
            if NO.search(t):
                scanner.offer = None
                return Result("Okay, I'll leave it alone. If you know it and it's fine, say \"it's safe\" and I "
                              "won't warn you about it again.")
            if YES.search(t):
                if offer == "reinstall":
                    rep = vs.latest() or {}
                    pkgs = sorted({f["package"] for f in rep.get("findings", []) if f.get("fix") == "reinstall"})
                    scanner.offer = None
                    r = await tools.call("install_packages", {"packages": pkgs})
                    return Result(f"Opening a terminal to reinstall {', '.join(pkgs)}. Confirm there and type your "
                                  "password!" if r.startswith("Opened") else r)
                r = await tools.call_confirmed("quarantine", {"items": "all"})
                if r.startswith("nothing from the last scan"):
                    scanner.offer = None
                    return Result("Those files are already gone, so there's nothing left to quarantine.")
                n = r.count("quarantined ")
                left = r.count("left ")
                if "repaired Discord" in r and not n:
                    return Result("Done, I repaired Discord's core module. Restart Discord so the clean version "
                                  "loads. The changed file is in quarantine, if you ever need it back.")
                if n:
                    return Result(f"Done, {'it is' if n == 1 else f'all {n} are'} in quarantine now and can't do "
                                  "anything. If something stops working, say \"restore it\"." +
                                  (f" {left} I left alone, they aren't your own files." if left else ""))
                return Result("I couldn't quarantine that: " + r)

    threat = THREAT.search(t)
    about_scan = threat or SCAN_WORD.search(t)
    if not about_scan:
        if RESTORE.search(t) and re.search(r"\bquarantine", t):
            return Result(_cap(await tools.call("virus_scan", {"action": "restore", "items": _name(t)})) + ".")
        return None
    if REPORT.search(t):
        rep = vs.latest()
        if not rep or not rep.get("report_md"):
            return Result("There's no scan report yet.")
        await tools.call("open", {"target": rep["report_md"]})
        return Result("Here's the report of the last scan.")
    if RESTORE.search(t):
        return Result(_cap(await tools.call("virus_scan", {"action": "restore", "items": _name(t)})) + ".")
    if STOP.search(t) and not re.search(r"\bstop (?:a |the )?(?:virus|malware)(?!\w*\s+(?:scan|check))", t):
        r = await tools.call("virus_scan", {"action": "stop"})
        return Result("Okay, I stopped the scan." if r.startswith("stopped") else "There's no scan running.")
    if STATUS.search(t) and scanner.running():
        return Result(_cap(scanner.status()) + ".")
    if RESULTS.search(t) or (STATUS.search(t) and not scanner.running() and not threat):
        rep = vs.latest()
        if not rep:
            return Result("I haven't scanned your PC yet. Want me to? Just say \"check my PC for viruses\".")
        text, offer = vs.summary(rep)
        scanner.offer = {"kind": offer, "t": __import__("time").time()} if offer else None
        return Result(f"From the scan on {_when(rep['t'])}: {text}")
    if not threat or NOT_A_SCAN.match(t) or not (MYPC.search(t) or (INTENT.search(t) and not GENERAL.match(t))):
        return None  # "what is a virus?" is a question, not a scan
    if scanner.running():
        return Result("I'm already scanning: " + scanner.status() + ".")
    quick = bool(re.search(r"\b(quick(?:ly)?|fast|short)\b", t))
    r = await tools.call("virus_scan", {"action": "quick" if quick else "start"})
    if r.startswith("ClamAV"):
        return Result("For a real deep scan I need ClamAV, the virus scanner, and it isn't installed. Want me to "
                      "install it? Or say \"scan anyway\" and I'll check everything else without it.")
    return Result(_started(r, quick))


def _started(r: str, quick: bool) -> str:
    if not r.startswith("started"):
        return _cap(r) + "."
    if quick:
        return "Okay, quick virus check! I'll look where malware usually hides: downloads, temp folders, hidden " \
               "folders, browser extensions, Discord, and what starts on its own. It takes about two minutes."
    return "Okay, I'm starting a deep scan of your whole PC. That takes a while, and it runs quietly in the " \
           "background. I'll tell you what I find when I'm done!"


def _name(t: str) -> str:
    m = re.search(r"\b(?:restore|bring back|put back)\s+(?:the\s+)?(.+?)(?:\s+from (?:the )?quarantine)?$", t)
    name = (m[1] if m else "").strip()
    return "last" if not name or re.fullmatch(r"(it|that|this|them|the file|file|last one|the last one)", name) \
        else ("all" if re.fullmatch(r"(all|everything|all of them|them all)", name) else name)


def _when(t: str) -> str:
    import datetime as dt

    d = dt.datetime.fromisoformat(t)
    today = dt.date.today()
    day = "today" if d.date() == today else "yesterday" if (today - d.date()).days == 1 else d.strftime("%B %-d")
    return f"{day} at {d.strftime('%-H:%M')}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s
