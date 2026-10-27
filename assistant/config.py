"""Configuration and well-known paths."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_DIR / "config" / "config.toml"

XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
XDG_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))

CONFIG_DIR = XDG_CONFIG / "assistant"
CONFIG_FILE = CONFIG_DIR / "config.toml"
MODELS_DIR = XDG_DATA / "assistant" / "models"
STATE_DIR = XDG_STATE / "assistant"
AUDIT_LOG = STATE_DIR / "audit.log"
MEMORY_FILE = STATE_DIR / "memory.json"
USAGE_FILE = STATE_DIR / "usage.json"
ESCALATIONS = STATE_DIR / "escalations.jsonl"

DAEMON_SOCK = RUNTIME / "assistant.sock"
AVATAR_SOCK = RUNTIME / "assistant-avatar.sock"


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    def section(self, name: str) -> dict:
        return self.raw.get(name, {})

    def __getattr__(self, name: str) -> dict:
        if name.startswith("_") or name == "raw":
            raise AttributeError(name)
        return self.section(name)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> Config:
    """Defaults from the repo, overridden by ~/.config/assistant/config.toml."""
    data = tomllib.loads(DEFAULT_CONFIG.read_text())
    if CONFIG_FILE.exists():
        data = _merge(data, tomllib.loads(CONFIG_FILE.read_text()))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return Config(data)
