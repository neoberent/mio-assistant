"""Chatterbox Turbo speech worker. Runs in its own venv (scripts/install_chatterbox.sh) so torch stays out of Mio's.

Protocol (stdin/stdout, one request at a time):
  request:  one JSON line {"text": "...", "voice": "/path/ref.wav" | null}
  reply:    one JSON line {"sr": 24000, "n": <samples>} or {"error": "..."}, then n float32 samples (little endian)
The first line written is {"ready": true} once the model is loaded.
Self-contained on purpose: started with `python -I`, so it can't import the assistant package.
"""
import json
import logging
import os
import sys
import warnings


def main():
    out = os.fdopen(os.dup(1), "wb", buffering=0)  # protocol channel
    os.dup2(2, 1)  # anything the libraries print goes to stderr (the journal), not into the protocol
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)

    import numpy as np
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    import contextlib

    device = sys.argv[1] if len(sys.argv) > 1 else "cuda"
    half = len(sys.argv) > 2 and sys.argv[2] == "half"
    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    half = half and device == "cuda"

    def load():
        m = ChatterboxTurboTTS.from_pretrained(device=device)
        if half:  # the speech LM in fp16: 2.85 → ~2.0 GB VRAM, sounds the same; autocast handles mixed inputs
            m.t3.half()
            torch.cuda.empty_cache()
        return m

    model = load()
    precision = (lambda: torch.autocast("cuda", dtype=torch.float16)) if half else contextlib.nullcontext
    voice = None

    def send(obj, data=b""):
        out.write(json.dumps(obj).encode() + b"\n" + data)

    send({"ready": True, "device": device + (" fp16" if half else "")})
    for line in sys.stdin:
        try:
            req = json.loads(line)
            ref = req.get("voice") or None
            if ref != voice:
                if ref:
                    model.prepare_conditionals(ref)
                else:  # back to the built-in voice
                    model = load()
                voice = ref
            with torch.inference_mode(), precision():
                wav = model.generate(req["text"])
            a = wav.squeeze(0).float().cpu().numpy().astype("<f4")
            send({"sr": model.sr, "n": int(a.size)}, a.tobytes())
        except Exception as e:  # noqa: BLE001 - report every failure to the daemon, keep serving
            send({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
