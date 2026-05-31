"""Shared helpers for ZeroGraph agents."""

from __future__ import annotations

import json
from typing import Any


def extract_tool_payload(result: Any) -> Any:
    if hasattr(result, "content") and result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if isinstance(result, dict):
        return result
    return str(result)


def payload_to_text(payload: Any, max_chars: int = 16_000) -> str:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, indent=2, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n… [{len(text) - max_chars} chars truncated]"
    return text
