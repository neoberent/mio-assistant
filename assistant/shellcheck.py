"""Can this shell command run without asking? The allow-list only names programs, and many of them can also
start other programs, write files or hand out secrets depending on their arguments. So commands are parsed the way
bash reads them, and anything that can't be shown to be a plain look gets the user's yes first."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

JOINERS = {"|", "||", "&&", ";"}
PUNCTUATION = set(";&|<>()")

# names under home whose contents are nobody's business but the user's
SECRETS = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass",
           ".password-store", ".claude", ".zen", ".mozilla", ".config/BraveSoftware", ".config/chromium",
           ".config/google-chrome", ".config/gh", ".config/rclone", ".config/assistant/env", ".local/share/keyrings",
           ".local/state/assistant/riff-token")
# these print what's inside the files they are given
READERS = {"cat", "head", "tail", "grep", "sort", "uniq", "cut"}

GIT_LOOKS = {"status", "log", "show", "diff", "blame", "shortlog", "ls-files", "rev-parse", "describe", "grep"}
GIT_BAD_ARG = re.compile(r"^(--output|--ext-diff|--textconv|--open-files-in-pager|-O)")
SYSTEMCTL_LOOKS = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "list-unit-files",
                   "show", "cat"}
FLATPAK_LOOKS = {"list", "info", "search", "history", "remotes", "remote-ls", "ps"}
PACTL_LOOKS = {"list", "info", "stat", "get-default-sink", "get-default-source", "get-sink-volume", "get-sink-mute",
               "get-source-volume", "get-source-mute"}
NIRI_BAD_ACTION = {"spawn", "spawn-sh", "quit", "load-config-file"}
FIND_BAD = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
# query only: -Q…, -S with search/info/group/list, never a sync or an upgrade
PACKAGE_LOOK = re.compile(r"-(?:Q[A-Za-z]*|S[sigl]+[q]?|V|h)")
JOURNAL_CHANGES = ("--vacuum", "--rotate", "--flush", "--relinquish", "--smart-relinquish", "--setup-keys",
                   "--update-catalog")


def _commands(cmd: str) -> list[list[str]] | None:
    """The simple commands in a line, as bash would see their words. None when it does more than run them in a row
    (redirects, subshells, background jobs, command substitution, several lines) or can't be read at all."""
    cmd = cmd.strip()
    if "\n" in cmd or "\r" in cmd or _substitutes(cmd):
        return None
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="".join(PUNCTUATION))
    lex.whitespace_split, lex.commenters = True, ""
    try:
        tokens = list(lex)
    except ValueError:  # unbalanced quotes
        return None
    out, words = [], []
    for t in tokens:
        if t in JOINERS:
            out.append(words)
            words = []
        elif t and set(t) <= PUNCTUATION:
            return None
        else:
            words.append(t)
    out.append(words)
    return [w for w in out if w]


def _substitutes(cmd: str) -> bool:
    """$(…) or `…` anywhere but inside single quotes (double quotes don't stop them)."""
    single = escaped = False
    for a, b in zip(cmd, cmd[1:] + " "):
        if escaped:
            escaped = False
        elif single:
            single = a != "'"
        elif a == "\\":
            escaped = True
        elif a == "'":
            single = True
        elif a == "`" or (a == "$" and b == "("):
            return True
    return False


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _exposes_secret(word: str, recursive: bool) -> bool:
    """Could this argument of a reader reach a secret? Globs count as everything they might match below their fixed
    start, and so does a recursive search of a folder above a secret."""
    word = word.partition("=")[2] if word.startswith("-") else word
    if not word or (word.startswith("-")):
        return False
    home = Path.home()
    word = re.sub(r"^(\$HOME|\$\{HOME\})(?=/|$)", "~", word)
    if "$" in word:
        return True  # can't tell where a variable points
    glob = re.search(r"[*?\[]", word)
    fixed = os.path.dirname(word[: glob.start()]) if glob else word
    path = Path(os.path.expanduser(fixed or "."))
    path = (path if path.is_absolute() else home / path).resolve()
    for name in SECRETS:
        secret = (home / name).resolve()
        if _inside(path, secret) or ((glob or recursive) and _inside(secret, path)):
            return True
    return False


def _reader_ok(prog: str, args: list[str]) -> bool:
    recursive = prog == "grep" and any(re.match(r"^(-[a-zA-Z]*[rR]|--recursive|--dereference-recursive)", a) for a in args)
    if recursive and not any(_is_path_like(a) for a in args):
        args = [*args, "."]  # no folder named: it searches where it runs, her shell runs in home
    return not any(_exposes_secret(a, recursive) for a in args)


def _is_path_like(word: str) -> bool:
    return not word.startswith("-") and ("/" in word or word.startswith((".", "~", "$")))


def _git_ok(args: list[str]) -> bool:
    rest = list(args)
    while rest and rest[0].startswith("-"):
        if rest[0] == "-C" and len(rest) > 1:
            rest = rest[2:]
        elif rest[0] == "--no-pager":
            rest = rest[1:]
        else:
            return False  # -c, --exec-path, --git-dir… can make git run something else
    if not rest:
        return True
    verb, more = rest[0], rest[1:]
    if any(GIT_BAD_ARG.match(a) for a in more):
        return False
    if verb in GIT_LOOKS:
        return True
    if verb == "branch":
        return all(a in ("-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes") for a in more)
    if verb == "remote":
        return all(a in ("-v", "--verbose") for a in more)
    if verb == "tag":
        return all(a in ("-l", "--list") for a in more)
    return verb == "stash" and more == ["list"]


def _niri_ok(args: list[str]) -> bool:
    return args[:1] == ["msg"] and not NIRI_BAD_ACTION.intersection(args)


def _first_word(args: list[str]) -> str:
    return next((a for a in args if not a.startswith("-")), "")


def _systemctl_ok(args: list[str]) -> bool:
    verb = _first_word(args)
    return not verb or verb in SYSTEMCTL_LOOKS


def _package_ok(args: list[str]) -> bool:
    flags = [a for a in args if a.startswith("-")]
    return bool(flags) and all(PACKAGE_LOOK.fullmatch(a) for a in flags)


def _sort_ok(args: list[str]) -> bool:
    return not any(re.match(r"^(-[a-zA-Z]*o|--o|--com)", a) for a in args)


def _uniq_ok(args: list[str]) -> bool:
    return sum(not a.startswith("-") for a in args) < 2  # the second name is a file it writes


RULES = {
    "git": _git_ok,
    "niri": _niri_ok,
    "systemctl": _systemctl_ok,
    "pacman": _package_ok, "paru": _package_ok, "yay": _package_ok,
    "sort": _sort_ok,
    "uniq": _uniq_ok,
    "find": lambda a: not FIND_BAD.intersection(a),
    "flatpak": lambda a: _first_word(a) in FLATPAK_LOOKS or a == ["--version"],
    "pactl": lambda a: _first_word(a) in PACTL_LOOKS,
    "wl-paste": lambda a: not any(re.match(r"^(-[a-zA-Z]*w|--watch)", x) for x in a),
    "nvidia-smi": lambda a: not any(re.match(r"^(-f|--filename)", x) for x in a),
    "journalctl": lambda a: not any(x.startswith(JOURNAL_CHANGES) for x in a),
    "timedatectl": lambda a: not any(x.startswith("set-") for x in a),
    "hostnamectl": lambda a: not any(x.startswith("set-") or x == "hostname" for x in a),
    "xdg-open": lambda _a: False,  # a link can carry data away; the `open` tool is for opening things
}


def plain_look(cmd: str, allowed: set[str]) -> bool:
    """True only if every command in the line is an allowed program used harmlessly."""
    commands = _commands(cmd)
    if not commands:
        return False
    for prog, *args in commands:
        if prog not in allowed or not RULES.get(prog, lambda _a: True)(args):
            return False
        if prog in READERS and not _reader_ok(prog, args):
            return False
    return True
