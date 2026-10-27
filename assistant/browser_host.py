"""Native messaging host for Mio's browser extension. Stdlib only; Brave starts it for the extension.

  extension ⇄ (stdin/stdout, length-prefixed JSON) ⇄ this host ⇄ $XDG_RUNTIME_DIR/mio-browser.sock ⇄ assistantd

The daemon connects to the socket per request and sends {"action", "args"}; the host forwards it to the
extension with an id and writes the reply back. Messages the extension starts itself (the popup's "ask Mio")
are passed to the daemon's own socket. The host lives as long as the extension keeps its port open.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import struct
import sys

from assistant.config import DAEMON_SOCK, RUNTIME

SOCK = RUNTIME / "mio-browser.sock"
TIMEOUT = 20


class Host:
    def __init__(self):
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)
        self.out = sys.stdout.buffer

    def send(self, msg: dict):
        data = json.dumps(msg).encode()
        self.out.write(struct.pack("<I", len(data)) + data)
        self.out.flush()

    async def read_extension(self):
        """Messages from the extension: replies to our requests, or its own requests."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while True:
            try:
                (size,) = struct.unpack("<I", await reader.readexactly(4))
                msg = json.loads(await reader.readexactly(size))
            except (asyncio.IncompleteReadError, struct.error):
                return  # the browser closed the port
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            elif msg.get("type") == "ask":
                asyncio.create_task(self.to_daemon({"cmd": "ask", "text": str(msg.get("text", ""))[:2000]}))
            elif msg.get("type") == "status":
                asyncio.create_task(self.status(msg.get("rid")))

    async def to_daemon(self, req: dict) -> dict:
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(DAEMON_SOCK)), 2)
            w.write((json.dumps(req) + "\n").encode())
            await w.drain()
            resp = json.loads(await asyncio.wait_for(r.readline(), 5) or b"{}")
            w.close()
            return resp
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
            return {"error": "Mio isn't running"}

    async def status(self, rid):
        resp = await self.to_daemon({"cmd": "status"})
        self.send({"type": "status", "rid": rid, "mio": "error" not in resp, "state": resp.get("state")})

    async def handle_daemon(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while line := await reader.readline():
                req = json.loads(line)
                rid = next(self.ids)
                fut = asyncio.get_running_loop().create_future()
                self.pending[rid] = fut
                self.send({"id": rid, "action": req.get("action"), "args": req.get("args") or {}})
                try:
                    resp = await asyncio.wait_for(fut, TIMEOUT)
                except asyncio.TimeoutError:
                    self.pending.pop(rid, None)
                    resp = {"error": "the browser didn't answer in time"}
                resp.pop("id", None)
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionError, json.JSONDecodeError):
            pass
        finally:
            writer.close()

    async def run(self):
        if SOCK.exists():
            SOCK.unlink()  # one browser at a time; a newer window takes over
        server = await asyncio.start_unix_server(self.handle_daemon, path=str(SOCK))
        os.chmod(SOCK, 0o600)
        mine = SOCK.stat().st_ino
        self.send({"type": "hello", "version": 1})
        try:
            await self.read_extension()
        finally:
            server.close()
            try:
                if SOCK.stat().st_ino == mine:  # don't remove a newer window's socket
                    SOCK.unlink()
            except FileNotFoundError:
                pass


def main():
    asyncio.run(Host().run())


if __name__ == "__main__":
    main()
