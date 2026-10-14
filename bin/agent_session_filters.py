"""Identify internal Codex approval reviews without inspecting chat content."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def codex_session_meta(path: str) -> dict[str, Any]:
    """Read the rollout header, leaving the potentially large transcript unread."""
    if not path:
        return {}
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            item = json.loads(handle.readline())
    except (OSError, ValueError):
        return {}
    if not isinstance(item, dict) or item.get("type") != "session_meta":
        return {}
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else {}


def is_guardian_review(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("thread_source") == "guardian_review":
        return True
    source = metadata.get("source")
    # A TSV or SQLite export may encode the structured source as JSON text.
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return False
    if not isinstance(source, dict):
        return False
    subagent = source.get("subagent")
    return isinstance(subagent, dict) and subagent.get("other") == "guardian"


def is_guardian_session(row: Mapping[str, Any]) -> bool:
    if row.get("tool") != "codex":
        return False
    return is_guardian_review(row) or is_guardian_review(
        codex_session_meta(row.get("session_path") or "")
    )
