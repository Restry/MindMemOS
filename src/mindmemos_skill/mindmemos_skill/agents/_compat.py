"""Shared helpers to convert Claude message blocks to OpenAI Chat Completions format.

Assistant message::
    ``role: "assistant"``
    ``content``         — all text blocks joined
    ``reasoning_content`` — all thinking blocks joined
    ``tool_calls``      — ``[{id, type: "function", function: {name, arguments}}]``

Tool result::
    ``role: "tool"``
    ``tool_call_id``
    ``content``
"""

from __future__ import annotations

import json
from typing import Any


def _collect_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )


def _collect_reasoning(blocks: list[dict[str, Any]]) -> str | None:
    parts = [b.get("thinking", "") for b in blocks if isinstance(b, dict) and b.get("type") == "thinking"]
    return "\n".join(parts) if parts else None


def _collect_tool_calls(blocks: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    calls = []
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        calls.append({
            "id": b.get("id", ""),
            "type": "function",
            "function": {
                "name": b.get("name", ""),
                "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
            },
        })
    return calls if calls else None


def convert_assistant_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a list of assistant content blocks into one OpenAI-format message."""
    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = _collect_text(blocks) or ""
    reasoning = _collect_reasoning(blocks)
    if reasoning:
        msg["reasoning_content"] = reasoning
    calls = _collect_tool_calls(blocks)
    if calls:
        msg["tool_calls"] = calls
    return msg


def convert_user_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert user-turn content blocks (tool_results / text) into OpenAI-format messages."""
    messages: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            # Separate tool result → role: tool
            messages.append({
                "role": "tool",
                "tool_call_id": b.get("tool_use_id", ""),
                "content": str(b.get("content", "")),
            })
        elif b.get("type") == "text":
            text_parts.append(b.get("text", ""))
    if text_parts:
        messages.insert(0, {"role": "user", "content": "\n".join(text_parts)})
    return messages
