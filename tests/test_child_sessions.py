"""Ensure recovery never opens an independent tab for a spawned Codex agent."""

from __future__ import annotations

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
from agent_session_filters import is_child_agent, is_child_session

LIVE = runpy.run_path(str(BIN / "agent-live-sessions"))
RESTORE = runpy.run_path(str(BIN / "agent-restore-sessions"))


class ChildSessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / ".codex" / "sessions"
        self.addCleanup(patch.stopall)
        patch.dict(RESTORE["main"].__globals__, {"CODEX_SESSIONS": self.sessions}).start()

    def session_id(self, number):
        return f"00000000-0000-0000-0000-{number:012d}"

    def rollout(self, number, metadata=None, tail=""):
        session_id = self.session_id(number)
        payload = {"id": session_id, "source": "cli", "thread_source": "user", "cwd": "/root-project"}
        payload.update(metadata or {})
        path = self.sessions / "2026" / "11" / "05" / f"rollout-test-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n" + tail)
        # Match an old snapshot, before child metadata was saved to the TSV.
        return {"tool": "codex", "session_id": session_id, "session_path": str(path),
                "recoverable": "yes", "cwd": "/child-project", "name": ""}

    def child(self, number, parent_number):
        return self.rollout(number, {
            "thread_source": "subagent", "multi_agent_version": "v2",
            "parent_thread_id": self.session_id(parent_number),
        })

    def restore(self, rows, extra=(), live_ids=()):
        snapshot = self.root / "snapshot.tsv"
        LIVE["write_tsv"](snapshot, rows)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["agent-restore-sessions", "--snapshot", str(snapshot), *extra]
        namespace = RESTORE["main"].__globals__
        with patch.object(sys, "argv", argv), patch.dict(namespace, {
            "live_session_ids": lambda: set(live_ids),
        }), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = RESTORE["main"]()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_child_metadata_variants_are_not_independently_recoverable(self):
        parent = self.session_id(1)
        spawn = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        for metadata in (
            {"thread_source": "subagent"},
            {"parent_thread_id": parent, "multi_agent_version": "v2"},
            {"source": spawn},
            {"source": json.dumps(spawn)},
            {"source": {"subagent": {"other": "review"}}},
        ):
            with self.subTest(metadata=metadata):
                self.assertTrue(is_child_agent(metadata))
                self.assertFalse(RESTORE["is_recoverable"](self.rollout(2, metadata)))

    def test_old_snapshot_with_parent_and_children_opens_one_parent_tab(self):
        parent = self.rollout(1)
        parent["name"] = "Original parent title"
        first, second = self.child(2, 1), self.child(3, 1)
        with patch("subprocess.Popen") as launch:
            result, stdout, stderr = self.restore([first, parent, second, parent], ["--force"])
        self.assertEqual(result, 0)
        self.assertEqual(launch.call_count, 1)
        command = launch.call_args.args[0]
        self.assertIn("codex: Original parent title", command)
        self.assertIn("resume " + parent["session_id"], command[-1])
        self.assertNotIn(first["session_id"], command[-1])
        self.assertNotIn(second["session_id"], command[-1])
        self.assertIn("Opened 1", stdout)
        self.assertIn("child agent", stderr)

    def test_child_only_snapshot_resolves_nested_parent_before_limit(self):
        parent = self.rollout(1)
        self.child(2, 1)
        leaf = self.child(3, 2)
        sibling = self.rollout(4, {"source": json.dumps({"subagent": {
            "thread_spawn": {"parent_thread_id": parent["session_id"]},
        }})})
        result, stdout, stderr = self.restore([leaf, sibling], ["--dry-run", "--force", "--limit", "1"])
        self.assertEqual(result, 0)
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertIn("resume " + parent["session_id"], stdout)
        self.assertIn("--cd /root-project", stdout)
        self.assertNotIn(leaf["session_id"], stdout)
        self.assertNotIn(sibling["session_id"], stdout)
        self.assertIn("using parent " + parent["session_id"], stderr)

    def test_saved_child_metadata_resolves_even_without_child_file(self):
        parent = self.rollout(1)
        child = {"tool": "codex", "session_id": self.session_id(2), "thread_source": "subagent",
                 "parent_thread_id": parent["session_id"], "recoverable": "yes"}
        result, stdout, _ = self.restore([child], ["--dry-run", "--force"])
        self.assertEqual(result, 0)
        self.assertIn("resume " + parent["session_id"], stdout)
        self.assertNotIn(child["session_id"], stdout)

    def test_missing_invalid_or_cyclic_parents_never_launch_children(self):
        missing = self.child(2, 99)
        cyclic = self.child(3, 4)
        self.child(4, 3)
        self_parent = self.child(5, 5)
        no_parent = self.rollout(6, {"thread_source": "subagent", "forked_from_id": self.session_id(1)})
        broken_parent = self.rollout(7)
        Path(broken_parent["session_path"]).write_text("invalid header\n")
        broken = self.child(8, 7)
        wrong_parent = self.rollout(9, {"id": self.session_id(10)})
        mismatched = self.child(11, 9)
        guardian_parent = self.rollout(12, {"thread_source": "guardian_review"})
        guardian_child = self.child(13, 12)
        for row in (missing, cyclic, self_parent, no_parent, broken, mismatched, guardian_child):
            with self.subTest(session_id=row["session_id"]), patch("subprocess.Popen") as launch:
                with self.assertRaisesRegex(SystemExit, "no sessions selected"):
                    self.restore([row], ["--force"])
                launch.assert_not_called()

    def test_live_parent_is_skipped_after_resolving_a_child(self):
        parent = self.rollout(1)
        with patch("subprocess.Popen") as launch:
            with self.assertRaisesRegex(SystemExit, "no sessions selected"):
                self.restore([self.child(2, 1)], live_ids=[parent["session_id"]])
            launch.assert_not_called()

    def test_live_child_also_marks_its_root_as_live(self):
        parent = self.rollout(1)
        child = self.child(2, 1)
        namespace = RESTORE["live_session_ids"].__globals__

        def run(args):
            output = "123\n" if args[0] == "pgrep" else "n" + child["session_path"] + "\n"
            return subprocess.CompletedProcess(args, 0, output, "")

        with patch.dict(namespace, {"run": run, "HOME": self.root}):
            live_ids = RESTORE["live_session_ids"]()
        self.assertIn(parent["session_id"], live_ids)
        self.assertIn(child["session_id"], live_ids)

    def test_live_scan_keeps_roots_once_even_if_only_children_are_open(self):
        parent = self.rollout(1)
        child = self.child(2, 1)
        grandchild = self.child(3, 2)
        orphan = self.child(4, 99)
        guardian = self.rollout(5, {"source": {"subagent": {"other": "guardian"}}})
        namespace = LIVE["scan_codex"].__globals__
        for opened in ((child, grandchild, orphan, guardian), (parent, child, grandchild, parent)):
            with self.subTest(opened=[r["session_id"] for r in opened]), patch.dict(namespace, {
                "CODEX_SESSIONS": self.sessions,
                "pids_by_exact_name": lambda name: [123, 456],
                "proc_exists": lambda pid: True,
                "lsof_names": lambda pid: [row["session_path"] for row in opened],
                "proc_cwd": lambda pid: "/root-project",
                "ps_field": lambda pid, field: "",
            }):
                rows = LIVE["scan_codex"]()
            self.assertEqual([r["session_id"] for r in rows], [parent["session_id"]])
            self.assertEqual(rows[0]["session_path"], parent["session_path"])
            self.assertFalse(is_child_session(rows[0]))
            self.assertIn("resume " + parent["session_id"], rows[0]["resume"])

    def test_user_forks_mentions_and_claude_sessions_remain_recoverable(self):
        parent = self.rollout(1)
        user = self.rollout(2, {"forked_from_id": parent["session_id"], "session_id": parent["session_id"]},
                            json.dumps({"type": "event_msg", "payload": {"thread_source": "subagent"}}))
        user["name"] = "subagent review"
        claude = {"tool": "claude", "session_id": self.session_id(3), "thread_source": "subagent",
                  "parent_thread_id": parent["session_id"], "recoverable": "yes"}
        for row in (user, claude):
            self.assertTrue(RESTORE["is_recoverable"](row))
        selected, skipped = RESTORE["select_sessions"]([user, claude])
        self.assertEqual(selected, [user, claude])
        self.assertEqual(skipped, [])

    def test_stale_parent_path_in_snapshot_is_found_at_current_location(self):
        parent = self.rollout(1)
        child = self.child(2, 1)
        parent["session_path"] = str(self.root / "old-location" / Path(parent["session_path"]).name)
        result, stdout, _ = self.restore([parent, child], ["--dry-run", "--force"])
        self.assertEqual(result, 0)
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertIn("resume " + parent["session_id"], stdout)


if __name__ == "__main__":
    unittest.main()
