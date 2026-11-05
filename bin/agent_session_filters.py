"""Identify internal Codex threads and resolve spawned agents to their root."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Iterable, Mapping


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


def codex_source(metadata: Mapping[str, Any]) -> dict[str, Any]:
    source = metadata.get("source")
    # A TSV or SQLite export may encode the structured source as JSON text.
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return {}
    return source if isinstance(source, dict) else {}


def is_guardian_review(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("thread_source") == "guardian_review":
        return True
    subagent = codex_source(metadata).get("subagent")
    return isinstance(subagent, dict) and subagent.get("other") == "guardian"


def is_guardian_session(row: Mapping[str, Any]) -> bool:
    if row.get("tool") != "codex":
        return False
    return is_guardian_review(row) or is_guardian_review(
        codex_session_meta(row.get("session_path") or "")
    )


def codex_parent_thread_id(metadata: Mapping[str, Any]) -> str:
    parent = metadata.get("parent_thread_id")
    if isinstance(parent, str) and parent:
        return parent
    subagent = codex_source(metadata).get("subagent")
    if isinstance(subagent, dict):
        spawn = subagent.get("thread_spawn")
        if isinstance(spawn, dict):
            parent = spawn.get("parent_thread_id")
            if isinstance(parent, str):
                return parent
    return ""


def is_child_agent(metadata: Mapping[str, Any]) -> bool:
    # forked_from_id alone also occurs on ordinary user forks. It does not
    # establish that a conversation is a spawned agent.
    return (
        metadata.get("thread_source") == "subagent"
        or isinstance(codex_source(metadata).get("subagent"), dict)
        or bool(codex_parent_thread_id(metadata))
    )


def is_child_session(row: Mapping[str, Any]) -> bool:
    if row.get("tool") != "codex":
        return False
    return is_child_agent(row) or is_child_agent(
        codex_session_meta(row.get("session_path") or "")
    )


class CodexSessionResolver:
    """Find a child's root using rollout headers, never transcript contents."""

    def __init__(self, sessions_dir: Path, rows: Iterable[Mapping[str, Any]] = ()):
        self.sessions_dir = sessions_dir
        self.paths = {
            str(row["session_id"]).lower(): Path(row["session_path"])
            for row in rows
            if row.get("tool") == "codex" and row.get("session_id") and row.get("session_path")
        }
        self.indexed = False

    def session_path(self, session_id: str) -> Path | None:
        key = session_id.lower()
        path = self.paths.get(key)
        if path is not None and path.is_file():
            return path
        if not self.indexed:
            # Index names once, lazily; read headers only along parent chains.
            for candidate in self.sessions_dir.rglob("rollout-*.jsonl"):
                candidate_id = candidate.stem[-36:].lower()
                known = self.paths.get(candidate_id)
                if known is None or not known.is_file():
                    self.paths[candidate_id] = candidate
            self.indexed = True
        path = self.paths.get(key)
        return path if path is not None and path.is_file() else None

    def root_session(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        if is_guardian_session(row):
            return None
        metadata = codex_session_meta(row.get("session_path") or "")
        if not is_child_agent(metadata):
            metadata = dict(row)
        if row.get("tool") != "codex" or not is_child_agent(metadata):
            return dict(row)

        seen = {str(row.get("session_id", "")).lower()}
        while is_child_agent(metadata):
            parent = codex_parent_thread_id(metadata).lower()
            if not parent or parent in seen:
                return None
            seen.add(parent)
            path = self.session_path(parent)
            if path is None:
                return None
            metadata = codex_session_meta(str(path))
            if not metadata or str(metadata.get("id", parent)).lower() != parent:
                return None
            if is_guardian_review(metadata):
                return None

        result = dict(row)
        cwd = metadata.get("cwd") or row.get("cwd") or ""
        resume = f"codex resume {shlex.quote(parent)}"
        result.update(
            session_id=parent,
            session_path=str(path),
            cwd=cwd,
            name="",
            thread_source=metadata.get("thread_source") or "",
            parent_thread_id="",
            source=metadata.get("source"),
            recoverable="yes",
            resume=f"cd {shlex.quote(cwd)} && {resume}" if cwd else resume,
        )
        return result
