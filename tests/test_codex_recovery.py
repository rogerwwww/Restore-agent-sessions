"""Recover Codex conversations across writer changes and daemon disconnects."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import agent_session_filters as filters

LIVE = runpy.run_path(str(BIN / "agent-live-sessions"))
RESTORE = runpy.run_path(str(BIN / "agent-restore-sessions"))


class CodexRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / ".codex" / "sessions"

    def sid(self, number):
        return f"00000000-0000-0000-0000-{number:012d}"

    def rollout(self, number, writer=None, **metadata):
        sid = self.sid(number)
        suffix = f"_{self.sid(writer)}" if writer else ""
        path = self.sessions / f"rollout-test-{sid}{suffix}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"id": sid, "session_id": sid, "cwd": f"/project-{number}", "thread_source": "user"}
        payload.update(metadata)
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
        return path

    def scan(self, opened, resume_id=""):
        with patch.dict(LIVE["scan_codex"].__globals__, {
            "CODEX_SESSIONS": self.sessions,
            "pids_by_exact_name": lambda name: [123],
            "proc_exists": lambda pid: True,
            "lsof_names": lambda pid: [str(p) for p in opened],
            "proc_cwd": lambda pid: "/daemon-project",
            "ps_field": lambda pid, field: "",
        }), patch.object(filters, "codex_resume_id", return_value=resume_id):
            return LIVE["scan_codex"]()

    def test_two_uuid_filename_uses_header_identity_and_project(self):
        path = self.rollout(1, writer=2)
        row, = self.scan([path])
        self.assertEqual(row["session_id"], self.sid(1))
        self.assertEqual(row["cwd"], "/project-1")
        self.assertIn(self.sid(1), row["resume"])
        self.assertNotIn(self.sid(2), row["resume"])

    def test_filename_fallback_uses_first_uuid_and_header_id_takes_precedence(self):
        path = self.rollout(1, writer=2, session_id=self.sid(3))
        self.assertEqual(filters.codex_session_id(str(path)), self.sid(1))
        path.write_text("bad header\n")
        self.assertEqual(filters.codex_session_id(str(path)), self.sid(1))

    def test_old_writer_id_and_bad_directory_are_corrected_and_deduplicated(self):
        path = self.rollout(1, writer=2)
        rows = [{"tool": "codex", "session_id": self.sid(n), "session_path": str(path),
                 "cwd": "/daemon-project", "recoverable": "yes"} for n in (1, 2)]
        selected, skipped = RESTORE["select_sessions"](rows)
        self.assertEqual(skipped, [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["session_id"], self.sid(1))
        self.assertEqual(selected[0]["cwd"], "/project-1")

    def test_parent_with_writer_suffix_is_found_by_its_thread_id(self):
        parent = self.rollout(1, writer=2)
        child = self.rollout(3, thread_source="subagent", parent_thread_id=self.sid(1))
        row, = self.scan([child])
        self.assertEqual(row["session_path"], str(parent))
        self.assertEqual(row["session_id"], self.sid(1))

    def test_explicit_resume_client_survives_missing_rollout_fd(self):
        self.rollout(1, writer=2)
        row, = self.scan([], resume_id=self.sid(1))
        self.assertEqual(row["session_id"], self.sid(1))
        self.assertEqual(row["cwd"], "/project-1")

    def test_open_runtime_rollout_precedes_old_resume_argument(self):
        self.rollout(1)
        current = self.rollout(2)
        row, = self.scan([current], resume_id=self.sid(1))
        self.assertEqual(row["session_id"], self.sid(2))

    def test_open_writer_lock_recovers_but_stale_lock_on_disk_does_not(self):
        self.rollout(1)
        lock = self.sessions.parent / "thread-writer-locks" / f"{self.sid(1)}.lock"
        lock.parent.mkdir()
        lock.touch()
        row, = self.scan([lock])
        self.assertEqual(row["session_id"], self.sid(1))
        self.assertEqual(self.scan([]), [])

    def test_fallback_still_filters_guardians_and_resolves_children(self):
        self.rollout(1)
        self.rollout(2, thread_source="subagent", parent_thread_id=self.sid(1))
        self.rollout(3, thread_source="guardian_review")
        row, = self.scan([], resume_id=self.sid(2))
        self.assertEqual(row["session_id"], self.sid(1))
        self.assertEqual(self.scan([], resume_id=self.sid(3)), [])
        self.assertEqual(self.scan([], resume_id=self.sid(99)), [])

    def test_resume_argument_requires_explicit_interactive_target(self):
        for argv, expected in (
            (["codex", "resume", self.sid(1)], self.sid(1)),
            (["codex", "app-server", "--token", self.sid(1)], ""),
            (["codex", "exec", "resume", self.sid(1)], ""),
            (["codex", "resume", "--last", self.sid(1)], ""),
            (["codex", self.sid(1)], ""),
        ):
            with self.subTest(argv=argv), patch.object(Path, "read_bytes", return_value="\0".join(argv).encode()):
                self.assertEqual(filters.codex_resume_id(123), expected)

    def test_partial_lsof_output_is_kept_in_scan_and_already_live_check(self):
        path = self.rollout(1, writer=2)

        def run(args):
            if args[0] == "pgrep":
                return subprocess.CompletedProcess(args, 0, "123\n", "")
            return subprocess.CompletedProcess(args, 1, f"p123\nn{path}\n", "process disappeared")

        with patch.dict(LIVE["lsof_names"].__globals__, {"run": run}):
            self.assertEqual(LIVE["lsof_names"](123), [str(path)])
        with patch.dict(RESTORE["live_session_ids"].__globals__, {
            "run": run, "HOME": self.root, "CODEX_SESSIONS": self.sessions,
        }):
            self.assertEqual(RESTORE["live_session_ids"](), {self.sid(1)})

    def test_already_live_check_uses_resume_fallback(self):
        self.rollout(1, writer=2)

        def run(args):
            return subprocess.CompletedProcess(args, 0, "123\n" if args[0] == "pgrep" else "", "")

        with patch.dict(RESTORE["live_session_ids"].__globals__, {
            "run": run, "HOME": self.root, "CODEX_SESSIONS": self.sessions,
        }), patch.object(filters, "codex_resume_id", return_value=self.sid(1)):
            self.assertEqual(RESTORE["live_session_ids"](), {self.sid(1)})

    def history(self, records):
        path = self.root / "history.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n{partial")
        return path

    def test_history_selects_complete_snapshot_before_outage_without_union(self):
        before = [{"tool": "codex", "session_id": self.sid(1)}]
        after = [{"tool": "claude", "session_id": self.sid(2)}]
        path = self.history([
            {"scanned_at": "2026-11-10T11:25:01-05:00", "sessions": before},
            {"scanned_at": "2026-11-10T11:30:01-05:00", "sessions": after},
            {"scanned_at": 123, "sessions": []},
            [],
        ])
        at = RESTORE["parse_timestamp"]("2026-11-10T16:25:01Z")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RESTORE["read_history"](path, at), before)
            self.assertEqual(RESTORE["read_history"](path, RESTORE["parse_timestamp"]("2026-11-10T17:00:00Z")), after)
        with self.assertRaisesRegex(SystemExit, "no historical snapshot"):
            RESTORE["read_history"](path, RESTORE["parse_timestamp"]("2026-11-09T00:00:00Z"))

    def test_history_can_select_empty_snapshot_and_requires_timezone(self):
        path = self.history([{"scanned_at": "2026-11-10T11:30:01-05:00", "sessions": []}])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RESTORE["read_history"](path, RESTORE["parse_timestamp"]("2026-11-11T00:00:00Z")), [])
        with self.assertRaises(argparse.ArgumentTypeError):
            RESTORE["parse_timestamp"]("2026-11-10T11:30:01")

    def test_history_cli_corrects_writer_id_and_preserves_force_filtering(self):
        path = self.rollout(1, writer=2)
        guardian = self.rollout(3, thread_source="guardian_review")
        history = self.history([{"scanned_at": "2026-11-10T11:25:01-05:00", "sessions": [
            {"tool": "codex", "session_id": self.sid(2), "session_path": str(path), "cwd": "/wrong"},
            {"tool": "codex", "session_id": self.sid(3), "session_path": str(guardian)},
        ]}])
        argv = ["restore", "--history", str(history), "--at", "2026-11-10T16:26:00Z", "--tool", "codex", "--dry-run", "--force"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(RESTORE["main"](), 0)
        self.assertIn(self.sid(1), stdout.getvalue())
        self.assertIn("--cd /project-1", stdout.getvalue())
        self.assertNotIn(self.sid(2), stdout.getvalue())
        self.assertNotIn(self.sid(3), stdout.getvalue())
        self.assertIn("Guardian review", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
