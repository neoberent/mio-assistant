"""Teacher data for Mio's dreams: Claude writes realistic requests with the right tool calls, every one is
checked here, and her own mistakes on them become "don't do this" examples.

    .venv/bin/python scripts/distill.py gen [--rounds 3] [--per 40] [--model sonnet] [--jobs 4] [--only music,…]
    .venv/bin/python scripts/distill.py check          validate teacher-raw.jsonl → teacher.jsonl (+ stats)
    .venv/bin/python scripts/distill.py mistakes       run her current brain on them → pairs.jsonl (stops nothing)
    .venv/bin/python scripts/distill.py exam           build the fixed exam set (exam.jsonl)
    .venv/bin/python scripts/distill.py cost           what generating has cost so far

Claude is only the teacher here (one-off, `claude -p`, no tools); the dream trains on its answers locally and she
keeps running on her own model. Files live in the dream folder (~/.local/share/assistant/dream).
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import config  # noqa: E402
from assistant.brain.local import EXAMPLES, HIDDEN, HINT, ESCALATE, LocalBrain, pick_tools  # noqa: E402
from assistant.dream import HOME  # noqa: E402
from assistant.tools import ToolBox  # noqa: E402

RAW = HOME / "teacher-raw.jsonl"
TEACHER = HOME / "teacher.jsonl"
PAIRS = HOME / "pairs.jsonl"
EXAM = HOME / "exam.jsonl"
COST = HOME / "teacher-cost.json"
TESTS = Path(__file__).resolve().parent.parent / "tests"

# what to practise: (category, what the requests are about)
TOPICS = {
    "music": "songs, artists, playlists, liked songs, shuffle, skip/like/what's playing — all in Riff, her music app",
    "youtube": "playing or searching YouTube videos and channels, what's this video about, seek/speed in the video",
    "browser": "the open browser page and tabs: summarize, read, click, scroll, close/switch tabs, history",
    "apps": "opening/closing/focusing apps and windows, workspaces, fullscreen, niri window actions",
    "games": "launching Roblox or Steam games, patch notes, prices and sales, clipping gameplay",
    "discord": "opening DMs/servers in Discord, sending a friend a message with the exact words",
    "timers": "timers, reminders at a clock time or in N minutes, what timers are running, cancelling",
    "system": "RAM/CPU/GPU numbers, temperatures, what's using memory, disk space, uptime, health problems",
    "shell": "things only a shell command answers: IP, package count, kernel, processes, services, killing apps",
    "packages": "updates, installing packages, is a package safe, package info, Arch/CachyOS news",
    "files": "finding files by name/kind/date, what's in a folder, opening folders, tidying downloads, trash",
    "desktop": "night light, do not disturb, wifi, bluetooth status/toggle, wallpaper, Noctalia panels",
    "web": "current facts and news, who/what questions about the world, opening a best-match site, site searches",
    "knowledge": "how-tos for Arch (ArchWiki), Wikipedia topics, the user's own config/keybinds (search_my_setup)",
    "screen": "what's on screen, this error in the terminal, what game/picture is this, clipboard",
    "phone": "find/ring the phone, phone battery, send a link or text to the phone",
    "memory": "the user telling her facts to remember about themselves, routines ('when I say X do …')",
    "security": "virus scans, scan results, quarantining found items",
    "keys": "pressing shortcuts in apps, typing text into the focused field, new/close terminal tabs",
    "volume": "volume up/down/set/mute, pausing/resuming media, 'too loud'",
    "chat": "pure small talk, thanks, compliments, feelings, 'good night', jokes — no tool at all",
    "ask": "requests that change or close something but could mean two things and nothing says which — "
           "she asks one short either/or question instead of guessing (no tool)",
    "multi": "one request with 2-3 independent parts that each need a tool (all calls at once)",
}

TEACHER_SYSTEM = """You write training data for Mio, a small local voice assistant on the user's Linux desktop \
(CachyOS/Arch, niri, Noctalia shell, Zen browser, Riff music app, Discord, Steam, Roblox via Froststrap/Sober, \
kitty terminal, fish shell, iPhone via KDE Connect). For each request you write, give the exact tool calls the best \
assistant would make with ONLY these tools. Output JSON Lines only — one object per line, no prose, no code fences."""

TEACHER_TASK = """Her tools (name: description; parameters, * = required):
{tools}

House rules she follows (the examples show the right tool for common requests — follow them exactly):
{rules}

People and names in the user's world (use them naturally, sometimes misspelled the way speech recognition would):
{names}

Write {n} different requests for the topic: **{topic}** — {about}
Mix: about half spoken (voice transcript: lowercase, little punctuation, "um", run-ons, sometimes a misheard word), \
a quarter typed casually with typos/slang, a quarter clean. Vary length and wording a lot; don't reuse the house-rule \
examples word for word. {extra}

Each line: {{"say": "<what the user said>", "calls": [{{"name": "<tool>", "args": {{…}}}}], "reply": "<only when calls \
is empty: her short spoken answer>", "now": "<optional: the 'Right now' desktop state the request relies on>"}}
Rules for the calls:
- Only the tools above, only their parameters, valid enum values. Fill arguments from the user's own words (fix \
obvious mishearings/typos of names); never invent places, URLs, people or values they didn't say.
- A question only checks (never toggles); "play X" songs → riff, YouTube only when they say YouTube/video.
- Requests about "this/it/the song/the video/the game" need a "now" field, e.g. \
"Right now — focused window: zen — YouTube; media playing: Lofi Girl — lofi hip hop radio; game running: Roblox".
- calls = [] only for small talk or an either/or question; then "reply" is 1-2 short casual sentences in her voice \
(cheerful, warm, no emoji). Never let a reply claim she did something.
- Never use {escalate}. Requests to write code/scripts/emails are out of scope — don't write any."""

EXTRA = {
    "chat": "All of these get calls = [] and a warm short reply. Include some that sound like requests but aren't "
            "(\"I don't mind\", \"bravo, love you, cheers\", \"that's cool\").",
    "ask": "All of these get calls = [] and the reply is ONE short either/or question naming both options, e.g. "
           "\"close it\" with a browser and a game open → \"The browser tab or the game window?\". Give each a \"now\" "
           "field that makes both readings possible.",
    "multi": "Each request has 2-3 parts joined by and/then/commas that don't depend on each other's results; "
             "calls lists one call per part, in order.",
}
ROUND_STYLE = ["", "This round: mostly longer, rambling spoken requests with filler words.",
               "This round: mostly very short commands and slang (\"yo mute\", \"pc temps?\").",
               "This round: mostly polite full sentences, and some indirect ones (\"it's way too dark in here\")."]


# ── shared bits ────────────────────────────────────────────────────────────
def toolbox():
    cfg = config.load()

    async def no(*_a, **_k):
        return False
    tb = ToolBox(cfg.security, no, None, cfg.lookup)
    return cfg, tb, LocalBrain(cfg.brain, cfg.persona, tb)


def tool_lines(tb) -> str:
    out = []
    for t in tb.specs():
        if t.name in HIDDEN:
            continue
        s = t.schema()
        req = set(s.get("required", []))
        ps = []
        for k, p in s.get("properties", {}).items():
            typ = p.get("type", "string") + (f" one of {p['enum']}" if "enum" in p else "")
            ps.append(f"{k}{'*' if k in req else ''} ({typ})")
        out.append(f"- {t.name}: {t.description} | {', '.join(ps) or 'no parameters'}")
    return "\n".join(out)


def world_names() -> str:
    try:
        lex = json.loads((config.STATE_DIR / "lexicon.json").read_text())["names"]
    except (OSError, KeyError, json.JSONDecodeError):
        return "(none known)"
    by: dict[str, list[str]] = {}
    for name, v in lex.items():
        by.setdefault(v.get("src", "other"), []).append(name)
    label = {"app": "apps", "game": "games", "youtube": "YouTube channels", "discord": "Discord friends/chats",
             "site": "favourite sites"}
    return "\n".join(f"- {label[k]}: {', '.join(by[k][:20])}" for k in label if by.get(k))


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", str(text).lower()) if len(w) > 2 and w not in STOP]


STOP = {"the", "and", "for", "you", "can", "please", "with", "that", "this", "what", "how", "from", "into", "some",
        "my", "me", "on", "in", "to", "of", "a", "an", "is", "it"}


# ── gen ────────────────────────────────────────────────────────────────────
async def ask_claude(prompt: str, model: str) -> tuple[str, float]:
    env = dict(os.environ, MAX_THINKING_TOKENS="0")
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--model", model, "--output-format", "json", "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--system-prompt", TEACHER_SYSTEM, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=str(HOME))
    out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), 900)
    if proc.returncode:
        raise RuntimeError(err.decode()[-400:] or out.decode()[-400:])
    j = json.loads(out)
    return j.get("result", ""), float(j.get("total_cost_usd") or 0)


def add_cost(usd: float, n: int):
    c = json.loads(COST.read_text()) if COST.exists() else {"usd": 0.0, "calls": 0, "items": 0}
    c["usd"] += usd
    c["calls"] += 1
    c["items"] += n
    COST.write_text(json.dumps(c))


async def gen(rounds: int, per: int, model: str, jobs: int, only: list[str]):
    _, tb, _ = toolbox()
    tools, names = tool_lines(tb), world_names()
    HOME.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(jobs)
    todo = [(r, cat) for r in range(rounds) for cat in TOPICS if not only or cat in only]

    async def one(r: int, cat: str):
        extra = " ".join(x for x in (EXTRA.get(cat, ""), ROUND_STYLE[r % len(ROUND_STYLE)]) if x)
        prompt = TEACHER_TASK.format(tools=tools, rules=f"{HINT}\n{EXAMPLES}", names=names, n=per, topic=cat,
                                     about=TOPICS[cat], extra=extra, escalate=ESCALATE)
        async with sem:
            t = time.time()
            try:
                text, usd = await ask_claude(prompt, model)
            except Exception as e:  # noqa: BLE001
                print(f"✗ {cat} r{r}: {e}", flush=True)
                return
        items = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if line.startswith("{"):
                try:
                    items.append(dict(json.loads(line), cat=cat, round=r))
                except json.JSONDecodeError:
                    pass
        with RAW.open("a") as f:
            f.writelines(json.dumps(i, ensure_ascii=False) + "\n" for i in items)
        add_cost(usd, len(items))
        print(f"✓ {cat} r{r}: {len(items)} items, ${usd:.3f}, {time.time() - t:.0f}s", flush=True)

    await asyncio.gather(*(one(r, c) for r, c in todo))
    cost()


def cost():
    c = json.loads(COST.read_text()) if COST.exists() else {"usd": 0, "calls": 0, "items": 0}
    per = c["usd"] / max(c["items"], 1)
    print(f"teacher so far: {c['items']} items in {c['calls']} calls, ${c['usd']:.2f} (${per * 1000:.2f} per 1000)")


# ── check ──────────────────────────────────────────────────────────────────
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
GROUNDED = {"query", "who", "name", "game", "label", "playlist", "location", "app", "fact", "items"}
EXACT = {("discord", "text"), ("phone", "text"), ("type_text", "text")}
FREE = {"web_research", "wikipedia", "arch_wiki", "search_my_setup", "open_best_match", "routine", "remember"}


def problems(item: dict, specs: dict) -> list[str]:
    say, calls = item.get("say", ""), item.get("calls")
    if not isinstance(say, str) or not say.strip() or not isinstance(calls, list):
        return ["malformed"]
    if not calls and not (item.get("reply") or "").strip():
        return ["no calls and no reply"]
    if not calls and re.search(r"\bI(?:'ve| have)? (?:just )?(?:opened|launched|started|closed|set|turned|sent|"
                               r"played|checked)\b", item["reply"], re.I):
        return ["reply claims an action"]
    if item["cat"] == "ask" and (calls or "?" not in item.get("reply", "")):
        return ["ask item without a question"]
    if item["cat"] == "chat" and calls:
        return ["chat item with calls"]
    out = []
    seen = norm(say + " " + item.get("now", ""))
    for c in calls:
        name, args = c.get("name"), c.get("args") or {}
        if name not in specs or name == ESCALATE:
            out.append(f"unknown tool {name}")
            continue
        if not isinstance(args, dict):
            out.append(f"{name}: args not an object")
            continue
        schema = specs[name]
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in args:
                out.append(f"{name}: missing {k}")
        for k, v in args.items():
            p = props.get(k)
            if p is None:
                out.append(f"{name}: unknown arg {k}")
                continue
            typ = TYPES.get(p.get("type", "string"))
            if typ and not isinstance(v, typ) or (p.get("type") in ("integer", "number") and isinstance(v, bool)):
                out.append(f"{name}.{k}: wrong type {type(v).__name__}")
            if "enum" in p and v not in p["enum"]:
                out.append(f"{name}.{k}: {v!r} not in enum")
            if isinstance(v, str) and (name, k) in EXACT:
                want = words(v)
                have = sum(1 for w in want if w in seen)
                if want and have / len(want) < 0.7:
                    out.append(f"{name}.{k}: not the user's words")
            elif isinstance(v, str) and k in GROUNDED and name not in FREE:
                if not grounded(v, seen):
                    out.append(f"{name}.{k}={v!r} not said")
            elif k == "location" and isinstance(v, str) and not grounded(v, seen):
                out.append(f"location {v!r} not said")
    return out


def grounded(value: str, seen: str) -> bool:
    want = words(value)
    if not want:
        return True
    pool = seen.split() + [seen.replace(" ", "")]
    hits = sum(1 for w in want if w in seen or difflib.get_close_matches(w, pool, n=1, cutoff=0.7))
    return hits >= max(1, len(want) // 2)


def check():
    _, tb, brain = toolbox()
    specs = {t.name: t.schema() for t in tb.specs() if t.name not in HIDDEN}
    quiz = {norm(q["say"]) for f in TESTS.glob("curriculum*.json") for q in json.loads(f.read_text())["quiz"]}
    items = [json.loads(l) for l in RAW.read_text().splitlines() if l.strip()]
    kept, why, seen, routing = [], {}, set(), {}
    for it in items:
        for c in it.get("calls") or []:
            if isinstance(c, dict):
                c["args"] = c.get("args") or {}  # tools without parameters come without args
        p = problems(it, specs)
        n = norm(it.get("say", ""))
        if not p and (n in seen or n in quiz):
            p = ["duplicate or quiz wording"]
        if p:
            why[p[0].split(":")[0] if "unknown" not in p[0] else p[0]] = why.get(p[0].split(":")[0], 0) + 1
            it["problems"] = p
            continue
        seen.add(n)
        offered = pick_tools(it["say"], [])
        missing = [c["name"] for c in it["calls"] if c["name"] not in offered]
        if missing:  # her keyword routing wouldn't even offer the tool: worth fixing in CATEGORIES too
            for m in missing:
                routing.setdefault(m, []).append(it["say"])
        kept.append(it)
    TEACHER.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in kept))
    bad = [i for i in items if i.get("problems")]
    (HOME / "teacher-rejected.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in bad))
    cats: dict[str, int] = {}
    for i in kept:
        cats[i["cat"]] = cats.get(i["cat"], 0) + 1
    print(f"{len(kept)}/{len(items)} kept → {TEACHER}")
    print("by topic:", ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
    print("dropped:", ", ".join(f"{k} {v}" for k, v in sorted(why.items(), key=lambda x: -x[1])) or "none")
    print("tools her routing wouldn't offer:", ", ".join(f"{k} ×{len(v)}" for k, v in
                                                         sorted(routing.items(), key=lambda x: -len(x[1]))) or "none")
    json.dump(routing, (HOME / "routing-gaps.json").open("w"), indent=1, ensure_ascii=False)


from assistant.dream import exam_split, hash_said, row, teacher_exam  # noqa: E402


# ── mistakes: her own wrong answers become rejected examples ──────────────────────────────────────
def same_calls(got: list[dict], want: list[dict]) -> bool:
    """Her calls match the teacher's: same tools in order (doing only the first part now is fine, the rest
    follows in the next turn), same strict values, and wordy values sharing the important words."""
    from assistant.dream import calls_match
    return calls_match(got, want)


SWAPPABLE = [{"wikipedia", "web_research", "arch_wiki"}, {"system_info", "system_health", "run_shell"},
             {"media", "riff"}]
KEY_ARGS = {"action", "mode", "source", "site", "direction", "apply", "inside"}


def real_mistake(chosen: dict, rejected: dict) -> bool:
    """Worth unlearning: talking/pretending instead of acting (or acting on small talk), the wrong tool, or the
    wrong action. Not: another fine value (volume 20 vs 30), another shell command, an extra harmless call."""
    want = [(c["function"]["name"], c["function"]["arguments"]) for c in chosen.get("tool_calls") or []]
    got = [(c["function"]["name"], c["function"].get("arguments") or {}) for c in rejected.get("tool_calls") or []]
    if not want or not got:
        return bool(want) != bool(got)
    (wn, wa), (gn, ga) = want[0], got[0]
    if wn != gn:
        return not any(wn in s and gn in s for s in SWAPPABLE)
    if wn == "run_shell":
        return False
    if any(str(ga.get(k, "")).lower() != str(v).lower() for k, v in wa.items() if k in KEY_ARGS):
        return True
    return wn == "shell_panel" and ga.get("command") != wa.get("command")


async def mistakes(model: str, limit: int):
    import httpx
    from assistant.dream import active_model
    cfg, _, brain = toolbox()
    model = model or active_model(cfg.learning, cfg.brain["ollama_model"]) or cfg.brain["ollama_model"]
    model = model + ("-text" if not model.endswith("-text") and f"{model}-text" in os.popen("ollama list").read()
                     else "")
    items = [json.loads(l) for l in TEACHER.read_text().splitlines() if l.strip()]
    learn, _ = exam_split(items)
    rng = random.Random(7)
    learn = learn[:limit] if limit else learn
    pairs, right, t0 = [], 0, time.time()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:11434", timeout=120) as http:
        for i, it in enumerate(learn):
            r = row(it, brain, rng)
            body = {"model": model, "messages": r["messages"][:2], "stream": False, "think": False,
                    "tools": [{"type": "function", "function": t} for t in r["tools"]], "keep_alive": "5m",
                    "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 200}}
            msg = (await http.post("/api/chat", json=body)).json()["message"]
            got = [{"name": c["function"]["name"], "args": c["function"].get("arguments") or {}}
                   for c in msg.get("tool_calls") or []]
            ok = same_calls(got, r["expect"]) if r["expect"] else not got
            right += ok
            if not ok:
                rejected = {"role": "assistant", "content": (msg.get("content") or "").strip()}
                if got:
                    rejected = {"role": "assistant", "content": "", "tool_calls": [
                        {"type": "function", "function": {"name": c["name"], "arguments": c["args"]}} for c in got]}
                if not real_mistake(r["messages"][2], rejected):
                    continue
                pairs.append({"id": r["id"], "cat": it["cat"], "prompt": r["messages"][:2], "tools": r["tools"],
                              "chosen": r["messages"][2], "rejected": rejected})
            if i % 50 == 49:
                print(f"{i + 1}/{len(learn)}: {right / (i + 1):.0%} right, {len(pairs)} mistakes, "
                      f"{time.time() - t0:.0f}s", flush=True)
    PAIRS.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pairs))
    print(f"{model}: {right}/{len(learn)} right; {len(pairs)} mistakes → {PAIRS}")


# ── exam: fixed, broad, never trained on ─────────────────────────────────────
def exam():
    _, _, brain = toolbox()
    rows = teacher_exam(brain)
    EXAM.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    cats: dict[str, int] = {}
    for r in rows:
        cats[r["cat"]] = cats.get(r["cat"], 0) + 1
    print(f"{len(rows)} exam questions → {EXAM}\n" + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))


def main():
    a = sys.argv[1:]
    opt = lambda k, d: a[a.index(k) + 1] if k in a else d  # noqa: E731
    cmd = a[0] if a else ""
    if cmd == "gen":
        only = [x for x in opt("--only", "").split(",") if x]
        asyncio.run(gen(int(opt("--rounds", 3)), int(opt("--per", 40)), opt("--model", "sonnet"),
                        int(opt("--jobs", 4)), only))
    elif cmd == "check":
        check()
    elif cmd == "mistakes":
        asyncio.run(mistakes(opt("--model", ""), int(opt("--limit", 0))))
    elif cmd == "exam":
        exam()
    elif cmd == "filter":  # re-apply real_mistake to an existing pairs file
        ps = [json.loads(l) for l in PAIRS.read_text().splitlines() if l.strip()]
        keep = [p for p in ps if real_mistake(p["chosen"], p["rejected"])]
        PAIRS.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in keep))
        print(f"{len(keep)}/{len(ps)} pairs are real mistakes")
    elif cmd == "cost":
        cost()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
