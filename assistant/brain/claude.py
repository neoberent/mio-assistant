"""Claude brain, used only for requests the fast path and local model can't handle.

backend "cli": runs `claude -p` (your own Claude Code login, unmodified binary) with
  a short custom system prompt, no built-in tools, and only our MCP tool server.
backend "api": Anthropic Messages API with ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys

from ..config import PROJECT_DIR, RUNTIME, STATE_DIR
from ..tools import ToolBox

log = logging.getLogger("claude")

RULES = ("Your reply is spoken aloud by text-to-speech: at most 3 short sentences, plain words, no markdown, "
         "no lists, no code blocks. Use the desk tools to act on the user's Linux desktop (niri compositor, "
         "Noctalia shell). Act directly; the tools ask the user for confirmation when needed. "
         "If a tool says the user declined, accept it. For current facts use web_research (or read_link / "
         "read_clipboard_link), then answer from the extracts and name the sites. For the user's screen use read_screen, "
         "or look_at_screen when visuals matter.")

MCP_CONFIG = RUNTIME / "assistant-mcp.json"


class ClaudeError(Exception):
    pass


class ClaudeBrain:
    def __init__(self, cfg: dict, persona: dict, tools: ToolBox):
        self.cfg = cfg
        self.persona = persona
        self.tools = tools
        self.backend = cfg.get("claude_backend", "cli")

    def enabled(self) -> bool:
        return self.backend in ("cli", "api")

    def _system(self) -> str:
        return f"{self.persona['style']}\n{RULES}"

    async def respond(self, text: str, context: str) -> tuple[str, float]:
        from .. import clock

        context = clock.now_line() + ("\n" + context if context else "")
        prompt = f"<context>\n{context}\n</context>\n\n{text}"
        if self.backend == "api":
            return await self._api(prompt)
        return await self._cli(prompt)

    # ── claude -p ───────────────────────────────────────────────────────────
    def _write_mcp_config(self):
        MCP_CONFIG.write_text(json.dumps({"mcpServers": {"desk": {
            "command": sys.executable, "args": ["-I", "-m", "assistant.mcp_server"],
            "env": {"PYTHONPATH": str(PROJECT_DIR)}}}}))

    async def _cli(self, prompt: str) -> tuple[str, float]:
        exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        self._write_mcp_config()
        argv = [exe, "-p", prompt, "--output-format", "json", "--model", self.cfg.get("claude_model", "haiku"),
                "--system-prompt", self._system(), "--tools", "", "--mcp-config", str(MCP_CONFIG),
                "--strict-mcp-config", "--allowedTools", "mcp__desk", "--permission-mode", "dontAsk",
                "--max-turns", str(self.cfg.get("claude_max_turns", 4)), "--no-session-persistence",
                "--setting-sources", "", "--disable-slash-commands"]
        env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", DISABLE_TELEMETRY="1", MAX_THINKING_TOKENS="0")
        env.pop("ANTHROPIC_API_KEY", None)  # make sure the subscription login is used, not a stray key
        proc = await asyncio.create_subprocess_exec(*argv, cwd=STATE_DIR, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.cfg.get("claude_timeout_s", 90))
        except asyncio.TimeoutError:
            proc.kill()
            raise ClaudeError("Claude took too long")
        try:
            data = json.loads(out.decode())
        except json.JSONDecodeError:
            raise ClaudeError((err or out).decode(errors="replace").strip()[:300] or f"exit {proc.returncode}")
        if data.get("is_error"):
            raise ClaudeError(str(data.get("result") or data.get("subtype"))[:300])
        cost = float(data.get("total_cost_usd") or 0)
        log.info("claude: %s turns, $%.4f, usage=%s", data.get("num_turns"), cost, data.get("usage"))
        return (data.get("result") or "").strip(), cost

    # ── Messages API ────────────────────────────────────────────────────────
    async def _api(self, prompt: str) -> tuple[str, float]:
        import anthropic

        client = anthropic.AsyncAnthropic()
        model = self.cfg.get("claude_model", "haiku")
        model = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}.get(model, model)
        tools = [{"name": t.name, "description": t.description, "input_schema": t.schema()}
                 for t in self.tools.specs()]
        tools[-1]["cache_control"] = {"type": "ephemeral"}  # cache tool defs + system prompt
        system = [{"type": "text", "text": self._system(), "cache_control": {"type": "ephemeral"}}]
        messages = [{"role": "user", "content": prompt}]
        for _ in range(self.cfg.get("claude_max_turns", 4)):
            resp = await client.messages.create(model=model, max_tokens=400, system=system, tools=tools,
                                                messages=messages)
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text").strip(), 0.0
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    out = await self.tools.call(b.name, dict(b.input))
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages.append({"role": "user", "content": results})
        return "I had to stop there, that was getting long.", 0.0
