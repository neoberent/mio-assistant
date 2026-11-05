#!/usr/bin/env python3
"""Before/after benchmark for gpu-unlock: Ollama, Whisper, Chatterbox, a raw matmul and LoRA training, with GPU temp and power.

Run with Mio's venv:  .venv/bin/python scripts/bench_unlock.py before
Results:              ~/.local/state/assistant/bench_unlock/<label>.json
Stops Mio's user services while it runs and restarts the ones that were running.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICES = ["assistantd.service", "assistant-avatar.service"]
OLLAMA_MODEL = "mio-qwen35"
OLLAMA_RUNS = 3
SPEECH_RUNS = 5
MATMUL_N = 8192
MATMUL_SECONDS = 20
SEED = 1234
RESULT_MARK = "BENCH_JSON:"
TTS_SENTENCE = "Good evening, I finished checking your calendar, and your first meeting tomorrow starts at nine thirty."
CLIP_TEXT = ("Please open the browser, find the weather forecast for this weekend, and then set a timer for twenty "
             "minutes. After that, play some quiet music and remind me to call my sister before dinner tonight.")
TTS_PY = Path.home() / ".local/share/assistant/chatterbox/.venv/bin/python"
DREAM = Path.home() / ".local/share/assistant/dream"
LORA_EXAMPLES = 200
LORA_STEPS = 40  # optimizer steps, 4 examples each
LORA_WARMUP_STEPS = 5


def ollama_prompt() -> str:
    """A fixed prompt of about 2000 tokens."""
    lines = [f"Log entry {i}: sensor {i % 17} at station {(i * 13) % 29} reported {(i * 37) % 1000} units, "
             f"status {['nominal', 'degraded', 'recovering'][i % 3]}." for i in range(1, 76)]
    return "\n".join(lines) + "\n\nWrite a detailed report of at least 300 words about what these log entries show."


# ---- workers: each runs in the venv that holds its libraries ----

def emit(obj: dict):
    print(RESULT_MARK + json.dumps(obj), flush=True)


def worker_whisper(s: dict):
    import wave

    import numpy as np
    from faster_whisper import WhisperModel

    from assistant.stt import VOCAB

    cfg = s["stt"]
    model = WhisperModel(cfg["model"], device=cfg["device"], compute_type=cfg["compute_type"],
                         download_root=s["whisper_root"], local_files_only=True)
    with wave.open(s["clip"], "rb") as f:  # Mio also hands whisper a float32 16 kHz array, not a file
        rate, raw = f.getframerate(), f.readframes(f.getnframes())
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    audio = np.interp(np.arange(0, len(pcm) * 16000 // rate) * rate / 16000, np.arange(len(pcm)), pcm).astype(np.float32)

    def once() -> tuple[float, str]:  # same arguments as STT._transcribe(command=True)
        start = time.perf_counter()
        segments, _ = model.transcribe(
            audio, language=cfg.get("language") or None, beam_size=cfg.get("beam_size", 5),
            vad_filter=False, condition_on_previous_text=False, without_timestamps=True,
            hotwords=None, initial_prompt=cfg.get("vocabulary", VOCAB))
        text = " ".join(x.text.strip() for x in segments).strip()
        return time.perf_counter() - start, text

    warmup, _ = once()
    runs = [once() for _ in range(SPEECH_RUNS)]
    emit({"warmup_s": warmup, "runs_s": [t for t, _ in runs], "median_s": statistics.median(t for t, _ in runs),
          "clip_s": len(audio) / 16000, "text": runs[-1][1]})


def worker_tts(s: dict):
    import wave

    import numpy as np
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    cfg = s["tts"]
    device, half = cfg.get("chatterbox_device", "cuda"), cfg.get("chatterbox_half", True)
    model = ChatterboxTurboTTS.from_pretrained(device=device)
    if half:  # same as tts_worker.py
        model.t3.half()
        torch.cuda.empty_cache()
    if cfg.get("chatterbox_voice"):
        model.prepare_conditionals(cfg["chatterbox_voice"])

    def once(text: str):
        torch.manual_seed(SEED)  # sampling is random: the same seed gives the same work every run
        start = time.perf_counter()
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16, enabled=half):
            wav = model.generate(text)
        torch.cuda.synchronize()
        return time.perf_counter() - start, wav.squeeze(0).float().cpu().numpy()

    if not Path(s["clip"]).exists():
        _, audio = once(CLIP_TEXT)
        with wave.open(s["clip"], "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(model.sr)
            f.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    warmup, _ = once(TTS_SENTENCE)
    runs = [once(TTS_SENTENCE) for _ in range(SPEECH_RUNS)]
    emit({"warmup_s": warmup, "runs_s": [t for t, _ in runs], "median_s": statistics.median(t for t, _ in runs),
          "audio_s": len(runs[-1][1]) / model.sr, "audio_samples": sorted({len(a) for _, a in runs})})


def worker_matmul(s: dict):
    import torch

    def tflops(dtype) -> dict:
        torch.manual_seed(SEED)
        a = torch.randn(MATMUL_N, MATMUL_N, device="cuda").to(dtype)
        b = torch.randn(MATMUL_N, MATMUL_N, device="cuda").to(dtype)
        times, i, end = [], 0, time.monotonic() + MATMUL_SECONDS  # long enough for temp and power to show the load
        while time.monotonic() < end:
            torch.cuda.synchronize()
            start = time.perf_counter()
            torch.matmul(a, b)
            torch.cuda.synchronize()
            if i >= 3:  # the first three are warmup
                times.append(time.perf_counter() - start)
            i += 1
        return {"runs": len(times), "median_s": statistics.median(times),
                "tflops": 2 * MATMUL_N ** 3 / statistics.median(times) / 1e12}

    emit({"n": MATMUL_N, "bf16": tflops(torch.bfloat16), "fp32_control": tflops(torch.float32),
          "torch": torch.__version__})


def worker_lora(s: dict):
    """A short training run with the settings in dream.py, on a fixed sample of Mio's own training data."""
    from unsloth import FastLanguageModel  # first: it patches transformers
    from unsloth.chat_templates import train_on_responses_only
    from datasets import Dataset
    from transformers import TrainerCallback
    from trl import SFTConfig, SFTTrainer

    model, tok = FastLanguageModel.from_pretrained(s["lora_weights"], max_seq_length=4608, load_in_4bit=True)
    tok = getattr(tok, "tokenizer", tok)
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=16, lora_dropout=0, bias="none", use_gradient_checkpointing="unsloth",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
    rows = [json.loads(x) for x in Path(s["lora_data"]).read_text().splitlines()]
    texts = [{"text": tok.apply_chat_template(r["messages"], tools=r["tools"], tokenize=False, enable_thinking=False)}
             for r in rows]
    texts = [t for t in texts if len(tok(t["text"]).input_ids) <= 4608]

    class Clock(TrainerCallback):
        stamps: list[float] = []

        def on_step_end(self, args, state, control, **kwargs):
            self.stamps.append(time.perf_counter())

    trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=Dataset.from_list(texts), callbacks=[Clock()], args=SFTConfig(
        dataset_text_field="text", max_seq_length=4608, per_device_train_batch_size=1,
        gradient_accumulation_steps=4, max_steps=LORA_STEPS, learning_rate=1e-4,
        warmup_steps=10, lr_scheduler_type="cosine", optim="adamw_8bit", logging_steps=10,
        output_dir=s["lora_dir"], save_strategy="no", report_to="none", seed=7))
    trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n",
                                      response_part="<|im_start|>assistant\n")
    trainer.train()
    stamps = Clock.stamps[LORA_WARMUP_STEPS:]  # the first steps include compiling and caching
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    emit({"examples": len(texts), "steps_timed": len(gaps), "median_step_s": statistics.median(gaps),
          "steps_per_s": len(gaps) / sum(gaps), "bf16_arg": trainer.args.bf16, "fp16_arg": trainer.args.fp16,
          "model_dtype": str(model.config.dtype)})


WORKERS = {"whisper": worker_whisper, "tts": worker_tts, "matmul": worker_matmul, "lora": worker_lora}


def lora_sample(path: Path):
    """Keep one fixed sample of the training data: Mio adds to data.jsonl over time, which would change the test."""
    if not path.exists():
        rows = (DREAM / "data.jsonl").read_text().splitlines()
        path.write_text("\n".join(rows[::max(1, len(rows) // LORA_EXAMPLES)][:LORA_EXAMPLES]) + "\n")


# ---- GPU log ----

class GpuLog:
    """Samples the GPU once a second during a test."""
    FIELDS = ["temperature.gpu", "power.draw", "utilization.gpu", "clocks.sm", "memory.used"]
    NAMES = ["temp_c", "power_w", "util_pct", "clock_mhz", "vram_mib"]

    def __enter__(self):
        self.samples, self.stop = [], threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return self

    def loop(self):
        while not self.stop.wait(1):
            out = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(self.FIELDS)}",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
            try:
                self.samples.append(dict(zip(self.NAMES, (float(x) for x in out.strip().split(",")))))
            except ValueError:
                pass  # a field said [N/A]

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()

    def summary(self) -> dict:
        if not self.samples:
            return {}
        busy = [x for x in self.samples if x["util_pct"] >= 50] or self.samples
        col = lambda rows, k: [x[k] for x in rows]
        return {"samples": len(self.samples), "temp_max_c": max(col(self.samples, "temp_c")),
                "temp_busy_avg_c": round(statistics.mean(col(busy, "temp_c")), 1),
                "power_max_w": max(col(self.samples, "power_w")),
                "power_busy_avg_w": round(statistics.mean(col(busy, "power_w")), 1),
                "clock_busy_avg_mhz": round(statistics.mean(col(busy, "clock_mhz"))),
                "vram_max_mib": max(col(self.samples, "vram_mib"))}


# ---- tests ----

def stop_ollama_models():
    out = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout
    for line in out.splitlines()[1:]:
        if line.strip():
            subprocess.run(["ollama", "stop", line.split()[0]], capture_output=True)
    time.sleep(2)


def test_ollama() -> dict:
    runs, prompt = [], ollama_prompt()
    for _ in range(OLLAMA_RUNS):
        stop_ollama_models()  # otherwise Ollama reuses its cached prompt and measures nothing
        p = subprocess.run(["ollama", "run", OLLAMA_MODEL, "--verbose", "--think=false", "--hidethinking",
                            "--nowordwrap"], input=prompt, capture_output=True, text=True, timeout=600)
        if p.returncode:
            raise RuntimeError(p.stderr[-500:])
        err = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", p.stderr)

        def stat(name: str) -> float:
            return float(re.search(rf"^{name}:\s+([\d.]+)", err, re.M)[1])
        runs.append({k.replace(" ", "_"): stat(k) for k in
                     ("prompt eval count", "prompt eval rate", "eval count", "eval rate")})
    stop_ollama_models()
    return {"model": OLLAMA_MODEL, "runs": runs,
            "prompt_tokens": runs[-1]["prompt_eval_count"],
            "prompt_eval_rate_median": statistics.median(r["prompt_eval_rate"] for r in runs),
            "eval_rate_median": statistics.median(r["eval_rate"] for r in runs),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}


def run_worker(name: str, python: str, settings: dict) -> dict:
    cmd = [python, *(["-I"] if python != sys.executable else []), str(Path(__file__).resolve()),
           "--worker", name, "--settings", json.dumps(settings)]
    env = dict(os.environ)
    if name == "whisper":  # as bin/assistantd: the pip-installed CUDA libs on the loader path
        import nvidia
        root = Path(list(nvidia.__path__)[0])
        env["LD_LIBRARY_PATH"] = ":".join(str(d / "lib") for d in sorted(root.iterdir()) if (d / "lib").is_dir())
    # unsloth writes its compiled cache into the working directory, so it gets the same one as dream.py
    cwd = DREAM if name == "lora" else REPO
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, cwd=cwd, env=env)
    marks = [x for x in p.stdout.splitlines() if x.startswith(RESULT_MARK)]
    if p.returncode or not marks:
        raise RuntimeError(p.stderr[-1500:] or p.stdout[-500:])
    return json.loads(marks[-1][len(RESULT_MARK):])


def active_services() -> list[str]:
    return [s for s in SERVICES if subprocess.run(["systemctl", "--user", "is-active", "--quiet", s]).returncode == 0]


def query(field: str) -> str:
    return subprocess.run(["nvidia-smi", f"--query-gpu={field}", "--format=csv,noheader"],
                          capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("label", nargs="?")
    ap.add_argument("--force", action="store_true", help="overwrite an existing result with this label")
    ap.add_argument("--worker", choices=WORKERS)
    ap.add_argument("--settings")
    a = ap.parse_args()
    if a.worker:
        if a.worker == "whisper":
            sys.path.insert(0, str(REPO))
        return WORKERS[a.worker](json.loads(a.settings))
    if not a.label or not re.fullmatch(r"[\w.-]+", a.label):
        ap.error("give a label made of letters, digits, '.', '_' or '-', such as before")

    sys.path.insert(0, str(REPO))
    from assistant.config import MODELS_DIR, STATE_DIR, load

    out_dir = STATE_DIR / "bench_unlock"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{a.label}.json"
    if out.exists() and not a.force:
        sys.exit(f"{out} already exists; pass --force to overwrite it")

    cfg = load()
    clip = out_dir / "clip.wav"
    lora_data = out_dir / "lora_data.jsonl"
    lora_sample(lora_data)
    settings = {"stt": cfg.stt, "tts": cfg.tts, "clip": str(clip), "whisper_root": str(MODELS_DIR / "whisper"),
                "lora_weights": str(DREAM / "base-qwen35"), "lora_data": str(lora_data),
                "lora_dir": str(out_dir / "lora_run")}
    result = {"label": a.label, "started": time.strftime("%FT%T"), "kernel": platform.release(),
              "cmdline": Path("/proc/cmdline").read_text().strip(), "gpu": query("name"), "driver": query("driver_version"),
              "gpu_unlock_loaded": os.path.isdir("/sys/module/gpu_unlock"),
              "power_limit": query("power.limit"), "tests": {}}

    def on_signal(signum, _):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    running = active_services()
    failed = False
    try:
        subprocess.run(["systemctl", "--user", "stop", *running], check=bool(running))
        stop_ollama_models()
        time.sleep(5)  # let the GPU drop back to idle and free its memory
        result["compute_apps_at_start"] = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name,used_memory", "--format=csv,noheader"],
            capture_output=True, text=True).stdout.strip().splitlines()
        with GpuLog() as idle:
            time.sleep(10)
        result["idle"] = idle.summary()

        # TTS runs first because it creates the fixed clip whisper transcribes on the first run
        tests = {"tts": lambda: run_worker("tts", str(TTS_PY), settings),
                 "whisper": lambda: run_worker("whisper", sys.executable, settings),
                 "ollama": test_ollama,
                 "matmul": lambda: run_worker("matmul", str(TTS_PY), settings),
                 "lora": lambda: run_worker("lora", str(DREAM / "venv/bin/python"), settings)}
        for name, fn in tests.items():
            print(f"== {name}", flush=True)
            with GpuLog() as gpu:
                try:
                    result["tests"][name] = fn()
                except Exception as e:  # noqa: BLE001 - keep the other tests' results
                    failed = True
                    result["tests"][name] = {"error": f"{type(e).__name__}: {e}"}
            result["tests"][name]["gpu"] = gpu.summary()
            time.sleep(5)
        if clip.exists():
            result["clip_sha256"] = hashlib.sha256(clip.read_bytes()).hexdigest()
    finally:
        if running:
            subprocess.run(["systemctl", "--user", "start", *running])
        result["services_restarted"] = running
        result["services_active_after"] = active_services()
        result["finished"] = time.strftime("%FT%T")
        out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    print(f"\nSaved to {out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
