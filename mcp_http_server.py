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

token 在面板 http://192.168.1.246:8666 →「访问令牌」页生成，每客户端一条，
可命名 / 可撤销 / 可设过期 / 分 read|write。明文只在生成那一刻显示一次。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mcp_tokens as _tok
from turn_ingest import (
    IdempotencyConflict,
    IngestError,
    IngestWorker,
    LedgerFull,
    get_default_ledger,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.getenv("MM_MCP_PORT", "8765"))
FALLBACK_HOST = os.getenv("MM_MCP_FALLBACK_HOST", f"127.0.0.1:{PORT}")
SKILL_FILE = os.path.join(HERE, "skills", "mindmemos-memory", "SKILL.md")
LLMS_FILE = os.path.join(HERE, "llms.txt")
MAX_INGEST_BODY = int(os.getenv("MM_MAX_INGEST_BODY", str(300 * 1024)))
_worker: IngestWorker | None = None

# 复用 stdio 版的工具实现，避免两份代码走偏
_spec = importlib.util.spec_from_file_location("mm_stdio", os.path.join(HERE, "mcp_server.py"))
if _spec is None or _spec.loader is None:
    sys.exit(f"找不到 mcp_server.py（应在 {HERE}）")
_mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mm)


def dispatch(req: dict, principal=None) -> dict | None:
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
            text = _mm.call_tool(
                p.get("name") or "",
                p.get("arguments") or {},
                principal=principal,
                request_id=mid,
            )
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

    def _text(self, text: str, code=200, content_type="text/plain; charset=utf-8"):
        b = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _public_origin(self) -> str:
        """根据反代传入的 Host 自动生成外网地址，不把 246 内网 IP 写死进公开文档。"""
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
        # 防止 Host header 注入到 llms.txt；只允许域名/IP/端口的安全字符。
        if not host or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]" for c in host
        ):
            host = FALLBACK_HOST
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if forwarded in ("http", "https"):
            scheme = forwarded
        else:
            # 直连内网走 http；域名入口默认按外网 HTTPS 生成。
            internal = host.startswith(("192.168.", "10.", "127.", "localhost", "[::1]"))
            scheme = "http" if internal else "https"
        return f"{scheme}://{host}"

    def _llms_txt(self) -> str:
        origin = self._public_origin()
        replacements = {
            "{{ORIGIN}}": origin,
            "{{MCP_URL}}": f"{origin}/mcp",
            "{{SKILL_URL}}": f"{origin}/skills/mindmemos-memory/SKILL.md",
            "{{TOOLS}}": "\n".join(f"- `{tool['name']}` — {tool['description']}" for tool in _mm.TOOLS),
        }
        with open(LLMS_FILE, encoding="utf-8") as handle:
            document = handle.read()
        for marker, value in replacements.items():
            document = document.replace(marker, value)
        return document

    def _bearer(self) -> str:
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()

    def _authenticate(self, required_scope: str = "read"):
        result = _tok.authenticate(self._bearer(), required_scope)
        if not result.ok:
            sys.stderr.write(f"{self.address_string()} AUTH DENY scope={required_scope} reason={result.reason}\n")
        return result.principal

    def _read_json(self, maximum: int = MAX_INGEST_BODY) -> dict | list:
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise IngestError("invalid Content-Length") from exc
        if size <= 0:
            raise IngestError("empty request body")
        if size > maximum:
            raise LedgerFull(f"request body exceeds {maximum} bytes")
        try:
            return json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc:
            raise IngestError(f"invalid JSON: {exc}") from exc

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_response(302)
            self.send_header("Location", f"{self._public_origin()}/llms.txt")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/health":
            origin = self._public_origin()
            self._json(
                {
                    "ok": True,
                    "server": "mindmemos-http",
                    "mcp": f"{origin}/mcp",
                    "llms": f"{origin}/llms.txt",
                    "skill": f"{origin}/skills/mindmemos-memory/SKILL.md",
                    "tools": [t["name"] for t in _mm.TOOLS],
                }
            )
            return
        if path == "/llms.txt":
            # 公开接入说明：不含 token，不依赖 8666 面板。
            self._text(self._llms_txt())
            return
        if path in ("/skills/mindmemos-memory/SKILL.md", "/skills/mindmemos-memory.md"):
            # canonical Agent Skill + 旧 URL 兼容入口；同一份唯一真源。
            try:
                with open(SKILL_FILE, encoding="utf-8") as f:
                    skill = f.read()
            except OSError as e:
                self._text(f"skill unavailable: {e}\n", 500)
                return
            self._text(skill, content_type="text/markdown; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path in ("/ingest/turn", "/ingest/memory"):
            principal = self._authenticate("write")
            if principal is None:
                self._json({"error": "unauthorized"}, 401)
                return
            try:
                payload = self._read_json()
                if not isinstance(payload, dict):
                    raise IngestError("ingestion payload must be an object")
                ledger = get_default_ledger()
                if path == "/ingest/turn":
                    result = ledger.submit_turn(payload, principal)
                else:
                    result = ledger.submit_memory(payload, principal, capture_mode="explicit_remember")
                if _worker is not None:
                    _worker.wake()
                self._json({"ok": True, **result.as_dict()}, 202)
            except IdempotencyConflict as exc:
                self._json({"ok": False, "error": str(exc)}, 409)
            except LedgerFull as exc:
                self._json({"ok": False, "error": str(exc)}, 507)
            except IngestError as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return

        if path != "/mcp":
            self.send_error(404)
            return

        principal = self._authenticate("read")
        if principal is None:
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            payload = self._read_json()
        except LedgerFull as exc:
            self._json({"error": str(exc)}, 413)
            return
        except IngestError as exc:
            self._json(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}},
                400,
            )
            return

        def guarded(request: dict) -> dict | None:
            """Write tools require the already-resolved credential principal."""

            if isinstance(request, dict) and request.get("method") == "tools/call":
                name = ((request.get("params") or {}).get("name")) or ""
                if name in _tok.WRITE_TOOLS and principal.scope != "write":
                    return {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {
                            "content": [{"type": "text", "text": "拒绝：当前 token 是只读的，无写入权限。"}],
                            "isError": True,
                        },
                    }
            return dispatch(request, principal=principal)

        if isinstance(payload, list):
            out = [response for response in (guarded(item) for item in payload) if response is not None]
            self._json(out if out else {"ok": True})
            return

        if not isinstance(payload, dict):
            self._json(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                400,
            )
            return
        response = guarded(payload)
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(response)


def main() -> None:
    global _worker
    ledger = get_default_ledger()
    _worker = IngestWorker(ledger, _mm._post)
    _worker.start()
    print(f"MindMemOS MCP (HTTP)  ->  http://0.0.0.0:{PORT}/mcp")
    print(f"Durable ingest       ->  http://0.0.0.0:{PORT}/ingest/turn")
    print(f"工具: {[t['name'] for t in _mm.TOOLS]}")
    print(f"Token 存储: {_tok.STORE}（面板 http://<ip>:8666 → 「访问令牌」生成）")
    print(f"Ledger: {ledger.path} states={ledger.stats()['states']}")
    print(f"已签发 {len([r for r in _tok.listing() if not r.get('revoked')])} 条有效 token")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    finally:
        _worker.stop()
        _worker = None


if __name__ == "__main__":
    main()
