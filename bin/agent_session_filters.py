"""Identify internal Codex threads and resolve spawned agents to their root."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Iterable, Mapping


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


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


def codex_session_id(path: str, metadata: Mapping[str, Any] | None = None) -> str:
    """A rollout may end in a writer UUID; its header owns the thread ID."""
    if metadata is None:
        metadata = codex_session_meta(path)
    for key in ("id", "session_id"):
        value = metadata.get(key)
        if isinstance(value, str) and UUID_RE.fullmatch(value):
            return value.lower()
    name = Path(path).name
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        match = UUID_RE.search(name)
        if match:
            return match.group().lower()
    return ""


def codex_resume_id(pid: int) -> str:
    """Read an explicit CLI resume target when a daemon owns the rollout FD."""
    try:
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode().split("\0")
    except (OSError, UnicodeError):
        return ""
    # Ignore app-server/exec processes and UUIDs in prompts or other arguments.
    if len(argv) > 2 and argv[1] == "resume" and UUID_RE.fullmatch(argv[2]):
        return argv[2].lower()
    return ""


def codex_process_paths(
    pid: int, names: Iterable[str], resolver: CodexSessionResolver,
) -> list[str]:
    """Use open rollouts first, then held writer locks or an explicit resume ID."""
    names = tuple(names)
    paths = {
        name for name in names
        if Path(name).parent.is_relative_to(resolver.sessions_dir)
        and Path(name).name.startswith("rollout-") and name.endswith(".jsonl")
    }
    if not paths:
        ids = {
            Path(name).stem for name in names
            if Path(name).parent == resolver.sessions_dir.parent / "thread-writer-locks"
            and name.endswith(".lock") and UUID_RE.fullmatch(Path(name).stem)
        }
        resume_id = codex_resume_id(pid)
        if resume_id:
            ids.add(resume_id)
        for session_id in ids:
            path = resolver.session_path(session_id)
            if path is not None:
                paths.add(str(path))
    return sorted(paths)


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
                # Index both thread and writer IDs to repair older snapshots.
                for candidate_id in UUID_RE.findall(candidate.name):
                    candidate_id = candidate_id.lower()
                    known = self.paths.get(candidate_id)
                    if known is None or not known.is_file():
                        self.paths[candidate_id] = candidate
            self.indexed = True
        path = self.paths.get(key)
        return path if path is not None and path.is_file() else None

    def root_session(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        if row.get("tool") != "codex":
            return dict(row)
        if is_guardian_session(row):
            return None
        row = dict(row)
        metadata = codex_session_meta(row.get("session_path") or "")
        session_id = codex_session_id(row.get("session_path") or "", metadata)
        if metadata and session_id:
            cwd = metadata.get("cwd") or row.get("cwd") or ""
            resume = f"codex resume {shlex.quote(session_id)}"
            row.update(session_id=session_id, cwd=cwd,
                       resume=f"cd {shlex.quote(cwd)} && {resume}" if cwd else resume)
        if not is_child_agent(metadata):
            metadata = dict(row)
        if not is_child_agent(metadata):
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
