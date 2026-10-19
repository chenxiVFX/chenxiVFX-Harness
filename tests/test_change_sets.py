from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cvfx_harness.cli import _confirm_action, _completion_matches, build_parser, run_console
from cvfx_harness.providers import ToolCall
from cvfx_harness.tools import (
    CHANGE_SET_BACKUP_SCAN_LIMIT,
    CHANGE_SET_JOURNAL_SCAN_LIMIT,
    ActionRequest,
    Workspace,
    WorkspaceConflictError,
    WorkspaceError,
    run_tool,
)

from test_core import _set_windows_dacl_protection, cli_subprocess_env, make_test_temp


class ChangeSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = make_test_temp(prefix="change-set-")
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.sources = {
            "a.txt": "中文段落甲\n旧值甲\n",
            "b.txt": "中文段落乙🙂\n旧值乙\n",
            "c.txt": "中文段落丙\n旧值丙\n",
        }
        for relative, content in self.sources.items():
            (self.root / relative).write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def changes(self, names: tuple[str, ...] = ("b.txt", "a.txt")) -> list[dict[str, str]]:
        changes: list[dict[str, str]] = []
        replacements = {"a.txt": ("旧值甲", "新值甲"), "b.txt": ("旧值乙", "新值乙"), "c.txt": ("旧值丙", "新值丙")}
        for relative in names:
            source = (self.root / relative).read_bytes()
            old_text, new_text = replacements[relative]
            changes.append({
                "path": relative,
                "expected_sha256": hashlib.sha256(source).hexdigest(),
                "old_text": old_text,
                "new_text": new_text,
            })
        return changes

    @staticmethod
    def approval(requests: list[ActionRequest], answer: bool = True):
        def callback(request: ActionRequest) -> bool:
            requests.append(request)
            return answer

        return callback

    def call(self, changes: list[dict[str, str]]) -> ToolCall:
        return ToolCall("change-set-call", "apply_change_set", {"changes": changes})

    def make_interrupted_fully_applied_set(self) -> str:
        return self.make_interrupted_set_after_commit(2)

    def make_interrupted_set_after_commit(self, kill_after_commit: int) -> str:
        script = r'''
import hashlib, os, sys
from pathlib import Path
from cvfx_harness.tools import Workspace

root = Path(sys.argv[1])
kill_after_commit = int(sys.argv[2])
workspace = Workspace(root)
changes = []
for name, old, new in (("a.txt", "旧值甲", "新值甲"), ("b.txt", "旧值乙", "新值乙")):
    data = (root / name).read_bytes()
    changes.append({"path": name, "expected_sha256": hashlib.sha256(data).hexdigest(), "old_text": old, "new_text": new})
real_commit = Workspace.commit_write
calls = 0
def kill_after_nth(self, preview, **kwargs):
    global calls
    backup = real_commit(self, preview, **kwargs)
    calls += 1
    if calls == kill_after_commit:
        os._exit(75)
    return backup
Workspace.commit_write = kill_after_nth
workspace.apply_change_set(changes, approve_action=lambda _request: True)
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root), str(kill_after_commit)],
            cwd=ROOT,
            env=cli_subprocess_env(str(self.root)),
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 75, result.stderr.decode("utf-8", "replace"))
        records = self.workspace.list_incomplete_change_sets()
        self.assertEqual(len(records), 1)
        return records[0]["id"]

    def test_preview_uses_one_approval_for_the_complete_sorted_set(self) -> None:
        original_a_sha = hashlib.sha256((self.root / "a.txt").read_bytes()).hexdigest()
        requests: list[ActionRequest] = []
        result = run_tool(
            self.workspace,
            self.call(self.changes()),
            approve_action=self.approval(requests),
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].operation, "change_set")
        self.assertLess(requests[0].diff.index("文件：a.txt"), requests[0].diff.index("文件：b.txt"))
        self.assertIn(original_a_sha[:16] + "…", requests[0].diff)
        self.assertIn("完整差异指纹", requests[0].diff)
        self.assertIn("不是原子事务", requests[0].diff)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "中文段落甲\n新值甲\n")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "中文段落乙🙂\n新值乙\n")
        record = self.workspace._load_change_set_record(requests[0].path)
        self.assertEqual(record["state"], "completed")
        self.assertTrue(all(item["status"] == "committed" for item in record["files"]))
        backups = [data for _path, data in self.workspace._load_backups() if data.get("change_set_id") == requests[0].path]
        self.assertEqual(len(backups), 2)
        self.assertTrue(all(data["before_exists"] is True for data in backups))
        self.assertEqual(self.workspace.describe_incomplete_change_sets(), "没有未完成的多文件变更集。")

    def test_terminal_journal_cannot_hide_contradictory_file_states(self) -> None:
        requests: list[ActionRequest] = []
        self.workspace.apply_change_set(self.changes(), approve_action=self.approval(requests))
        journal = self.root / ".harness" / "change-sets" / f"{requests[0].path}.json"
        original = json.loads(journal.read_text(encoding="utf-8"))
        cases = (
            ("completed", "applying"),
            ("completed", "pending"),
            ("rolled_back", "committed"),
            ("recovered", "committed"),
        )
        for state, status in cases:
            with self.subTest(state=state, status=status):
                contradicted = json.loads(json.dumps(original))
                contradicted["state"] = state
                contradicted["files"][0]["status"] = status
                journal.write_text(json.dumps(contradicted), encoding="utf-8")
                with self.assertRaisesRegex(WorkspaceError, "终态与文件状态矛盾"):
                    self.workspace.describe_incomplete_change_sets()
        journal.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.workspace.describe_incomplete_change_sets(), "没有未完成的多文件变更集。")

    def test_corrupt_escaped_and_linked_journals_remain_fail_closed(self) -> None:
        requests: list[ActionRequest] = []
        self.workspace.apply_change_set(self.changes(), approve_action=self.approval(requests))
        journal = self.root / ".harness" / "change-sets" / f"{requests[0].path}.json"
        original = json.loads(journal.read_text(encoding="utf-8"))

        journal.write_bytes(b"{broken json")
        with self.assertRaisesRegex(WorkspaceError, "损坏或无法读取"):
            self.workspace.describe_incomplete_change_sets()

        escaped = json.loads(json.dumps(original))
        escaped["files"][0]["path"] = "../outside.txt"
        journal.write_text(json.dumps(escaped), encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "越界或不再安全"):
            self.workspace.describe_incomplete_change_sets()

        journal.unlink()
        alternate = self.root / "alternate-journal.json"
        alternate.write_text(json.dumps(original), encoding="utf-8")
        try:
            journal.symlink_to(alternate)
        except (OSError, NotImplementedError):
            journal.write_text(json.dumps(original), encoding="utf-8")
            return
        with self.assertRaisesRegex(WorkspaceError, "链接形式"):
            self.workspace.describe_incomplete_change_sets()

    def test_missing_journal_is_reported_from_orphan_batch_backups_without_recovery(self) -> None:
        change_set_id = self.make_interrupted_set_after_commit(1)
        journal = self.root / ".harness" / "change-sets" / f"{change_set_id}.json"
        journal.unlink()
        before_a = (self.root / "a.txt").read_bytes()
        before_b = (self.root / "b.txt").read_bytes()
        self.assertEqual(before_a.decode("utf-8"), "中文段落甲\n新值甲\n")
        self.assertEqual(before_b.decode("utf-8"), self.sources["b.txt"])

        with self.assertRaisesRegex(WorkspaceError, "不得据此判定为没有待办"):
            self.workspace.list_incomplete_change_sets()
        startup = self.workspace.describe_incomplete_change_sets()
        self.assertIn("孤立批次备份", startup)
        self.assertIn("不能从备份重建授权", startup)
        self.assertNotIn("没有未完成的多文件变更集", startup)
        evidence = self.workspace.describe_change_set_evidence()
        self.assertIn(change_set_id, evidence)
        self.assertIn("a.txt", evidence)
        self.assertIn("备份", evidence)
        inputs = iter(["/变更集 证据", "/退出"])
        outputs: list[str] = []
        args = build_parser().parse_args(["--workspace", str(self.root), "--plain"])
        self.assertEqual(
            run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append),
            0,
        )
        self.assertIn("孤立批次备份", "\n".join(outputs))
        self.assertEqual((self.root / "a.txt").read_bytes(), before_a)
        self.assertEqual((self.root / "b.txt").read_bytes(), before_b)

    def test_corrupt_orphan_backup_is_uncertain_evidence_not_a_clean_scan(self) -> None:
        change_set_id = self.make_interrupted_fully_applied_set()
        journal = self.root / ".harness" / "change-sets" / f"{change_set_id}.json"
        journal.unlink()
        batch_backups = [
            path for path, data in self.workspace._load_backups()
            if data.get("change_set_id") == change_set_id
        ]
        self.assertEqual(len(batch_backups), 2)
        batch_backups[0].write_bytes(b"{broken json")

        evidence = self.workspace.describe_change_set_evidence()
        self.assertIn("孤立批次备份", evidence)
        self.assertIn("无法校验", evidence)
        self.assertIn("不能判定为无孤立项", evidence)
        self.assertNotIn("没有未完成的多文件变更集", evidence)

    def test_backup_evidence_scan_limit_does_not_claim_storage_is_clean(self) -> None:
        backup_dir = self.workspace._backup_dir(create=True)
        assert backup_dir is not None
        for index in range(CHANGE_SET_BACKUP_SCAN_LIMIT + 1):
            backup_id = f"1-{index:032x}"
            (backup_dir / f"{backup_id}.json").write_text("{}", encoding="utf-8")

        evidence = self.workspace.describe_change_set_evidence()
        self.assertIn(f"达到 {CHANGE_SET_BACKUP_SCAN_LIMIT} 项上限", evidence)
        self.assertIn("不能据此判定为无待办", evidence)
        self.assertNotIn("未发现孤立、损坏或矛盾", evidence)

    def test_journal_evidence_scan_limit_fails_closed(self) -> None:
        journal_dir = self.workspace._change_set_dir(create=True)
        assert journal_dir is not None
        for index in range(CHANGE_SET_JOURNAL_SCAN_LIMIT + 1):
            (journal_dir / f"{index:032x}.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "超过安全扫描上限"):
            self.workspace.describe_incomplete_change_sets()

    def test_linked_backup_is_rejected_during_evidence_scan(self) -> None:
        backup_dir = self.workspace._backup_dir(create=True)
        assert backup_dir is not None
        target = self.root / "ordinary-backup.json"
        target.write_text("{}", encoding="utf-8")
        linked = backup_dir / f"1-{'a' * 32}.json"
        try:
            linked.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("platform policy does not permit a symlink fixture")
        with self.assertRaisesRegex(WorkspaceError, "链接或非普通文件"):
            self.workspace.describe_change_set_evidence()

    def test_denial_and_plan_mode_do_not_create_private_state_or_modify_files(self) -> None:
        source = {name: (self.root / name).read_bytes() for name in self.sources}
        requests: list[ActionRequest] = []
        denied = run_tool(
            self.workspace,
            self.call(self.changes()),
            approve_action=self.approval(requests, answer=False),
        )
        self.assertIn("拒绝", denied)
        self.assertEqual(len(requests), 1)
        self.assertFalse((self.root / ".harness").exists())
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.sources}, source)

        blocked_requests: list[ActionRequest] = []
        planned = run_tool(
            self.workspace,
            self.call(self.changes()),
            approve_action=lambda request: blocked_requests.append(request) or self.fail("plan mode must not prompt"),
            plan_only=True,
        )
        self.assertIn("计划模式", planned)
        self.assertFalse(blocked_requests)
        self.assertFalse((self.root / ".harness").exists())
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.sources}, source)

    def test_stale_member_after_approval_invalidates_entire_set_before_backups(self) -> None:
        original_a = (self.root / "a.txt").read_bytes()

        def edit_second(request: ActionRequest) -> bool:
            self.assertEqual(request.operation, "change_set")
            (self.root / "b.txt").write_text("外部编辑\n", encoding="utf-8")
            return True

        with self.assertRaises(WorkspaceConflictError):
            self.workspace.apply_change_set(self.changes(), approve_action=edit_second)
        self.assertEqual((self.root / "a.txt").read_bytes(), original_a)
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "外部编辑\n")
        self.assertFalse((self.root / ".harness" / "backups").exists())
        self.assertFalse((self.root / ".harness" / "change-sets").exists())

    def test_kth_failure_stops_forward_writes_and_restores_prior_members(self) -> None:
        original = {name: (self.root / name).read_bytes() for name in self.sources}
        real_commit = self.workspace.commit_write
        calls = 0

        def fail_second(preview, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise WorkspaceError("synthetic second-file failure")
            result = real_commit(preview, **kwargs)
            if calls == 1:
                # An unrelated corrupt legacy backup must not block recovery
                # for this batch's validated backup IDs.
                (self.root / ".harness" / "backups" / "unrelated.json").write_text(
                    "{not a backup", encoding="utf-8"
                )
            return result

        self.workspace.commit_write = fail_second  # type: ignore[method-assign]
        requests: list[ActionRequest] = []
        outputs: list[str] = []
        result = run_tool(
            self.workspace,
            self.call(self.changes(("c.txt", "b.txt", "a.txt"))),
            approve_action=self.approval(requests),
            emit_output=outputs.append,
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(calls, 2, "the third file must not be attempted")
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.sources}, original)
        self.assertIn("已恢复", result)
        self.assertIn("未改", result)
        self.assertTrue(outputs)
        record = self.workspace._load_change_set_record(requests[0].path)
        self.assertEqual(record["state"], "rolled_back")
        self.assertEqual([item["status"] for item in record["files"]], ["restored", "not_applied", "not_applied"])

    def test_competing_write_at_kth_member_is_preserved_and_later_members_are_not_attempted(self) -> None:
        real_commit = self.workspace.commit_write
        calls = 0

        def change_second_after_first(preview, **kwargs):
            nonlocal calls
            calls += 1
            result = real_commit(preview, **kwargs)
            if calls == 1:
                (self.root / "b.txt").write_text("并发版本乙\n", encoding="utf-8")
            return result

        self.workspace.commit_write = change_second_after_first  # type: ignore[method-assign]
        requests: list[ActionRequest] = []
        result = self.workspace.apply_change_set(
            self.changes(("a.txt", "b.txt", "c.txt")),
            approve_action=self.approval(requests),
        )
        self.assertEqual(calls, 2)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), self.sources["a.txt"])
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "并发版本乙\n")
        self.assertEqual((self.root / "c.txt").read_text(encoding="utf-8"), self.sources["c.txt"])
        self.assertIn("冲突", result)
        record = self.workspace._load_change_set_record(requests[0].path)
        self.assertEqual(record["state"], "recovery_required")
        self.assertEqual([item["status"] for item in record["files"]], ["restored", "conflict", "not_applied"])
        backup = next(data for _path, data in self.workspace._load_backups() if data["change_set_index"] == 2)
        self.assertIsNone(backup["conflict_snapshot"], "batch logs must not store competitor contents")

    def test_rollback_conflict_never_overwrites_external_version(self) -> None:
        real_commit = self.workspace.commit_write
        calls = 0

        def change_first_then_fail_second(preview, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.root / "a.txt").write_text("外部保存版本\n", encoding="utf-8")
                raise WorkspaceError("synthetic second-file failure")
            return real_commit(preview, **kwargs)

        self.workspace.commit_write = change_first_then_fail_second  # type: ignore[method-assign]
        requests: list[ActionRequest] = []
        result = self.workspace.apply_change_set(
            self.changes(("a.txt", "b.txt", "c.txt")),
            approve_action=self.approval(requests),
        )
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "外部保存版本\n")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), self.sources["b.txt"])
        self.assertEqual((self.root / "c.txt").read_text(encoding="utf-8"), self.sources["c.txt"])
        self.assertIn("冲突", result)
        record = self.workspace._load_change_set_record(requests[0].path)
        self.assertEqual(record["state"], "recovery_required")
        self.assertEqual([item["status"] for item in record["files"]], ["conflict", "not_applied", "not_applied"])

    def test_cancellation_after_first_commit_runs_non_cancelled_compensation(self) -> None:
        original = {name: (self.root / name).read_bytes() for name in self.sources}
        real_commit = self.workspace.commit_write
        calls = 0
        cancelled = threading.Event()

        def cancel_after_first(preview, **kwargs):
            nonlocal calls
            calls += 1
            result = real_commit(preview, **kwargs)
            if calls == 1:
                cancelled.set()
            return result

        self.workspace.commit_write = cancel_after_first  # type: ignore[method-assign]
        requests: list[ActionRequest] = []
        result = self.workspace.apply_change_set(
            self.changes(("a.txt", "b.txt", "c.txt")),
            approve_action=self.approval(requests),
            cancellation_event=cancelled,
        )
        self.assertEqual(calls, 1)
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.sources}, original)
        self.assertIn("已恢复", result)
        self.assertEqual(self.workspace._load_change_set_record(requests[0].path)["state"], "rolled_back")

    def test_recovery_after_hard_kill_is_opt_in_and_can_be_declined_then_approved(self) -> None:
        script = r'''
import hashlib, os, sys
from pathlib import Path
from cvfx_harness.tools import Workspace

root = Path(sys.argv[1])
workspace = Workspace(root)
changes = []
for name, old, new in (("a.txt", "旧值甲", "新值甲"), ("b.txt", "旧值乙", "新值乙")):
    data = (root / name).read_bytes()
    changes.append({"path": name, "expected_sha256": hashlib.sha256(data).hexdigest(), "old_text": old, "new_text": new})
real_commit = Workspace.commit_write
calls = 0
def kill_after_first(self, preview, **kwargs):
    global calls
    backup = real_commit(self, preview, **kwargs)
    calls += 1
    if calls == 1:
        os._exit(73)
    return backup
Workspace.commit_write = kill_after_first
workspace.apply_change_set(changes, approve_action=lambda _request: True)
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=ROOT,
            env=cli_subprocess_env(str(self.root)),
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 73, result.stderr.decode("utf-8", "replace"))
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "中文段落甲\n新值甲\n")
        original_b = (self.root / "b.txt").read_text(encoding="utf-8")
        workspace = Workspace(self.root)
        incomplete = workspace.list_incomplete_change_sets()
        self.assertEqual(len(incomplete), 1)
        change_set_id = incomplete[0]["id"]
        notice = workspace.describe_incomplete_change_sets()
        self.assertIn(change_set_id, notice)
        self.assertIn("不会自动恢复", notice)
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), original_b)

        inputs = iter(["/变更集 列表", f"/变更集 恢复 {change_set_id}", "n", "/退出"])
        outputs: list[str] = []
        args = build_parser().parse_args(["--workspace", str(self.root), "--plain"])
        self.assertEqual(
            run_console(args, input_fn=lambda _prompt: next(inputs), output=outputs.append),
            0,
        )
        rendered = "\n".join(outputs)
        self.assertIn("启动时不会自动恢复", rendered)
        self.assertIn("完整审阅", rendered)
        self.assertIn("用户拒绝", rendered)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "中文段落甲\n新值甲\n")

        requests: list[ActionRequest] = []
        denied = workspace.recover_change_set(
            change_set_id,
            approve_action=self.approval(requests, answer=False),
        )
        self.assertIn("拒绝", denied)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].operation, "change_set_recovery")
        self.assertIn("待恢复", requests[0].diff)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "中文段落甲\n新值甲\n")

        restored = workspace.recover_change_set(change_set_id, approve_action=lambda _request: True)
        self.assertIn("恢复完成", restored)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), self.sources["a.txt"])
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), self.sources["b.txt"])
        self.assertEqual(workspace._load_change_set_record(change_set_id)["state"], "recovered")
        self.assertFalse(workspace.list_incomplete_change_sets())

    def test_interrupted_restore_is_reconciled_from_file_and_backup_checkpoints(self) -> None:
        original = {name: (self.root / name).read_bytes() for name in self.sources}
        script = r'''
import os, sys
from cvfx_harness.tools import Workspace, WorkspaceError

workspace = Workspace(sys.argv[1])
real_commit = Workspace.commit_write
count = 0
def fail_second(self, preview, **kwargs):
    global count
    count += 1
    if count == 2:
        raise WorkspaceError("synthetic second-file failure")
    return real_commit(self, preview, **kwargs)
def kill_after_restore(self, _backup_id):
    os._exit(74)
Workspace.commit_write = fail_second
Workspace._mark_change_set_backup_undone = kill_after_restore
changes = []
for name, old, new in (("a.txt", "旧值甲", "新值甲"), ("b.txt", "旧值乙", "新值乙")):
    data = open(os.path.join(sys.argv[1], name), "rb").read()
    import hashlib
    changes.append({"path": name, "expected_sha256": hashlib.sha256(data).hexdigest(), "old_text": old, "new_text": new})
workspace.apply_change_set(changes, approve_action=lambda _request: True)
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=ROOT,
            env=cli_subprocess_env(str(self.root)),
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 74, result.stderr.decode("utf-8", "replace"))
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.sources}, original)
        workspace = Workspace(self.root)
        incomplete = workspace.list_incomplete_change_sets()
        self.assertEqual(len(incomplete), 1)
        record = incomplete[0]
        self.assertEqual(record["files"][0]["status"], "restoring")
        preview = workspace._change_set_recovery_preview(record["id"], record)
        self.assertEqual(preview.items[0].status, "restored")
        result = workspace.recover_change_set(
            record["id"],
            approve_action=lambda _request: self.fail("no content write remains to approve"),
        )
        self.assertIn("恢复完成", result)
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.sources}, original)
        self.assertEqual(workspace._load_change_set_record(record["id"])["state"], "recovered")

    def test_cancelling_explicit_recovery_keeps_unrestored_files_recoverable(self) -> None:
        change_set_id = self.make_interrupted_fully_applied_set()
        cancellation = threading.Event()
        real_atomic_write = self.workspace._atomic_write_bytes

        def cancel_after_one_restore(target, *args, **kwargs):
            result = real_atomic_write(target, *args, **kwargs)
            if Path(target).name == "b.txt":
                cancellation.set()
            return result

        self.workspace._atomic_write_bytes = cancel_after_one_restore  # type: ignore[method-assign]
        result = self.workspace.recover_change_set(
            change_set_id,
            approve_action=lambda _request: True,
            cancellation_event=cancellation,
        )
        self.assertIn("取消", result)
        record = self.workspace._load_change_set_record(change_set_id)
        self.assertEqual(record["state"], "recovery_required")
        self.assertEqual([item["status"] for item in record["files"]], ["restore_failed", "restored"])
        notice = self.workspace.describe_incomplete_change_sets()
        self.assertIn(change_set_id, notice)
        self.assertIn("recovery_required", notice)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "中文段落甲\n新值甲\n")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), self.sources["b.txt"])

        cancellation.clear()
        self.workspace._atomic_write_bytes = real_atomic_write  # type: ignore[method-assign]
        result = self.workspace.recover_change_set(
            change_set_id,
            approve_action=lambda _request: True,
            cancellation_event=cancellation,
        )
        self.assertIn("恢复完成", result)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), self.sources["a.txt"])
        self.assertEqual(self.workspace._load_change_set_record(change_set_id)["state"], "recovered")

    def test_backup_marker_failure_stays_recoverable_after_file_restore(self) -> None:
        change_set_id = self.make_interrupted_fully_applied_set()
        real_mark_undone = self.workspace._mark_change_set_backup_undone
        failed = False

        def fail_first_marker(backup_id: str) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise WorkspaceError("synthetic backup marker failure")
            real_mark_undone(backup_id)

        self.workspace._mark_change_set_backup_undone = fail_first_marker  # type: ignore[method-assign]
        result = self.workspace.recover_change_set(
            change_set_id,
            approve_action=lambda _request: True,
        )
        self.assertIn("备份标记未完成", result)
        record = self.workspace._load_change_set_record(change_set_id)
        self.assertEqual(record["state"], "recovery_required")
        self.assertEqual(record["files"][1]["status"], "restore_failed")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), self.sources["b.txt"])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "中文段落甲\n新值甲\n")
        preview = self.workspace._change_set_recovery_preview(change_set_id, record)
        self.assertEqual([item.status for item in preview.items], ["will_restore", "restored"])

        result = self.workspace.recover_change_set(
            change_set_id,
            approve_action=lambda _request: True,
        )
        self.assertIn("恢复完成", result)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), self.sources["a.txt"])
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), self.sources["b.txt"])
        self.assertEqual(self.workspace._load_change_set_record(change_set_id)["state"], "recovered")

    def test_schema_is_closed_and_rejects_duplicates_or_secret_content_before_approval(self) -> None:
        requests: list[ActionRequest] = []
        duplicate_path = self.changes(("a.txt", "b.txt"))
        duplicate_path[1]["path"] = "./a.txt"
        duplicate_path[1]["expected_sha256"] = duplicate_path[0]["expected_sha256"]
        duplicate_path[1]["old_text"] = "旧值甲"
        duplicate_path[1]["new_text"] = "另一个值"
        with self.assertRaises(WorkspaceError):
            run_tool(self.workspace, self.call(duplicate_path), approve_action=self.approval(requests))
        self.assertFalse(requests)

        secret = self.root / "c.txt"
        secret.write_text("api_key=synthetic-secret-value\n", encoding="utf-8")
        bad = self.changes(("a.txt", "b.txt"))
        source = secret.read_bytes()
        bad.append({
            "path": "c.txt",
            "expected_sha256": hashlib.sha256(source).hexdigest(),
            "old_text": "synthetic-secret-value",
            "new_text": "redacted",
        })
        with self.assertRaises(WorkspaceError):
            run_tool(self.workspace, self.call(bad), approve_action=self.approval(requests))
        self.assertFalse(requests)
        self.assertEqual(secret.read_bytes(), source)
        self.assertFalse((self.root / ".harness").exists())

    def test_terminal_journal_retention_stays_bounded_across_many_batches(self) -> None:
        for index in range(34):
            changes: list[dict[str, str]] = []
            for relative, suffix in (("a.txt", "甲"), ("b.txt", "乙")):
                target = self.root / relative
                source = target.read_bytes()
                current_value = target.read_text(encoding="utf-8").splitlines()[1]
                changes.append({
                    "path": relative,
                    "expected_sha256": hashlib.sha256(source).hexdigest(),
                    "old_text": current_value,
                    "new_text": f"批次标记{index}{suffix}",
                })
            result = self.workspace.apply_change_set(changes, approve_action=lambda _request: True)
            self.assertIn("已应用全部文件变更", result)
        records = list((self.root / ".harness" / "change-sets").glob("*.json"))
        self.assertEqual(len(records), 32)
        self.assertFalse(self.workspace.list_incomplete_change_sets())
        self.assertEqual(self.workspace.describe_incomplete_change_sets(), "没有未完成的多文件变更集。")
        self.assertTrue(self.workspace._load_backups())
        retained_ids = {path.stem for path in (self.root / ".harness" / "change-sets").glob("*.json")}
        self.assertTrue(all(
            data.get("change_set_id") in retained_ids
            for _path, data in self.workspace._load_backups()
            if data.get("change_set_id") is not None
        ))

    def test_cli_approval_renders_complete_diff_and_asks_once(self) -> None:
        request = ActionRequest(
            "change_set",
            "0" * 32,
            diff="文件：甲.txt\n原文件 SHA-256：0123456789abcdef…\n-旧值\n+新值\n文件：乙.txt\n-旧乙\n+新乙",
        )
        outputs: list[str] = []
        prompts: list[str] = []
        self.assertTrue(_confirm_action(
            request,
            input_fn=lambda prompt: prompts.append(prompt) or "是",
            output=outputs.append,
        ))
        self.assertEqual(len(prompts), 1)
        self.assertIn("一次审批", prompts[0])
        self.assertIn("0123456789abcdef", "\n".join(outputs))
        self.assertIn("乙.txt", "\n".join(outputs))
        self.assertEqual(_completion_matches("/变更集 恢", "恢"), ("恢复",))
        self.assertEqual(_completion_matches("/变更集 证", "证"), ("证据",))

    @unittest.skipUnless(os.name == "nt", "Windows DACL semantics require a Windows fixture")
    def test_windows_dacl_is_retained_through_batch_failure_compensation(self) -> None:
        first = self.root / "a.txt"
        original_bytes = first.read_bytes()
        before_acl = Workspace._windows_dacl_token(first)
        _set_windows_dacl_protection(first, True)
        protected_acl = Workspace._windows_dacl_token(first)
        real_commit = self.workspace.commit_write
        count = 0

        def fail_second(preview, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise WorkspaceError("synthetic second-file failure")
            return real_commit(preview, **kwargs)

        self.workspace.commit_write = fail_second  # type: ignore[method-assign]
        requests: list[ActionRequest] = []
        result = self.workspace.apply_change_set(
            self.changes(("a.txt", "b.txt")),
            approve_action=self.approval(requests),
        )
        self.assertIn("已恢复", result)
        self.assertEqual(first.read_bytes(), original_bytes)
        self.assertEqual(
            Workspace._windows_dacl_copy_token(Workspace._windows_dacl_token(first)),
            Workspace._windows_dacl_copy_token(protected_acl),
        )
        self.assertNotEqual(protected_acl, before_acl)


if __name__ == "__main__":
    unittest.main()
