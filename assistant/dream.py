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

log = logging.getLogger("dream")

HOME = XDG_DATA / "assistant" / "dream"
VENV_PY = HOME / "venv" / "bin" / "python"
LLAMA_CPP = HOME / "llama.cpp"            # convert_lora_to_gguf.py + build/bin/llama-{export-lora,quantize}
DATA = HOME / "data.jsonl"
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
    assistant.tools.vision = dict(v, ollama_model=model,
                                  vision_model=model if v.get("vision_model") == base else v.get("vision_model"))


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
    HOME.mkdir(parents=True, exist_ok=True)
    DATA.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return len(rows)


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
        if len(skills) < self.cfg.get("train_min_total", 80):
            return f"{len(skills)}/{self.cfg.get('train_min_total', 80)} wins so far"
        if new < self.cfg.get("train_min_new", 40):
            return f"{new}/{self.cfg.get('train_min_new', 40)} new wins since the last dream"
        if last and dt.datetime.now() - dt.datetime.fromisoformat(last) < dt.timedelta(
                days=self.cfg.get("train_every_days", 3)):
            return "dreamed recently"
        return ""

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
        asyncio.create_task(self._watch_idle(minutes))
        while True:
            await asyncio.sleep(300)
            try:
                if self.proc is None and not self.due() and await self._away(minutes):
                    await self.dream()
            except Exception:
                log.exception("dream failed")

    async def dream(self, dry: bool = False):
        if not dry and self.a.learning:  # homework first: new wordings for what she learned (~1 s each)
            self._practice = asyncio.create_task(self.a.learning.practice(self.a.local, self.cfg.get("practice", 80)))
            try:
                await self._practice
            except asyncio.CancelledError:
                return  # woken up: no training this time
            finally:
                self._practice = None
        n = export(self.a.learning.skills.items, self.a.local)
        log.info("dreaming: training on %d wins (stops as soon as you need me)", n)
        await self.a.local.unload()  # ~6 GB; training peaks at ~9.5 GB in total with Whisper still loaded
        env = dict(os.environ, PYTHONPATH=str(PROJECT_DIR), PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
        self.proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "19", "ionice", "-c", "3", str(VENV_PY), "-m", "assistant.dream", "train",
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


def exam(model: str, rows: list[dict]) -> float:
    """Share of held-out wins where the model picks the same tool (½) with the same arguments (½)."""
    score = 0.0
    for r in rows:
        try:
            msg = _ollama("/api/chat", {"model": model, "messages": r["messages"][:2], "stream": False,
                                        "think": False, "keep_alive": "2m",
                                        "tools": [{"type": "function", "function": t} for t in r["tools"]],
                                        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 200}})["message"]
        except Exception as e:
            print(f"exam {model}: {e}", file=sys.stderr)
            continue
        calls = msg.get("tool_calls") or []
        want = r["expect"][0]
        if calls and calls[0]["function"]["name"] == want["name"]:
            got = calls[0]["function"].get("arguments") or {}
            score += 0.5 + 0.5 * (all(_same(got.get(k), v) for k, v in want["args"].items()))
    _ollama("/api/generate", {"model": model, "keep_alive": 0})
    return round(score / max(len(rows), 1), 3)


def train(base: str, dry: bool = False):
    weights, conf, model_name = BASES[base]
    rows, alts = [], {}
    for r in (json.loads(l) for l in DATA.read_text().splitlines() if l.strip()):
        if r.get("alt"):
            alts[r["skill"]] = alts.get(r["skill"], 0) + 1
            if alts[r["skill"]] > MAX_ALTS:
                continue
        rows.append(r)
    wins = [r for r in rows if not r.get("alt")]
    held = wins[4::5] if len(wins) >= 25 else wins[-max(1, len(wins) // 5):]   # every 5th win: the exam
    exam_skills = {r.get("skill", r["id"]) for r in held}
    learn = [r for r in rows if r.get("skill", r["id"]) not in exam_skills]  # its rewordings would leak the exam
    print(f"{len(learn)} wins to learn from, {len(held)} for the exam", file=sys.stderr)
    if len(learn) < 10:
        sys.exit(f"not enough wins to learn from ({len(learn)})")

    from unsloth import FastLanguageModel  # noqa: E402 (first: it patches transformers)
    from unsloth.chat_templates import train_on_responses_only
    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer

    model, tok = FastLanguageModel.from_pretrained(str(HOME / weights), max_seq_length=4096, load_in_4bit=True)
    tok = getattr(tok, "tokenizer", tok)  # qwen3.5 comes with a text+image processor: train on its text part
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=16, lora_dropout=0, bias="none", use_gradient_checkpointing="unsloth",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])

    def render(r):
        return {"text": tok.apply_chat_template(r["messages"], tools=r["tools"], tokenize=False,
                                                enable_thinking=False)}
    texts = [render(r) for r in learn]
    texts = [t for t in texts if len(tok(t["text"]).input_ids) <= 4096]
    ds = Dataset.from_list(texts)
    trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=ds, args=SFTConfig(
        dataset_text_field="text", max_seq_length=4096, per_device_train_batch_size=1,
        gradient_accumulation_steps=4, num_train_epochs=2, learning_rate=1e-4, warmup_steps=5,
        lr_scheduler_type="cosine", optim="adamw_8bit", logging_steps=5, output_dir=str(HOME / "run"),
        save_strategy="no", report_to="none", seed=7))
    # learn only her own turns (tool calls and answers), not the prompts or tool results
    trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n",
                                      response_part="<|im_start|>assistant\n")
    trainer.train()
    adapter = HOME / "adapter"
    model.save_pretrained(str(adapter))
    tok.save_pretrained(str(adapter))
    del model, trainer
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
    base_score = exam(base, held)
    before = active_model({}, base)
    current = exam(before, held) if before else 0.0  # the dream before, on the same questions
    score = exam(candidate, held)
    print(f"exam: {candidate} {score} vs {base} {base_score} (current {current})", file=sys.stderr)
    result = {"trained_at": dt.datetime.now().isoformat(timespec="seconds"), "examples": len(texts),
              "score": score, "base_score": base_score, "model": model_name, "base": base}
    if dry:
        subprocess.run(["ollama", "rm", candidate], stdout=subprocess.DEVNULL)
        print(json.dumps(result), file=sys.stderr)
        return
    # requantizing costs a little precision, so equal isn't good enough; and never worse than the current one
    if score > base_score and score >= current:
        subprocess.run(["ollama", "cp", candidate, model_name], check=True, stdout=subprocess.DEVNULL)
        result["active"] = True
    elif before:  # keep the previous dream if it was better
        result.update(active=True, model=before)
    else:
        result["active"] = False
    subprocess.run(["ollama", "rm", candidate], stdout=subprocess.DEVNULL)
    STATE.write_text(json.dumps(dict(s, **result), indent=1))


if __name__ == "__main__":
    if sys.argv[1:2] == ["train"] and "--base" in sys.argv:
        train(sys.argv[sys.argv.index("--base") + 1], dry="--dry" in sys.argv)
    else:
        print("usage: python -m assistant.dream train --base qwen3.5:9b [--dry]")
