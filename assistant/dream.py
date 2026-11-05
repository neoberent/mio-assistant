"""Mio dreams: while the PC is idle she fine-tunes her local model on her own wins (a small LoRA adapter).

Daemon side (`Dreamer`, stdlib only): when you've been away for a while, no game runs and she has enough new
wins (assistant/learning.py skills), it exports them as training chats, frees the GPU and starts the trainer
in its own venv (scripts/install_dream.sh) at the lowest CPU/IO priority. Talking to her, a game starting
or you coming back stops it at once; it simply tries again another time.

Trainer side (`python -m assistant.dream train --base <ollama model>`, needs the dream venv): QLoRA with unsloth
on the HF weights of the model she runs (BASES; loaded in 4 bit, Whisper stays loaded so "Hey Mio" keeps
working) → GGUF LoRA → merged into Ollama's own file of that model (llama-export-lora; Ollama dropped adapter
support) → back to Q4_K_M (llama-quantize) → `ollama create`. Same size, so she uses no more VRAM than before.
Then an exam on wins it didn't train on, against the plain model: she only switches to her trained self
(`mio-qwen35`, …) if it does better.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import PROJECT_DIR, STATE_DIR, XDG_DATA
from .aio import spawn
from .fileutil import atomic_write_text

log = logging.getLogger("dream")

HOME = XDG_DATA / "assistant" / "dream"
VENV_PY = HOME / "venv" / "bin" / "python"
LLAMA_CPP = HOME / "llama.cpp"            # convert_lora_to_gguf.py + build/bin/llama-{export-lora,quantize}
DATA = HOME / "data.jsonl"
TEACHER = HOME / "teacher.jsonl"   # scripts/distill.py: checked requests + calls written by Claude as teacher
PAIRS = HOME / "pairs.jsonl"       # her own wrong answers next to the right ones (distill.py mistakes + complaints)
EXAM = HOME / "exam.jsonl"         # fixed exam: held-out teacher requests of every topic + the school quizzes
NEGATIVES = STATE_DIR / "negatives.jsonl"  # what she did when the user said "no, that's wrong"
STATE = STATE_DIR / "dream.json"
# Ollama model she runs → (weights trained on, folder the LoRA → GGUF converter reads, name of her trained self)
BASES = {
    # unsloth/Qwen3-8B-unsloth-bnb-4bit; Qwen/Qwen3-8B config + tokenizer
    "qwen3:8b": ("base", "base-config", "mio-qwen3"),
    # unsloth/Qwen3.5-9B (no 4-bit upload: quantized on load); its converter indexes the full weights
    "qwen3.5:9b": ("base-qwen35", "base-qwen35", "mio-qwen35"),
}
MAX_ALTS = 1  # practice wordings per win in training: all of them (~3.5 each) made a dream take over an hour


def state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def installed(base: str) -> bool:
    if base not in BASES:
        return False
    weights, conf, _ = BASES[base]
    return VENV_PY.exists() and (HOME / weights / "config.json").exists() and \
        (HOME / conf / "config.json").exists() and (LLAMA_CPP / "build/bin/llama-quantize").exists()


def active_model(cfg: dict, base: str) -> str:
    """The model the local brain should use: the trained one if it passed its exam and isn't turned off — and
    was trained on the model she runs now (not an old dream of qwen3 after switching to qwen3.5)."""
    s = state()
    if s.get("active") and cfg.get("use_trained", True) and s.get("base", "qwen3:8b") == base:
        return s["model"]
    return ""


def use_trained(assistant, model: str):
    """Switch her brain to her trained self; her eyes too when they're the same model (it keeps its vision)."""
    base = assistant.local.cfg["ollama_model"]
    assistant.local.cfg = dict(assistant.local.cfg, ollama_model=model)
    v = assistant.tools.vision
    same = v.get("vision_model") == base or (base.endswith("-text") and  # brain was textbrain's copy of the eyes
                                             (v.get("vision_model") or "").split(":")[0] == base[:-5])
    assistant.tools.vision = dict(v, ollama_model=model, vision_model=model if same else v.get("vision_model"))


# ── teacher data (scripts/distill.py) in her exact prompt shape ─────────────
GENERIC_NOW = [
    "Right now — focused window: kitty — ~ - fish.",
    "Right now — focused window: zen — YouTube; media playing: Lofi Girl — lofi hip hop radio.",
    "Right now — focused window: discord — Friends - Discord.",
    "Right now — focused window: zen — Reddit - Dive into anything.",
    "Right now — focused window: Riff — Riff; media playing: Riff — Blinding Lights.",
    "Right now — focused window: steam — Steam.",
]
TESTS = PROJECT_DIR / "tests"


def _norm_said(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def hash_said(say: str) -> int:
    import hashlib
    return int(hashlib.sha1(_norm_said(say).encode()).hexdigest()[:8], 16)


def exam_split(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Every 8th teacher request (by its words, so always the same ones) is kept for the exam."""
    learn, held = [], []
    for it in items:
        (held if hash_said(it["say"]) % 8 == 0 else learn).append(it)
    return learn, held


def row(item: dict, brain, rng) -> dict:
    """A teacher request → the chat she'd see (her system prompt with a 'Right now' line, the offered tools)
    and the teacher's answer: the tool calls, or the words for small talk / an either-or question."""
    from .brain.local import pick_tools

    now = item.get("now") or rng.choice(GENERIC_NOW)
    if not now.startswith("Right now"):
        now = "Right now — " + now
    names = pick_tools(item["say"], [])
    names += [c["name"] for c in item["calls"] if c["name"] not in names]
    messages = [{"role": "system", "content": brain.system_prompt(names, now)},
                {"role": "user", "content": item["say"]}]
    if item["calls"]:
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": c["name"], "arguments": c["args"]}} for c in item["calls"]]})
    else:
        messages.append({"role": "assistant", "content": item["reply"].strip()})
    return {"id": f"t{hash_said(item['say']):x}", "cat": item["cat"], "teacher": True, "messages": messages,
            "tools": [x["function"] for x in brain._tool_specs(names)],
            "expect": [{"name": c["name"], "args": c["args"]} for c in item["calls"]]}


def teacher_rows(brain) -> list[dict]:
    import random
    rng = random.Random(7)
    learn, _ = exam_split(_read(TEACHER))
    return [row(it, brain, rng) for it in learn]


def teacher_exam(brain) -> list[dict]:
    """The fixed exam: held-out teacher requests of every topic + the school quizzes (graded like school)."""
    import random
    rng = random.Random(11)
    _, held = exam_split(_read(TEACHER))
    rows = [row(it, brain, rng) for it in held]
    for f in sorted(TESTS.glob("curriculum*.json")):
        for q in json.loads(f.read_text())["quiz"]:
            if len(q["teach"]) != 1:  # multi-step quiz items are split by the daemon before her brain sees them
                continue
            r = row({"say": q["say"], "calls": q["teach"],
                     "cat": "quiz-" + (f.stem.removeprefix("curriculum").strip("_") or "main")}, brain, rng)
            rows.append(dict(r, ok=q.get("ok", []), loose=True))
    return rows


def _free_mb() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


# ── daemon side ─────────────────────────────────────────────────────────────
def export(skills: list[dict], brain) -> int:
    """Wins → chats in exactly the shape the local model sees (same system prompt, same tool specs)."""
    from .brain.local import pick_tools

    rows = []
    for s in skills:
        names = pick_tools(s["text"], [])
        names += [c["name"] for c in s["calls"] if c["name"] not in names]
        calls = [{"type": "function", "function": {"name": c["name"], "arguments": c["args"]}} for c in s["calls"]]
        messages = [{"role": "system", "content": brain.system_prompt(names)},
                    {"role": "user", "content": s["text"]},
                    {"role": "assistant", "content": "", "tool_calls": calls}]
        messages += [{"role": "tool", "content": c["result"][:1200]} for c in s["calls"]]
        messages.append({"role": "assistant", "content": s["reply"]})
        tools = [x["function"] for x in brain._tool_specs(names)]
        expect = [{"name": c["name"], "args": c["args"]} for c in s["calls"]]
        rows.append({"id": s["id"], "skill": s["id"], "messages": messages, "tools": tools, "expect": expect})
        for i, alt in enumerate(s.get("alts", [])):  # her practice wordings: same calls, other words
            m = [dict(x) for x in messages]
            m[1] = {"role": "user", "content": alt["text"]}
            rows.append({"id": f"{s['id']}-{i}", "skill": s["id"], "alt": True, "messages": m, "tools": tools,
                         "expect": expect})
    rows += teacher_rows(brain)
    HOME.mkdir(parents=True, exist_ok=True)
    DATA.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    if TEACHER.exists():
        EXAM.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in teacher_exam(brain)))
    complaint_pairs(skills, brain)
    return len(rows)


def complaint_pairs(skills: list[dict], brain) -> int:
    """Real corrections: a request she got wrong ("no, that's wrong") that later worked → a pair for the
    preference pass (pairs-real.jsonl next to the teacher's pairs)."""
    from .brain.local import pick_tools
    from .learning import _norm

    try:
        bad = [json.loads(l) for l in NEGATIVES.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return 0
    good = {s["norm"]: s for s in skills if s.get("norm")}
    out = []
    for b in bad:
        s = good.get(_norm(b["text"]))
        if not s or not b.get("calls") or [c["name"] for c in b["calls"]] == [c["name"] for c in s["calls"]] and \
                all(c["args"] == d["args"] for c, d in zip(b["calls"], s["calls"])):
            continue
        names = pick_tools(s["text"], [])
        names += [c["name"] for c in s["calls"] + b["calls"] if c["name"] not in names]
        call = lambda cs: {"role": "assistant", "content": "", "tool_calls": [  # noqa: E731
            {"type": "function", "function": {"name": c["name"], "arguments": c["args"]}} for c in cs]}
        out.append({"id": "n" + s["id"], "cat": "real", "tools": [x["function"] for x in brain._tool_specs(names)],
                    "prompt": [{"role": "system", "content": brain.system_prompt(names)},
                               {"role": "user", "content": s["text"]}],
                    "chosen": call(s["calls"]), "rejected": call(b["calls"])})
    (HOME / "pairs-real.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in out))
    return len(out)


class Dreamer:
    def __init__(self, assistant, cfg: dict, base: str):
        self.a = assistant
        self.cfg = cfg
        self.base = base  # the Ollama model she runs, the one she dreams on
        self.proc: asyncio.subprocess.Process | None = None
        self._practice: asyncio.Task | None = None
        self.user_idle = False   # from swayidle (input idle, respects video idle-inhibitors)
        self._idle_watch = False

    def due(self) -> str:
        """'' if a dream is due, else why not."""
        if not self.cfg.get("train", True):
            return "training is off"
        if not installed(self.base):
            return f"trainer not installed for {self.base} (scripts/install_dream.sh {self.base})"
        skills = self.a.learning.skills.items if self.a.learning else []
        s = state()
        last = s.get("trained_at", "")
        new = sum(1 for k in skills if k["t"] > last)
        if self._carry_on() or self._new_teacher(last):  # unfinished dream, or new teacher data: no waiting
            return ""
        if len(skills) < self.cfg.get("train_min_total", 80):
            return f"{len(skills)}/{self.cfg.get('train_min_total', 80)} wins so far"
        if new < self.cfg.get("train_min_new", 40):
            return f"{new}/{self.cfg.get('train_min_new', 40)} new wins since the last dream"
        if last and dt.datetime.now() - dt.datetime.fromisoformat(last) < dt.timedelta(
                days=self.cfg.get("train_every_days", 3)):
            return "dreamed recently"
        return ""

    async def _free_gpu(self, need_mb: int = 9000) -> bool:
        """Her voice (~2.7 GB) steps aside for the trainer and restarts by itself the next time she speaks. It may
        still be loading (right after a restart), so check the VRAM really is free before starting."""
        for _ in range(30):
            if self.a.gaming:
                return False
            tts = getattr(self.a, "tts", None)
            if tts is not None and getattr(tts, "_chatterbox", None) is not None:
                await asyncio.to_thread(tts._chatterbox.close)
            if _free_mb() >= need_mb:
                return True
            await asyncio.sleep(3)
        return False

    @staticmethod
    def _carry_on() -> bool:
        """A dream was woken up halfway: its checkpoints are waiting."""
        return DATA.exists() and (any(HOME.glob("run/*/checkpoint-*")) or (HOME / "run/sft/done").exists())

    @staticmethod
    def _new_teacher(last: str) -> bool:
        for f in (TEACHER, PAIRS):
            if f.exists() and (not last or dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat() > last):
                return True
        return False

    async def _watch_idle(self, minutes: int):
        """swayidle reports when there was no keyboard/mouse input for `minutes` (and when you're back)."""
        exe = shutil.which("swayidle")
        if not exe:
            return
        self._idle_watch = True
        proc = await asyncio.create_subprocess_exec(
            exe, "-w", "timeout", str(minutes * 60), "echo idle", "resume", "echo active",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        async for line in proc.stdout:
            self.user_idle = line.strip() == b"idle"
            if not self.user_idle:
                await self.stop("you're back")
        self._idle_watch = False

    async def _away(self, minutes: int) -> bool:
        if self.a.gaming or self.a.state != "idle":
            return False
        if asyncio.get_running_loop().time() - self.a._last_activity < minutes * 60:
            return False
        if self._idle_watch:
            return self.user_idle
        # no swayidle: nothing playing and the GPU quiet counts as away
        try:
            sinks = subprocess.run(["pactl", "list", "sink-inputs", "short"], capture_output=True, text=True,
                                   timeout=5).stdout
            if any("RUNNING" in l for l in sinks.splitlines()):
                return False
            util = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=5).stdout
            return int(util.split()[0]) < 15
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return False

    async def run(self):
        minutes = self.cfg.get("train_idle_min", 30)
        if getattr(self, "_idle_task", None) is None or self._idle_task.done():  # once, also after a restart
            self._idle_task = spawn(self._watch_idle(minutes), "dream idle watch")
        while True:
            await asyncio.sleep(300)
            try:
                if self.proc is None and not self.due() and await self._away(minutes):
                    await self.dream()
            except Exception:
                log.exception("dream failed")

    async def dream(self, dry: bool = False):
        if self.a.gaming:
            log.info("no dreaming while you play")
            return
        carry_on = not dry and self._carry_on()  # woken up last time: same data, from its last checkpoint
        if not dry and not carry_on and self.a.learning:  # homework first: new wordings for what she learned (~1 s each)
            self._practice = asyncio.create_task(self.a.learning.practice(self.a.local, self.cfg.get("practice", 80)))
            try:
                await self._practice
            except asyncio.CancelledError:
                return  # woken up: no training this time
            finally:
                self._practice = None
        if carry_on:
            log.info("dreaming on from where I woke up last time (stops as soon as you need me)")
        else:
            n = export(self.a.learning.skills.items, self.a.local)
            log.info("dreaming: training on %d wins (stops as soon as you need me)", n)
        await self.a.local.unload()  # ~6 GB; training peaks at ~9.5 GB in total with Whisper still loaded
        if self.a.gaming:  # never next to a game (also when started by hand)
            log.info("dream skipped: a game is running")
            return
        if not await self._free_gpu():
            log.info("dream skipped: not enough free VRAM (%d MB)", _free_mb())
            return
        env = dict(os.environ, PYTHONPATH=str(PROJECT_DIR), PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
        # its own cgroup with a memory ceiling: if RAM gets tight (2026-11-03 systemd-oomd killed all of
        # assistantd with the trainer in it), the trainer slows down or is the one stopped, never Mio
        scope = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryHigh=12G",
                 "-p", "MemoryMax=16G", "-p", "MemorySwapMax=4G"] if shutil.which("systemd-run") else []
        self.proc = await asyncio.create_subprocess_exec(
            *scope, "nice", "-n", "19", "ionice", "-c", "3", str(VENV_PY), "-m", "assistant.dream", "train",
            "--base", self.base, *(["--dry"] if dry else []), env=env, start_new_session=True,
            cwd=HOME,  # unsloth writes its compiled cache into the working directory
            stdout=asyncio.subprocess.DEVNULL, stderr=open(HOME / "train.log", "w"))
        code = await self.proc.wait()
        self.proc = None
        s = state()
        if dry:
            log.info("test dream finished (exit %s), see %s", code, HOME / "train.log")
        elif code == 0 and (trained := active_model(self.cfg, self.base)):
            use_trained(self.a, trained)
            from . import textbrain

            await asyncio.to_thread(textbrain.apply, self.a)
            log.info("woke up smarter: now using %s (exam %s vs %s for plain %s)", trained, s.get("score"),
                     s.get("base_score"), self.base)
        elif code == 0:
            log.info("dream done; kept %s (exam %s vs %s)", self.base, s.get("score"), s.get("base_score"))
        else:
            log.info("dream stopped (exit %s), see %s", code, HOME / "train.log")

    async def stop(self, why: str):
        """Free the GPU right now (talking to her, a game starting, you coming back)."""
        if self._practice and not self._practice.done():
            log.info("practice stopped: %s", why)
            self._practice.cancel()
        p = self.proc
        if p is None or p.returncode is not None:
            return
        log.info("waking up from a dream: %s", why)
        try:
            os.killpg(p.pid, signal.SIGTERM)
            await asyncio.wait_for(p.wait(), 10)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ── trainer side (dream venv) ───────────────────────────────────────────────
def _ollama(path: str, body: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:11434{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _same(a, b) -> bool:
    norm = lambda v: re.sub(r"\W+", " ", str(v).lower()).strip()
    return norm(a) == norm(b)


STRICT_ARGS = {"action", "mode", "source", "site", "direction", "keys", "command", "apply", "inside", "submit",
               "shuffle", "list"}
WORDY_ARGS = {"query", "who", "text", "game", "name", "label", "target", "playlist", "location", "app", "fact",
              "question", "path", "items", "at", "url"}
_FILLER = {"the", "and", "for", "you", "can", "please", "with", "that", "this", "newest", "video", "some", "my"}


def _words(v) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(v).lower()) if len(w) > 2 and w not in _FILLER}


def arg_ok(key: str, got, want) -> bool:
    if _same(got, want):
        return True
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        try:
            return float(got) == float(want)
        except (TypeError, ValueError):
            return False
    if key in WORDY_ARGS or key not in STRICT_ARGS and isinstance(want, str) and len(str(want)) > 12:
        w, g = _words(want), _words(got)
        return bool(w) and len(w & g) >= max(1, (len(w) + 1) // 2)
    return False


def calls_match(got: list[dict], want: list[dict]) -> bool:
    """Same tools in order (only the first part now is fine: the rest follows next turn), strict values equal,
    wordy values sharing the important words, and no invented extra place/person."""
    if not got or len(got) > len(want):
        return False
    for g, w in zip(got, want):
        if g["name"] != w["name"]:
            return False
        ga = g.get("args") or {}
        if not all(arg_ok(k, ga.get(k), v) for k, v in w["args"].items()):
            return False
        if any(ga.get(k) and k not in w["args"] for k in ("location", "who", "at")):
            return False
    return True


CLAIM = re.compile(r"\bI(?:'ve| have)? (?:just )?(?:opened|launched|started|installed|closed|moved|deleted|created|"
                   r"sent|set|turned|switched|checked|updated|played|killed|found|muted|paused|skipped)\b", re.I)


def grade(r: dict, msg: dict) -> float:
    """One exam answer: 1 right, 0.5 right tool with wrong details, 0 wrong."""
    calls = [{"name": c["function"]["name"], "args": c["function"].get("arguments") or {}}
             for c in msg.get("tool_calls") or []]
    if not r["expect"]:  # small talk / either-or question: words, no tool, no pretending
        text = (msg.get("content") or "").strip()
        return float(not calls and bool(text) and not CLAIM.search(text))
    if r.get("loose"):  # school quiz: any call doing the taught thing (or an allowed other tool) counts
        want = r["expect"][0]
        if any(c["name"] in r.get("ok", []) for c in calls):
            return 1.0
        hit = [c for c in calls if c["name"] == want["name"]]
        return 0.0 if not hit else 1.0 if any(calls_match([c], [want]) for c in hit) else 0.5
    if calls_match(calls, r["expect"]):
        return 1.0
    return 0.5 if calls and calls[0]["name"] == r["expect"][0]["name"] else 0.0


def exam(model: str, rows: list[dict]) -> tuple[float, dict]:
    """→ (overall score, score per topic) on questions it never trained on."""
    per: dict[str, list[float]] = {}
    for r in rows:
        try:
            msg = _ollama("/api/chat", {"model": model, "messages": r["messages"][:2], "stream": False,
                                        "think": False, "keep_alive": "2m",
                                        "tools": [{"type": "function", "function": t} for t in r["tools"]],
                                        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 200}})["message"]
            s = grade(r, msg)
        except Exception as e:
            print(f"exam {model}: {e}", file=sys.stderr)
            s = 0.0
        per.setdefault(r.get("cat", "real"), []).append(s)
    _ollama("/api/generate", {"model": model, "keep_alive": 0})
    cats = {k: round(sum(v) / len(v), 3) for k, v in per.items()}
    allv = [x for v in per.values() for x in v]
    return round(sum(allv) / max(len(allv), 1), 3), cats


def _read(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def _render_pair(tok, p: dict) -> dict | None:
    """A preference pair as plain text in the exact chat format: prompt / chosen turn / rejected turn."""
    kw = dict(tools=p["tools"], tokenize=False, enable_thinking=False)
    prompt = tok.apply_chat_template(p["prompt"], add_generation_prompt=True, **kw)
    out = {"prompt": prompt}
    for k in ("chosen", "rejected"):
        full = tok.apply_chat_template(p["prompt"] + [p[k]], **kw)
        if not full.startswith(prompt):
            return None
        out[k] = full[len(prompt):]
    return out if out["chosen"] != out["rejected"] else None


def train(base: str, dry: bool = False, smoke: bool = False):
    weights, conf, model_name = BASES[base]
    rows, alts = [], {}
    for r in _read(DATA):
        if r.get("alt"):
            alts[r["skill"]] = alts.get(r["skill"], 0) + 1
            if alts[r["skill"]] > MAX_ALTS:
                continue
        rows.append(r)
    wins = [r for r in rows if not r.get("alt") and not r.get("teacher")]
    held = wins[4::5] if len(wins) >= 25 else wins[-max(1, len(wins) // 5):]   # every 5th real win: the exam
    exam_skills = {r.get("skill", r["id"]) for r in held}
    learn = [r for r in rows if r.get("skill", r["id"]) not in exam_skills]  # its rewordings would leak the exam
    held = [dict(r, cat="real") for r in held] + _read(EXAM)  # + the fixed exam (teacher topics, school quizzes)
    pairs = _read(PAIRS) + _read(HOME / "pairs-real.jsonl")
    teacher = sum(1 for r in learn if r.get("teacher"))
    print(f"{len(learn)} examples to learn from ({teacher} from the teacher), {len(pairs)} mistakes to unlearn, "
          f"{len(held)} exam questions", file=sys.stderr)
    if len(learn) < 10:
        sys.exit(f"not enough wins to learn from ({len(learn)})")
    if smoke:  # a few minutes: does every step of training work (no merge, no exam)
        learn = [r for r in learn if r.get("teacher")][:12] + [r for r in learn if not r.get("teacher")][:4]
        pairs = pairs[:24]

    from unsloth import FastLanguageModel, PatchDPOTrainer  # noqa: E402 (first: it patches transformers)
    from unsloth.chat_templates import train_on_responses_only
    from datasets import Dataset
    from trl import DPOConfig, DPOTrainer, SFTConfig, SFTTrainer

    # checkpoints survive being woken up: the next dream reuses this data (Dreamer skips the export) and
    # carries on where this one stopped; the folder goes once a dream finishes
    run_dir = HOME / ("run" if not smoke else "smoke")
    shutil.rmtree(HOME / "smoke", ignore_errors=True)

    def last_ckpt(d: Path) -> str | None:
        ck = sorted(d.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[1]))
        return str(ck[-1]) if ck else None

    def load():
        m, t = FastLanguageModel.from_pretrained(str(HOME / weights), max_seq_length=4608, load_in_4bit=True)
        t = getattr(t, "tokenizer", t)  # qwen3.5 comes with a text+image processor: train on its text part
        m = FastLanguageModel.get_peft_model(
            m, r=16, lora_alpha=16, lora_dropout=0, bias="none", use_gradient_checkpointing="unsloth",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
        return m, t
    model, tok = load()

    def render(r):
        return {"text": tok.apply_chat_template(r["messages"], tools=r["tools"], tokenize=False,
                                                enable_thinking=False)}
    texts = [render(r) for r in learn]
    texts = [t for t in texts if len(tok(t["text"]).input_ids) <= 4608]
    random_order = __import__("random").Random(7)
    random_order.shuffle(texts)  # topics mixed, real wins among them
    sft_dir = run_dir / "sft"
    trained_now = not (sft_dir / "done").exists()
    if trained_now:
        trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=Dataset.from_list(texts), args=SFTConfig(
            dataset_text_field="text", max_seq_length=4608, per_device_train_batch_size=1,
            gradient_accumulation_steps=4, num_train_epochs=1 if teacher else 2, learning_rate=1e-4,
            warmup_steps=10, lr_scheduler_type="cosine", optim="adamw_8bit", logging_steps=10,
            output_dir=str(sft_dir), save_strategy="steps", save_steps=25, save_total_limit=1,
            report_to="none", seed=7))
        # learn only her own turns (tool calls and answers), not the prompts or tool results
        trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n",
                                          response_part="<|im_start|>assistant\n")
        trainer.train(resume_from_checkpoint=last_ckpt(sft_dir))
        model.save_pretrained(str(sft_dir / "adapter"))
        (sft_dir / "done").write_text("1")
        del trainer
    if trained_now and pairs:
        # DPO runs chosen+rejected together (~2x the memory of SFT) and only fits on a clean GPU,
        # so it starts from fresh base weights plus the finished SFT adapter, like a new dream would
        import gc
        import torch
        del model
        gc.collect()
        torch.cuda.empty_cache()
        model, tok = load()
    if not trained_now or pairs:
        from peft import set_peft_model_state_dict
        from safetensors.torch import load_file
        set_peft_model_state_dict(model, load_file(str(sft_dir / "adapter" / "adapter_model.safetensors")))
        if not trained_now:
            print("SFT already done in an earlier dream, carrying on", file=sys.stderr)

    # her own mistakes: "this, not that" — pushes away from what she actually got wrong
    rendered = [x for x in (_render_pair(tok, p) for p in pairs) if x and
                len(tok(x["prompt"] + x["chosen"]).input_ids) <= 4608 and
                len(tok(x["prompt"] + x["rejected"]).input_ids) <= 4608]
    if len(rendered) >= 20:
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()
        PatchDPOTrainer()
        dpo_dir = run_dir / "dpo"
        dpo = DPOTrainer(model=model, ref_model=None, processing_class=tok, train_dataset=Dataset.from_list(rendered),
                         args=DPOConfig(
                             beta=0.1, loss_type=["sigmoid", "sft"], loss_weights=[1.0, 0.5], max_length=4608,
                             per_device_train_batch_size=1, gradient_accumulation_steps=4, num_train_epochs=1,
                             learning_rate=5e-6 * 4, warmup_steps=5, lr_scheduler_type="cosine", optim="adamw_8bit",
                             logging_steps=10, output_dir=str(dpo_dir), save_strategy="steps", save_steps=25,
                             save_total_limit=1, report_to="none", seed=7))
        dpo.train(resume_from_checkpoint=last_ckpt(dpo_dir))
        del dpo
    if smoke:
        shutil.rmtree(run_dir, ignore_errors=True)
        print(f"smoke test ok: {len(texts)} examples, {len(rendered)} pairs, peak VRAM "
              f"{__import__('torch').cuda.max_memory_allocated() / 2**30:.1f} GB", file=sys.stderr)
        return
    adapter = HOME / "adapter"
    model.save_pretrained(str(adapter))
    tok.save_pretrained(str(adapter))
    del model
    import gc
    import torch
    gc.collect()
    torch.cuda.empty_cache()

    lora, f16, q4 = HOME / "mio-lora.gguf", HOME / "merged-f16.gguf", HOME / "merged-q4.gguf"
    run = lambda *argv: subprocess.run([str(a) for a in argv], check=True, stdout=subprocess.DEVNULL)
    run(sys.executable, LLAMA_CPP / "convert_lora_to_gguf.py", "--base", HOME / conf, "--outtype", "f16",
        "--outfile", lora, adapter)
    modelfile = subprocess.run(["ollama", "show", base, "--modelfile"], capture_output=True, text=True,
                               check=True).stdout
    blob = re.search(r"^FROM (\S+)", modelfile, re.M)[1]  # Ollama's own Q4_K_M file of it
    try:
        run(LLAMA_CPP / "build/bin/llama-export-lora", "-m", blob, "--lora", lora, "-o", f16, "-t", "8")  # ~30 s
        run(LLAMA_CPP / "build/bin/llama-quantize", "--allow-requantize", f16, q4, "Q4_K_M", "8")       # ~50 s
        f16.unlink()
        (HOME / "Modelfile").write_text(re.sub(r"^FROM \S+", f"FROM {q4}", modelfile, count=1,
                                               flags=re.M))  # only the model, not a vision projector
        candidate = model_name + ("-test" if dry else "-new")
        run("ollama", "create", candidate, "-f", HOME / "Modelfile")  # same template/tools/thinking as the base
    finally:
        for f in (f16, q4):  # Ollama keeps its own copy
            f.unlink(missing_ok=True)

    s = state()
    base_score, base_cats = exam(base, held)
    before = active_model({}, base)
    current, cur_cats = exam(before, held) if before else (0.0, {})  # the dream before, on the same questions
    score, cats = exam(candidate, held)
    print(f"exam: {candidate} {score} vs {base} {base_score} (current {before or '-'} {current})", file=sys.stderr)
    for k in sorted(cats):
        print(f"  {k:14} {cats[k]:.2f}  plain {base_cats.get(k, 0):.2f}  current {cur_cats.get(k, 0):.2f}",
              file=sys.stderr)
    # a topic that got much worse than the plain model means it forgot something: don't take it, whatever the total
    worse = [k for k in cats if cats[k] < base_cats.get(k, 0) - 0.15]
    result = {"trained_at": dt.datetime.now().isoformat(timespec="seconds"), "examples": len(texts),
              "pairs": len(rendered), "score": score, "base_score": base_score, "current_score": current,
              "topics": cats, "worse_topics": worse, "model": model_name, "base": base}
    if dry:
        subprocess.run(["ollama", "rm", candidate], stdout=subprocess.DEVNULL)
        print(json.dumps(result), file=sys.stderr)
        return
    # requantizing costs a little precision, so equal isn't good enough; and never worse than the current one
    if score > base_score and score >= current and not worse:
        subprocess.run(["ollama", "cp", candidate, model_name], check=True, stdout=subprocess.DEVNULL)
        result["active"] = True
    elif before:  # keep the previous dream if it was better
        result.update(active=True, model=before)
    else:
        result["active"] = False
    subprocess.run(["ollama", "rm", candidate], stdout=subprocess.DEVNULL)
    shutil.rmtree(run_dir, ignore_errors=True)  # finished: nothing to carry on from
    atomic_write_text(STATE, json.dumps(dict(s, **result), indent=1))


if __name__ == "__main__":
    if sys.argv[1:2] == ["train"] and "--base" in sys.argv:
        train(sys.argv[sys.argv.index("--base") + 1], dry="--dry" in sys.argv, smoke="--smoke" in sys.argv)
    else:
        print("usage: python -m assistant.dream train --base qwen3.5:9b [--dry]")
