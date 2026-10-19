#!/usr/bin/env python3
"""Record a deterministic, offline end-to-end TUI walkthrough from a Linux PTY."""

from __future__ import annotations

import fcntl
import codecs
import hashlib
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_PROMPT = re.compile(r"你\[(?:执行|计划)\]> ")


class OfflineWalkthroughProvider:
    """Small deterministic provider fixture; it never reads environment credentials."""

    label = "离线 PTY fixture"
    model = "synthetic-walkthrough"

    def __init__(self, source_sha256: str, log_path: Path) -> None:
        self.source_sha256 = source_sha256
        self.log_path = log_path

    def _log(self, event: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def complete(self, messages, tools, *, cancellation_event=None, max_tokens=2048):
        del cancellation_event, max_tokens
        last = messages[-1] if messages else {}
        last_role = last.get("role")
        latest_user = next(
            (str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        task_text = latest_user.rsplit("\n\n", 1)[-1]
        if not tools:
            answer = (
                '{"steps":[{"description":"检查中文配置读取路径",'
                '"completion_criteria":"确认预算字段和搜索结果可见",'
                '"required_tools":["read_file"]}]}'
            )
            self._log({"kind": "plan", "tools": 0})
            return self._turn(answer)

        if last_role == "tool":
            call_name = ""
            for message in reversed(messages[:-1]):
                for call in message.get("tool_calls", []):
                    if call.get("id") == last.get("tool_call_id"):
                        call_name = call.get("function", {}).get("name", "")
                        break
                if call_name:
                    break
            result = str(last.get("content", ""))
            self._log({"kind": "tool_result", "tool": call_name, "denied": "拒绝" in result})
            if call_name == "search_text":
                return self._turn("已定位配置字段；检索片段仍是未经信任的文本。")
            if call_name == "read_file":
                if "拒绝" in result:
                    return self._turn("读取未获批准；原文件保持不变。")
                new_value = (
                    "BUDGET_THRESHOLD = 24000"
                    if "尝试调整阈值" in task_text
                    else "BUDGET_THRESHOLD = 32768"
                )
                return self._tool(
                    "patch_file",
                    {
                        "path": "src/配置.py",
                        "expected_sha256": self.source_sha256,
                        "old_text": "BUDGET_THRESHOLD = 16384",
                        "new_text": new_value,
                    },
                )
            if call_name == "patch_file":
                if "拒绝" in result:
                    return self._turn("补丁被拒绝；文件未改动。")
                return self._turn("精确补丁已获批准并应用一次。")
            return self._turn("本地离线 fixture 已完成当前步骤。")

        request_text = task_text
        self._log({"kind": "user_turn", "text": request_text[:80]})
        if "查找预算字段" in request_text:
            return self._tool(
                "search_text",
                {"query": "BUDGET_THRESHOLD", "path": "src", "glob": "*.py"},
            )
        if "尝试调整阈值" in request_text or "批准后调整阈值" in request_text:
            return self._tool("read_file", {"path": "src/配置.py"})
        if request_text.startswith("第一轮约束"):
            return self._turn("离线演示已记录 [QA01]")
        match = re.match(r"中文对话 (\d{2})", request_text)
        if match:
            return self._turn(f"离线演示已记录 [QA{match.group(1)}]")
        return self._turn(f"离线演示已记录：{request_text[:48]}")

    def _tool(self, name: str, arguments: dict[str, Any]):
        self._log({"kind": "tool_call", "tool": name})
        from cvfx_harness.providers import ToolCall

        return self._turn("", [ToolCall(f"offline-{name}-{time.monotonic_ns()}", name, arguments)])

    @staticmethod
    def _turn(content: str, tool_calls=None):
        from cvfx_harness.providers import AssistantTurn

        return AssistantTurn(content, tool_calls or [])


def _child(workspace: str, log_path: str, source_sha256: str) -> int:
    # Do not pass inherited user credentials or other unrelated environment to the fixture.
    os.environ.clear()
    os.environ.update(
        {
            "PATH": os.defpath,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
        }
    )
    from cvfx_harness import cli
    from cvfx_harness.tui import TerminalUI

    fixture = OfflineWalkthroughProvider(source_sha256, Path(log_path))
    cli.provider_from_environment = lambda *_args, **_kwargs: fixture
    cli.configure_console_encoding = lambda: None

    def _audit(kind: str, **fields: Any) -> None:
        with Path(log_path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False) + "\n")

    original_input = TerminalUI.input
    original_output = TerminalUI.output
    original_resolve = TerminalUI._resolve_pending

    def audited_input(self, prompt: str) -> str:
        value = original_input(self, prompt)
        if prompt.startswith("确认应用压缩？"):
            _audit("compression_answer", accepted=value.strip().casefold() in {"y", "yes", "是"})
        return value

    def audited_output(self, value: str) -> None:
        if "已取消压缩；当前上下文未改变。" in value:
            _audit("compression_cancelled")
        original_output(self, value)

    def audited_resolve(self, **kwargs: Any) -> None:
        if (
            self._pending is not None
            and self._pending.approval
            and self._prompt.startswith("确认应用这个精确补丁？")
        ):
            _audit(
                "patch_approval_view",
                prompt=self._prompt,
                context=self._approval_text,
            )
        original_resolve(self, **kwargs)

    TerminalUI.input = audited_input
    TerminalUI.output = audited_output
    TerminalUI._resolve_pending = audited_resolve
    return cli.main(["--workspace", workspace, "--provider", "mock"])


class PtyWalkthrough:
    def __init__(self, master_fd: int, workspace: Path, output: Path, log_path: Path) -> None:
        self.master_fd = master_fd
        self.workspace = workspace
        self.output = output
        self.log_path = log_path
        self.raw = bytearray()
        self.events: list[list[Any]] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._decoder_finalized = False
        self.started = time.monotonic()
        self.child_pid = -1
        self._input_ready_from = 0
        self._ready = False
        self.last_command_start = 0

    def _time(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def _collect(self, timeout: float = 0.1) -> None:
        ready, _, _ = select.select([self.master_fd], [], [], timeout)
        if not ready:
            return
        try:
            chunk = os.read(self.master_fd, 65536)
        except OSError:
            return
        if chunk:
            self.raw.extend(chunk)
            text = self._decoder.decode(chunk, final=False)
            if text:
                self.events.append([self._time(), "o", text])

    def wait(self, marker: str, *, start: int = 0, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while marker not in _ANSI.sub("", bytes(self.raw[start:]).decode("utf-8", errors="replace")):
            if time.monotonic() >= deadline:
                tail = bytes(self.raw[-5000:]).decode("utf-8", errors="replace")
                raise RuntimeError(f"PTY timed out waiting for {marker!r}; recent output: {tail!r}")
            self._collect(min(0.1, max(0.0, deadline - time.monotonic())))

    def send(self, data: str | bytes) -> None:
        encoded = data.encode("utf-8") if isinstance(data, str) else data
        self._ready = False
        self.events.append([self._time(), "i", encoded.decode("utf-8", errors="replace")])
        if len(encoded) > 3500:
            raise RuntimeError("synthetic PTY input exceeds the canonical line buffer budget")
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(self.master_fd, remaining)
            remaining = remaining[written:]

    def ready(self, *, start: int | None = None) -> None:
        if self._ready and start is None:
            return
        offset = self._input_ready_from if start is None else start
        deadline = time.monotonic() + 8.0
        while not _PROMPT.search(
            _ANSI.sub("", bytes(self.raw[offset:]).decode("utf-8", errors="replace"))
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("PTY timed out waiting for a fresh command prompt")
            self._collect(min(0.1, max(0.0, deadline - time.monotonic())))
        self._input_ready_from = len(self.raw)
        self._ready = True

    def command(
        self,
        value: str,
        *,
        expect: str | None = None,
        timeout: float = 8.0,
        wait_ready: bool = True,
    ) -> None:
        if not self._ready:
            self.ready()
        start = len(self.raw)
        self.last_command_start = start
        self.send(value + "\r")
        if expect:
            self.wait(expect, start=start, timeout=timeout)
        if wait_ready:
            self.ready(start=start)
        else:
            self._input_ready_from = start

    def mark(self, label: str) -> None:
        self.events.append([self._time(), "m", label])

    def resize(self, columns: int, rows: int) -> None:
        fcntl.ioctl(
            self.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )
        self.events.append([self._time(), "r", f"{columns}x{rows}"])
        self._collect(0.15)

    def save_cast(self) -> None:
        if not self._decoder_finalized:
            remaining = self._decoder.decode(b"", final=True)
            if remaining:
                self.events.append([self._time(), "o", remaining])
            self._decoder_finalized = True
        if any(event[1] == "o" and "\ufffd" in event[2] for event in self.events):
            raise RuntimeError("recording contains a malformed or split UTF-8 character")
        header = {
            "version": 2,
            "width": 100,
            "height": 32,
            "timestamp": int(time.time()),
            "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(header, ensure_ascii=False) + "\n")
            for event in self.events:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def _prepare_workspace(workspace: Path) -> str:
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    source = "# 合成演示配置：只在临时工作区使用。\nBUDGET_THRESHOLD = 16384\n"
    (workspace / "src" / "配置.py").write_text(source, encoding="utf-8")
    (workspace / "tests" / "test_demo.py").write_text(
        "import unittest\n\nclass DemoTests(unittest.TestCase):\n"
        "    def test_synthetic_arithmetic(self):\n"
        "        self.assertEqual(6 * 7, 42)\n",
        encoding="utf-8",
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _exercise(walk: PtyWalkthrough, workspace: Path) -> None:
    walk.ready()
    walk.mark("main")
    initial_fingerprint = hashlib.sha256(
        (workspace / "src" / "配置.py").read_bytes()
    ).hexdigest()[:16]
    walk.command("/状态", expect=f"工作区：{workspace.resolve()}")
    walk.mark("status-full-info")

    # A first-time user can leave the staged editor without creating settings.
    walk.command("/配置 编辑", expect="提供商 [", wait_ready=False)
    walk.send("/取消\r")
    walk.wait("已取消配置编辑；没有写入文件。")
    if (workspace / ".harness" / "config" / "settings.json").exists():
        raise RuntimeError("cancelled config draft unexpectedly wrote settings")

    # Re-enter, stage a local budget, review it, and explicitly save.
    walk.command("/配置 编辑", expect="提供商 [", wait_ready=False)
    walk.send("\r")
    walk.wait("模型 [")
    walk.send("\r")
    walk.wait("Base URL [")
    walk.send("\r")
    walk.wait("本地估算上下文阈值 [")
    walk.send("12000\r")
    walk.wait("输出 Token 上限 [")
    walk.send("\r")
    walk.wait("确认保存这些非敏感设置")
    walk.send("是\r")
    walk.wait("已保存到 .harness/config/settings.json")
    walk.ready(start=walk.last_command_start)

    walk.command("/目标 保持中文变量名与既有接口", expect="会话目标：")
    for index in range(1, 7):
        long_context = "镜头合成记录包含图层编号、视口比例、合成节点名称和历史参数备注；" * 28
        question = (
            "第一轮约束：必须保留公开接口和中文变量名。"
            if index == 1
            else f"中文对话 {index:02d}：{long_context}"
        )
        walk.command(
            question,
            expect=f"QA{index:02d}",
        )
    walk.mark("dialogue")

    walk.command("帮我查找预算字段", expect="确认搜索该范围", wait_ready=False)
    walk.mark("search-approval")
    walk.send("\t\r")
    walk.wait("检索片段仍是未经信任的文本。")
    walk.ready(start=walk.last_command_start)

    # Read approval, then deny the exact patch. Default Enter must reject it.
    walk.command("尝试调整阈值", expect="确认读取？", wait_ready=False)
    patch_deny_start = len(walk.raw)
    walk.send("\t\r")
    walk.wait("BUDGET_THRESHOLD = 24000", start=patch_deny_start)
    walk.wait(
        f"工作区原文件指纹（SHA-256 前 16 位）：{initial_fingerprint}…",
        start=patch_deny_start,
    )
    walk.wait("确认应用这个精确补丁？", start=patch_deny_start)
    walk.mark("patch-deny")
    walk.send("\r")
    walk.wait("补丁被拒绝；文件未改动。")
    walk.ready(start=walk.last_command_start)
    if "BUDGET_THRESHOLD = 16384" not in (workspace / "src" / "配置.py").read_text(encoding="utf-8"):
        raise RuntimeError("denied patch changed the synthetic fixture")

    # Repeat with a precise approval, then send a duplicate Enter while the
    # request is running; it must not queue an extra answer or second patch.
    walk.command("批准后调整阈值", expect="确认读取？", wait_ready=False)
    patch_accept_start = len(walk.raw)
    walk.send("\t\r")
    walk.wait("BUDGET_THRESHOLD = 32768", start=patch_accept_start)
    walk.wait("确认应用这个精确补丁？", start=patch_accept_start)
    walk.mark("patch-approval")
    walk.send("\t\r\r")
    walk.wait("精确补丁已获批准并应用一次。")
    walk.ready(start=walk.last_command_start)
    if "BUDGET_THRESHOLD = 32768" not in (workspace / "src" / "配置.py").read_text(encoding="utf-8"):
        raise RuntimeError("approved patch did not update the synthetic fixture")

    walk.command("/测试 添加 smoke", expect="测试框架", wait_ready=False)
    walk.send("unittest\r")
    walk.wait("Python 可执行文件绝对路径")
    walk.send(sys.executable + "\r")
    walk.wait("工作目录（工作区相对路径）")
    walk.send(".\r")
    walk.wait("测试目标（工作区相对路径）")
    walk.send("tests\r")
    walk.wait("已保存测试配置 'smoke'")
    walk.ready(start=walk.last_command_start)
    walk.command("/测试 运行 smoke", expect="确认启动这一次用户配置测试？", wait_ready=False)
    walk.mark("test-approval")
    walk.send("\t\r")
    walk.wait("Ran 1 test", timeout=15.0)
    walk.wait("测试进程结束，退出码为 0。", timeout=15.0)
    walk.ready(start=walk.last_command_start)

    # Save ordinary dialogue only, exercise restore confirmation after tool
    # interactions, then leave the model with a clean, reviewable history.
    walk.command("/保存 qa-walkthrough", expect="安全对话轮次到会话 'qa-walkthrough'")
    walk.command(
        "/恢复 qa-walkthrough",
        expect="恢复会话将替换当前上下文。确认？",
        wait_ready=False,
    )
    walk.send("\t\r")
    walk.wait("已恢复历史会话 'qa-walkthrough'")
    walk.ready(start=walk.last_command_start)

    walk.command("/计划 生成 检查预算字段", expect="待审阅候选计划")
    walk.mark("plan-candidate")
    walk.command("/计划 拒绝", expect="已拒绝并丢弃候选计划")

    walk.command("/压缩 2", expect="确认应用压缩？", wait_ready=False)
    compression_start = walk.last_command_start
    walk.mark("compression-pending")
    # The transcript is longer than the viewport. Page back while the approval
    # remains pending until the preview heading is visible and reviewable.
    for _ in range(40):
        visible = _ANSI.sub(
            "", bytes(walk.raw[compression_start:]).decode("utf-8", errors="replace")
        )
        if "压缩预览" in visible:
            break
        walk.send(b"\x1b[5~")
        walk._collect(0.05)
    walk.wait("压缩预览", start=compression_start)
    walk.mark("compression-preview")
    # Approval is still the default-deny button even while the preview is scrolled.
    decision_start = len(walk.raw)
    walk.send("\r")
    walk.ready(start=decision_start)
    audit_deadline = time.monotonic() + 3
    audit_events: list[dict[str, Any]] = []
    while time.monotonic() < audit_deadline:
        walk._collect(0.05)
        if walk.log_path.exists():
            audit_events = [
                json.loads(line)
                for line in walk.log_path.read_text(encoding="utf-8").splitlines()
            ]
            if any(item.get("kind") == "compression_cancelled" for item in audit_events):
                break
    answers = [item for item in audit_events if item.get("kind") == "compression_answer"]
    if [item.get("accepted") for item in answers] != [False]:
        raise RuntimeError("compression preview was not explicitly rejected at the default-deny prompt")
    if not any(item.get("kind") == "compression_cancelled" for item in audit_events):
        raise RuntimeError("compression cancellation notice was not emitted")
    walk.mark("compression-cancelled")

    # Check the narrow layout after real resizing, then make sure cancellation
    # drops an unsubmitted draft and the app can still exit normally.
    narrow_start = len(walk.raw)
    walk.resize(46, 14)
    walk.wait("你[执行]>", start=narrow_start)
    walk.mark("narrow")
    walk.send("未提交的中文草稿")
    walk.send(b"\x03")
    walk.wait("已清空草稿；尚未提交。")
    walk.send("/退出\r")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        return _child(sys.argv[2], sys.argv[3], sys.argv[4])
    if os.name != "posix":
        raise SystemExit("This recording script requires a POSIX PTY; it is not Windows acceptance evidence.")
    output = ROOT / "evidence" / "tui-synthetic-session.cast"
    with tempfile.TemporaryDirectory(prefix="cvfx-harness-pty-qa-") as temporary:
        base = Path(temporary)
        workspace = base / "合成工作区"
        workspace.mkdir()
        source_sha256 = _prepare_workspace(workspace)
        log_path = base / "provider-events.jsonl"
        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            os._exit(_child(str(workspace), str(log_path), source_sha256))

        walk = PtyWalkthrough(master_fd, workspace, output, log_path)
        walk.child_pid = child_pid
        walk.resize(100, 32)
        try:
            _exercise(walk, workspace)
            deadline = time.monotonic() + 8
            status = None
            while time.monotonic() < deadline:
                walk._collect(0.1)
                child, status = os.waitpid(child_pid, os.WNOHANG)
                if child:
                    break
            if status is None:
                os.kill(child_pid, signal.SIGTERM)
                _, status = os.waitpid(child_pid, 0)
                raise RuntimeError("TUI child did not exit after /退出")
            if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
                raise RuntimeError(f"TUI child exited with status {status}")

            final_source = (workspace / "src" / "配置.py").read_text(encoding="utf-8")
            if "BUDGET_THRESHOLD = 32768" not in final_source:
                raise RuntimeError("synthetic workspace did not retain the approved patch")
            if not (workspace / ".harness" / "sessions" / "qa-walkthrough.json").is_file():
                raise RuntimeError("session archive was not created")
            profiles = json.loads(
                (workspace / ".harness" / "config" / "test_profiles.json").read_text(encoding="utf-8")
            )
            if [profile["id"] for profile in profiles["profiles"]] != ["smoke"]:
                raise RuntimeError("test profile was not saved as expected")
            log = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            patches = [item for item in log if item.get("kind") == "tool_call" and item.get("tool") == "patch_file"]
            if len(patches) != 2:
                raise RuntimeError(f"expected one denied and one accepted patch attempt; got {len(patches)}")
            patch_results = [
                item for item in log
                if item.get("kind") == "tool_result" and item.get("tool") == "patch_file"
            ]
            if [item.get("denied") for item in patch_results] != [True, False]:
                raise RuntimeError("patch deny/accept outcomes did not match the walkthrough")
            patch_views = [item for item in log if item.get("kind") == "patch_approval_view"]
            if len(patch_views) != 2:
                raise RuntimeError("expected two reviewed patch approval panels")
            for view in patch_views:
                context = view.get("context", "")
                if (
                    view.get("prompt") != "确认应用这个精确补丁？"
                    or "目标文件：src/配置.py" not in context
                    or f"工作区原文件指纹（SHA-256 前 16 位）：{source_sha256[:16]}…" not in context
                    or "确认应用" in context
                    or "补丁差异" in context
                ):
                    raise RuntimeError("patch target/fingerprint review panel was malformed or repeated its question")
            if not any(item.get("kind") == "plan" for item in log):
                raise RuntimeError("offline provider did not receive a plan proposal request")
            walk.save_cast()
            recorded_output = "".join(
                event[2] for event in walk.events if event[1] == "o"
            )
            if "预期原文件 SHA-256：[已脱敏]" in recorded_output:
                raise RuntimeError("the trusted patch fingerprint was redacted in the PTY display")
            print(f"Recorded {len(walk.events)} PTY events to {output}")
            print("Synthetic offline flow passed: config, search, patch deny/accept, test approval, session, plan, compression, narrow resize.")
            return 0
        except BaseException:
            if walk.raw:
                sys.stderr.write(bytes(walk.raw[-8000:]).decode("utf-8", errors="replace"))
                sys.stderr.write("\n")
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
            walk.save_cast()
            raise
        finally:
            os.close(master_fd)


if __name__ == "__main__":
    raise SystemExit(main())
