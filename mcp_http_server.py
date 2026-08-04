#!/usr/bin/env python3
"""MindMemOS 远程 MCP server（Streamable HTTP transport）。

跟 mcp_server.py 的区别：
  - mcp_server.py  = stdio，每台客户端机器都要有这个脚本文件
  - 本文件         = HTTP，只在这台机器跑，别的电脑填个 URL 就能用

工具逻辑直接复用 mcp_server.py，不重复实现——那边改了这边自动跟上。

启动：
    python3 mcp_http_server.py            # 监听 0.0.0.0:8765

客户端配置（omp / Claude Code 等）：
    {"mcpServers": {"mindmemos": {
        "type": "http",
        "url": "http://192.168.1.246:8765/mcp",
        "headers": {"Authorization": "Bearer <token>"}
    }}}

token 见 ~/.hermes/mindmemos_mcp_token（首次启动自动生成）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.getenv("MM_MCP_PORT", "8765"))
TOKEN_FILE = os.path.expanduser("~/.hermes/mindmemos_mcp_token")

# 复用 stdio 版的工具实现，避免两份代码走偏
_spec = importlib.util.spec_from_file_location("mm_stdio", os.path.join(HERE, "mcp_server.py"))
if _spec is None or _spec.loader is None:
    sys.exit(f"找不到 mcp_server.py（应在 {HERE}）")
_mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mm)


def load_token() -> str:
    """局域网也别裸奔，用一个固定 token 做最基本的门禁。"""
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t)
    os.chmod(TOKEN_FILE, 0o600)
    return t


TOKEN = load_token()


def dispatch(req: dict) -> dict | None:
    """处理一条 JSON-RPC 消息，返回响应；notification 返回 None。"""
    method = req.get("method")
    mid = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mindmemos-http", "version": "1.0.0"},
                # 复用 stdio 版的同一份，别在这里复制一份——
                # 两边各写一份迟早会不同步，远程 agent 就拿到过期的行为准则。
                "instructions": _mm.SERVER_INSTRUCTIONS,
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in _mm.TOOLS]},
        }

    if method == "tools/call":
        p = req.get("params") or {}
        tool = _mm._BY_NAME.get(p.get("name"))
        if tool is None:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": f"未知工具：{p.get('name')}"}], "isError": True},
            }
        try:
            text = tool["handler"](p.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": f"出错：{type(e).__name__}: {e}"}], "isError": True},
            }

    if mid is None:
        return None  # notification，不用回

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        sys.stderr.write(f"{self.address_string()} {format % args}\n")

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self) -> bool:
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return secrets.compare_digest(got, TOKEN)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._json({"ok": True, "server": "mindmemos-http", "tools": [t["name"] for t in _mm.TOOLS]})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path.split("?")[0] != "/mcp":
            self.send_error(404)
            return
        if not self._authed():
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {e}"}}, 400)
            return

        # 支持批量请求
        if isinstance(payload, list):
            out = [r for r in (dispatch(x) for x in payload) if r is not None]
            self._json(out if out else {"ok": True})
            return

        resp = dispatch(payload)
        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(resp)


def main() -> None:
    print(f"MindMemOS MCP (HTTP)  ->  http://0.0.0.0:{PORT}/mcp")
    print(f"工具: {[t['name'] for t in _mm.TOOLS]}")
    print(f"Token 存放于 {TOKEN_FILE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
