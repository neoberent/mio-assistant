"""Talks to the Mio plugin inside the Riff music player (riff/mio.js).

Riff plugins run in its renderer and can't listen for connections, so the plugin long-polls a tiny HTTP server
here on 127.0.0.1: GET /mio/poll waits up to 25 s for a command, POST /mio/result sends the answer back. Both
need the token that scripts/install_riff.sh wrote into the plugin, so a web page can't drive Riff or fake answers.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .config import STATE_DIR

log = logging.getLogger("riff")

PORT = 38491
TOKEN_FILE = STATE_DIR / "riff-token"
NOT_CONNECTED = "Riff isn't connected (Riff has to be open with the Mio plugin on: scripts/install_riff.sh)"


def token() -> str:
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()


class Bridge:
    def __init__(self):
        self.token = token()
        self.commands: asyncio.Queue[dict] = asyncio.Queue()
        self.waiting: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)
        self.last_poll = 0.0
        self.polling = 0  # open poll requests

    @property
    def connected(self) -> bool:
        return self.polling > 0 or time.monotonic() - self.last_poll < 8

    async def call(self, action: str, timeout: float = 25, **args) -> dict:
        if not self.connected:
            return {"error": NOT_CONNECTED}
        cid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.waiting[cid] = fut
        await self.commands.put({"id": cid, "action": action, "args": args})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"error": "Riff didn't answer"}
        finally:
            self.waiting.pop(cid, None)

    async def serve(self):
        try:
            server = await asyncio.start_server(self._client, "127.0.0.1", PORT)
        except OSError as e:
            log.warning("riff bridge disabled: %s", e)
            return
        log.info("riff bridge on 127.0.0.1:%d", PORT)
        async with server:
            await server.serve_forever()

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode(errors="replace").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            body = await r.readexactly(min(int(headers.get("content-length") or 0), 1 << 20))
            url = urlsplit(target)
            ok_host = headers.get("host", "").split(":")[0] in ("127.0.0.1", "localhost")  # no DNS rebinding
            if not ok_host or not secrets.compare_digest(parse_qs(url.query).get("token", [""])[0], self.token):
                return await self._send(w, 403, {"error": "forbidden"})
            if method == "GET" and url.path == "/mio/poll":
                return await self._send(w, 200, await self._next())
            if method == "POST" and url.path == "/mio/result":
                msg = json.loads(body or b"{}")
                if (fut := self.waiting.get(msg.get("id"))) and not fut.done():
                    fut.set_result({k: v for k, v in msg.items() if k in ("result", "error")})
                return await self._send(w, 200, {"ok": True})
            if method == "OPTIONS":
                return await self._send(w, 204, None)
            await self._send(w, 404, {"error": "not found"})
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            pass
        finally:
            w.close()

    async def _next(self) -> dict:
        self.polling += 1
        self.last_poll = time.monotonic()
        try:
            while True:
                try:
                    cmd = await asyncio.wait_for(self.commands.get(), 25)
                except asyncio.TimeoutError:
                    return {}
                if cmd["id"] in self.waiting:  # skip commands whose caller already gave up
                    return {k: cmd[k] for k in ("id", "action", "args")}
        finally:
            self.polling -= 1
            self.last_poll = time.monotonic()

    @staticmethod
    async def _send(w: asyncio.StreamWriter, code: int, data):
        body = b"" if data is None else json.dumps(data).encode()
        w.write(f"HTTP/1.1 {code} OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Access-Control-Allow-Origin: *\r\nAccess-Control-Allow-Headers: Content-Type\r\n"
                "Connection: close\r\n\r\n".encode() + body)
        await w.drain()


bridge: Bridge | None = None  # started by the daemon


async def call(action: str, **args) -> dict:
    """{"result": …} or {"error": "…"}."""
    if bridge is None:
        return {"error": NOT_CONNECTED}
    return await bridge.call(action, **args)


def connected() -> bool:
    return bridge is not None and bridge.connected
