"""Regression checks for Guardian exclusion in snapshot and restore workflows."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from agent_session_filters import codex_session_meta, is_guardian_review, is_guardian_session

LIVE = runpy.run_path(str(BIN / "agent-live-sessions"))
RESTORE = runpy.run_path(str(BIN / "agent-restore-sessions"))


class GuardianSessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def rollout(self, number, metadata, tail=""):
        session_id = f"00000000-0000-0000-0000-{number:012d}"
        path = self.root / ".codex" / "sessions" / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": metadata}) + "\n" + tail)
        return {"tool": "codex", "session_id": session_id, "session_path": str(path), "recoverable": "yes"}

    def test_current_and_older_guardian_metadata(self):
        for metadata in (
            {"thread_source": "guardian_review"},
            {"source": {"subagent": {"other": "guardian"}}},
            {"thread_source": "subagent", "source": {"subagent": {"other": "guardian"}}},
            {"source": '{"subagent":{"other":"guardian"}}'},
        ):
            with self.subTest(metadata=metadata):
                self.assertTrue(is_guardian_review(metadata))
                self.assertFalse(RESTORE["is_recoverable"](self.rollout(1, metadata)))

    def test_user_sessions_remain_recoverable(self):
        for metadata in (
            {}, {"source": "cli"}, {"source": "vscode"},
            {"thread_source": "user"},
            {"source": None}, {"source": {"subagent": "guardian"}},
        ):
            with self.subTest(metadata=metadata):
                self.assertFalse(is_guardian_review(metadata))
                self.assertTrue(RESTORE["is_recoverable"](self.rollout(1, metadata)))

    def test_guardian_mentions_and_titles_do_not_exclude_user_chats(self):
        row = self.rollout(1, {"source": "cli", "thread_source": "user"},
                           json.dumps({"type": "event_msg", "payload": {"thread_source": "guardian_review"}}))
        row["name"] = "Guardian review"
        self.assertTrue(RESTORE["is_recoverable"](row))

    def test_bad_or_missing_headers_do_not_block_normal_restore(self):
        for contents in ("", "invalid json", "[]", '{"type":"session_meta","payload":[]}',
                         '{"type":"event_msg","payload":{"thread_source":"guardian_review"}}'):
            with self.subTest(contents=contents):
                path = self.root / "bad.jsonl"
                path.write_text(contents)
                self.assertEqual(codex_session_meta(str(path)), {})
        self.assertEqual(codex_session_meta(str(self.root / "missing")), {})
        self.assertTrue(RESTORE["is_recoverable"]({"tool": "codex", "session_id": "normal"}))

    def test_saved_metadata_works_without_rollout_file(self):
        row = {"tool": "codex", "session_id": "review", "thread_source": "guardian_review"}
        self.assertTrue(is_guardian_session(row))
        self.assertFalse(RESTORE["is_recoverable"](row))
        row["tool"] = "claude"
        self.assertFalse(is_guardian_session(row))
        self.assertTrue(RESTORE["is_recoverable"](row))

    def test_live_scan_filters_mixed_open_rollouts(self):
        user = self.rollout(1, {"source": "cli", "thread_source": "user"})
        current = self.rollout(2, {"thread_source": "guardian_review"})
        older = self.rollout(3, {"source": {"subagent": {"other": "guardian"}}})
        namespace = LIVE["scan_codex"].__globals__
        with patch.dict(namespace, {
            "CODEX_SESSIONS": self.root / ".codex" / "sessions",
            "pids_by_exact_name": lambda name: [123],
            "proc_exists": lambda pid: True,
            "lsof_names": lambda pid: [r["session_path"] for r in (user, current, older, user)],
            "proc_cwd": lambda pid: "/project",
            "ps_field": lambda pid, field: "",
        }):
            rows = LIVE["scan_codex"]()
        self.assertEqual([r["session_id"] for r in rows], [user["session_id"]])
        snapshot = self.root / "live.tsv"
        LIVE["write_tsv"](snapshot, rows)
        self.assertEqual(RESTORE["read_sessions"](snapshot)[0]["thread_source"], "user")

    def test_restore_old_snapshot_skips_guardian_even_with_force(self):
        # Old snapshots have neither name nor thread_source metadata.
        user = self.rollout(1, {"source": "cli"})
        guardian = self.rollout(2, {"source": {"subagent": {"other": "guardian"}}})
        snapshot = self.root / "old.tsv"
        with snapshot.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(user), delimiter="\t")
            writer.writeheader()
            writer.writerows([guardian, user])
        argv = ["agent-restore-sessions", "--snapshot", str(snapshot), "--dry-run", "--force", "--limit", "1"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(RESTORE["main"](), 0)
        self.assertIn(user["session_id"], stdout.getvalue())
        self.assertNotIn(guardian["session_id"], stdout.getvalue())
        self.assertIn(guardian["session_id"], stderr.getvalue())

    def test_guardian_only_snapshot_never_launches_a_tab(self):
        snapshot = self.root / "only.tsv"
        LIVE["write_tsv"](snapshot, [self.rollout(1, {"thread_source": "guardian_review"})])
        argv = ["agent-restore-sessions", "--snapshot", str(snapshot), "--force"]
        with patch.object(sys, "argv", argv), patch("subprocess.Popen") as launch, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "no sessions selected"):
                RESTORE["main"]()
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
