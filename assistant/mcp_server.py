"""Minimal stdio MCP server that exposes the daemon's tools to `claude -p`.

Spawned by Claude Code per request. It holds no logic: tool listing and tool calls
are forwarded to assistantd over its unix socket, so the permission gate, the
confirmations and the audit log all live in one place. Stdlib only, starts fast.
"""
from __future__ import annotations

import json
import socket
import sys

from assistant.config import DAEMON_SOCK


def daemon(req: dict, timeout: float = 120) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(DAEMON_SOCK))
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = {"code": -32603, "message": error}
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, id_ = msg.get("method"), msg.get("id")
        if id_ is None:
            continue  # notification
        try:
            if method == "initialize":
                reply(id_, {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "desk", "version": "0.1"}})
            elif method == "tools/list":
                reply(id_, {"tools": daemon({"cmd": "tools"})["tools"]})
            elif method == "tools/call":
                p = msg["params"]
                r = daemon({"cmd": "tool", "name": p["name"], "args": p.get("arguments") or {}})
                text = str(r.get("result", ""))
                content = []
                if text.startswith("__image__:"):  # screenshot for Claude's vision
                    path, _, text = text[len("__image__:"):].partition("\n")
                    import base64
                    with open(path, "rb") as f:
                        content.append({"type": "image", "mimeType": "image/jpeg",
                                        "data": base64.b64encode(f.read()).decode()})
                content.append({"type": "text", "text": text})
                reply(id_, {"content": content, "isError": text.startswith("error")})
            elif method == "ping":
                reply(id_, {})
            else:
                reply(id_, error=f"unsupported method {method}")
        except Exception as e:
            reply(id_, error=str(e))


if __name__ == "__main__":
    main()
