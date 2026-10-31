"""Text-only copy of a vision LLM for everyday talking.

qwen3.5 ships with a ~0.9 GB vision projector that Ollama keeps in VRAM next to the weights. With Whisper and
the Chatterbox voice on the same 12 GB card that pushed ~20% of her brain onto the CPU (32 instead of 76 tok/s).
The copy shares the weight blob (no extra disk), so it costs nothing; her eyes still use the full model and the
brain is unloaded for them (actions._eyes), then reloads on the next request.
"""
from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger("textbrain")


def _run(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["ollama", *args], input=stdin, capture_output=True, text=True, timeout=120,
                          check=True).stdout


def text_only(model: str) -> str:
    """Name of a projector-free copy of `model` (made or refreshed here), or `model` itself if it has no vision
    part or anything fails."""
    try:
        modelfile = _run("show", "--modelfile", model)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("can't read %s (%s); using it as is", model, e)
        return model
    froms = re.findall(r"^FROM (\S+)", modelfile, re.M)
    if len(froms) < 2:
        return model  # already text-only
    name = model.split(":")[0] + "-text"
    try:  # up to date already? (same weights blob = same model; dreams make a new blob)
        if re.findall(r"^FROM (\S+)", _run("show", "--modelfile", name), re.M) == froms[:1]:
            return name
    except (OSError, subprocess.SubprocessError):
        pass
    lines, seen = [], 0
    for line in modelfile.splitlines():
        if line.startswith("FROM "):
            seen += 1
            if seen > 1:
                continue  # the vision projector
        lines.append(line)
    try:
        subprocess.run(["ollama", "create", name, "-f", "/dev/stdin"], input="\n".join(lines) + "\n",
                       capture_output=True, text=True, timeout=300, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("couldn't make a text-only %s (%s); using it as is", model, e)
        return model
    log.info("made %s (text-only %s, no vision projector in VRAM)", name, model)
    return name


def apply(assistant) -> None:
    """Brain → the text-only copy; eyes keep the full model."""
    if not assistant.cfg.brain.get("text_only_brain", True):
        return
    full = assistant.local.cfg["ollama_model"]
    text = text_only(full)
    if text == full:
        return
    assistant.local.cfg = dict(assistant.local.cfg, ollama_model=text)
    v = assistant.tools.vision
    eyes = v.get("vision_model") or full
    assistant.tools.vision = dict(v, ollama_model=text, vision_model=full if eyes in (full, text) else eyes)
    log.info("brain %s, eyes %s", text, assistant.tools.vision["vision_model"])
