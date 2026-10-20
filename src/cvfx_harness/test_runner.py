from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profiles import TestProfile, TestProfileError, TestProfileStore
from .providers import ProviderCancelled
from .sessions import redact_likely_secrets
from .tools import ActionRequest, ApprovalCallback, Workspace, WorkspaceError

MAX_TEST_RUNTIME_SECONDS = 120.0
MAX_TEST_OUTPUT_BYTES = 32 * 1024
MAX_SHARED_TEST_OUTPUT_BYTES = 16 * 1024
PROCESS_POLL_SECONDS = 0.025
POSIX_TERMINATE_GRACE_SECONDS = 0.35


@dataclass(frozen=True)
class TestRunResult:
    status: str
    return_code: int | None
    output: str
    output_bytes: int
    output_truncated: bool


def _safe_output(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text, _redactions = redact_likely_secrets(text)
    visible: list[str] = []
    for char in text:
        if char == "\n":
            visible.append(char)
            continue
        category = unicodedata.category(char)
        if category.startswith("C"):
            value = ord(char)
            if value <= 0xFF:
                visible.append(f"\\x{value:02x}")
            elif value <= 0xFFFF:
                visible.append(f"\\u{value:04x}")
            else:
                visible.append(f"\\U{value:08x}")
        else:
            visible.append(char)
    return "".join(visible)


def _output_summary(result: TestRunResult) -> str:
    if result.status == "passed":
        title = "测试进程结束，退出码为 0。"
    elif result.status == "failed":
        title = f"测试进程结束，退出码为 {result.return_code}。"
    elif result.status == "timeout":
        title = "测试达到运行时限，进程树已终止。"
    elif result.status == "output_limit":
        title = "测试超过输出上限，进程树已终止。"
    elif result.status == "cancelled":
        title = "用户取消了测试，进程树已终止。"
    else:
        title = "测试启动或运行失败；详细系统错误已隐藏。"
    suffix = "（输出已截断）" if result.output_truncated else ""
    return f"{title} 输出 {result.output_bytes} bytes{suffix}。"


def _reports_missing_pytest(output: str) -> bool:
    missing_pytest_lines = {
        "no module named pytest",
        "no module named 'pytest'",
        'no module named "pytest"',
        "modulenotfounderror: no module named pytest",
        "modulenotfounderror: no module named 'pytest'",
        'modulenotfounderror: no module named "pytest"',
    }
    return any(line.strip().casefold() in missing_pytest_lines for line in output.splitlines())


def _test_environment() -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    if os.name == "nt":
        # The Windows loader and tempfile module need these standard OS paths.
        for name in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


def _command_for(profile: TestProfile, workspace: Workspace) -> tuple[list[str], Path]:
    workspace.resolve_directory(".")
    if not profile.python:
        raise TestProfileError("测试配置尚未选择外部 Python 解释器。")
    interpreter = TestProfileStore._validate_interpreter_path(
        profile.python, require_file=True
    )
    if interpreter is None:
        raise TestProfileError("测试配置尚未选择外部 Python 解释器。")
    target = workspace.root / Path(profile.target)
    cwd = workspace.root / Path(profile.cwd)
    if profile.framework == "unittest":
        argv = [interpreter, "-m", "unittest", "discover", "-s", str(target)]
    elif profile.framework == "pytest":
        argv = [interpreter, "-m", "pytest", "-q", str(target)]
    else:
        raise TestProfileError("测试框架无效；未启动测试进程。")
    return argv, cwd


def _group_exists_posix(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_posix_tree(process: subprocess.Popen[bytes]) -> None:
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + POSIX_TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline and _group_exists_posix(pgid):
        time.sleep(PROCESS_POLL_SECONDS)
    if _group_exists_posix(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    class _WindowsJob:
        """Own a kill-on-close Job Object assigned before user code is resumed."""

        def __init__(self, pid: int) -> None:
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32 = self._kernel32
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ]
            k32.QueryInformationJobObject.restype = wintypes.BOOL
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
            k32.Thread32First.restype = wintypes.BOOL
            k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
            k32.Thread32Next.restype = wintypes.BOOL
            k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenThread.restype = wintypes.HANDLE
            k32.ResumeThread.argtypes = [wintypes.HANDLE]
            k32.ResumeThread.restype = wintypes.DWORD
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.TerminateJobObject.restype = wintypes.BOOL
            self.handle = k32.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError("job_create_failed")
            self.process_handle = None
            try:
                limits = _ExtendedLimitInformation()
                limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not k32.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    raise OSError("job_limits_failed")
                process_handle = k32.OpenProcess(0x0001 | 0x0100 | 0x1000, False, pid)
                if not process_handle:
                    raise OSError("process_open_failed")
                self.process_handle = process_handle
                if not k32.AssignProcessToJobObject(self.handle, process_handle):
                    raise OSError("job_assign_failed")
                self._resume_initial_thread(pid)
            except BaseException:
                self.terminate()
                self.close()
                raise

        def _resume_initial_thread(self, pid: int) -> None:
            k32 = self._kernel32
            snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
            invalid = ctypes.c_void_p(-1).value
            if not snapshot or snapshot == invalid:
                raise OSError("thread_snapshot_failed")
            found = False
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            try:
                ok = k32.Thread32First(snapshot, ctypes.byref(entry))
                while ok:
                    if entry.th32OwnerProcessID == pid:
                        thread = k32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                        if not thread:
                            raise OSError("thread_open_failed")
                        try:
                            if k32.ResumeThread(thread) == 0xFFFFFFFF:
                                raise OSError("thread_resume_failed")
                        finally:
                            k32.CloseHandle(thread)
                        found = True
                        break
                    ok = k32.Thread32Next(snapshot, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snapshot)
            if not found:
                raise OSError("initial_thread_not_found")

        def active_processes(self) -> int:
            info = _BasicAccountingInformation()
            if not self._kernel32.QueryInformationJobObject(
                self.handle,
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                raise OSError("job_query_failed")
            return int(info.ActiveProcesses)

        def terminate(self) -> bool:
            if not self.handle:
                return False
            try:
                if self.active_processes() == 0:
                    return True
            except OSError:
                return False
            if not self._kernel32.TerminateJobObject(self.handle, 1):
                return False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if self.active_processes() == 0:
                        return True
                except OSError:
                    return False
                time.sleep(PROCESS_POLL_SECONDS)
            return False

        def close(self) -> None:
            if self.handle:
                try:
                    if self.active_processes() > 0:
                        self.terminate()
                except OSError:
                    # Closing the kill-on-close job below is the final cleanup path.
                    pass
            if self.process_handle:
                self._kernel32.CloseHandle(self.process_handle)
                self.process_handle = None
            if self.handle:
                self._kernel32.CloseHandle(self.handle)
                self.handle = None


def _start_process(argv: list[str], cwd: Path) -> tuple[subprocess.Popen[bytes], Any | None]:
    options: dict[str, Any] = {
        "args": argv,
        "cwd": cwd,
        "env": _test_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "shell": False,
        "close_fds": True,
        "bufsize": 0,
    }
    if os.name == "posix":
        options["start_new_session"] = True
        return subprocess.Popen(**options), None
    if os.name == "nt":
        # CREATE_SUSPENDED is 0x00000004 and isn't exported by subprocess on every
        # supported Python build; CREATE_NEW_PROCESS_GROUP is the documented API.
        options["creationflags"] = 0x00000004 | subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(**options)
        try:
            job = _WindowsJob(process.pid)
        except BaseException:
            # The process is still suspended unless the fail-closed resume path succeeded.
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.stdout is not None:
                process.stdout.close()
            raise
        return process, job
    raise OSError("unsupported_process_supervision")


def _terminate_tree(process: subprocess.Popen[bytes], job: Any | None) -> bool:
    if os.name == "posix":
        _terminate_posix_tree(process)
        # POSIX process-group signaling requests termination, but cannot prove
        # containment of descendants that escaped with setsid(). Windows Job
        # Objects provide an explicit active-process count and are verified below.
        return True
    elif job is not None:
        confirmed = job.terminate()
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            confirmed = False
        return confirmed
    return False


def execute_profile(
    profile: TestProfile,
    workspace: Workspace,
    *,
    cancellation_event: threading.Event | None = None,
    timeout_seconds: float = MAX_TEST_RUNTIME_SECONDS,
) -> TestRunResult:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= MAX_TEST_RUNTIME_SECONDS
    ):
        raise ValueError("test timeout outside hard limit")
    argv, cwd = _command_for(profile, workspace)
    try:
        process, job = _start_process(argv, cwd)
    except Exception:
        raise WorkspaceError("测试进程未能安全启动；系统错误详情已隐藏。") from None

    output = bytearray()
    total_output = 0
    overflow = threading.Event()
    reader_done = threading.Event()

    def read_output() -> None:
        nonlocal total_output
        try:
            if process.stdout is None:
                return
            while True:
                remaining = MAX_TEST_OUTPUT_BYTES - len(output)
                chunk = process.stdout.read(min(4096, remaining + 1))
                if not chunk:
                    return
                total_output += len(chunk)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    return
        except (OSError, ValueError):
            return
        finally:
            reader_done.set()

    reader = threading.Thread(target=read_output, name="cvfx-test-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    status = "failed"
    try:
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                status = "cancelled"
                if not _terminate_tree(process, job):
                    status = "failed"
                break
            if overflow.is_set():
                status = "output_limit"
                if not _terminate_tree(process, job):
                    status = "failed"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                if not _terminate_tree(process, job):
                    status = "failed"
                break
            return_code = process.poll()
            if os.name == "posix":
                tree_active = _group_exists_posix(process.pid)
            else:
                try:
                    tree_active = job.active_processes() > 0
                except OSError:
                    status = "failed"
                    _terminate_tree(process, job)
                    break
            if return_code is not None and not tree_active and reader_done.is_set():
                status = "passed" if return_code == 0 else "failed"
                break
            time.sleep(PROCESS_POLL_SECONDS)
        try:
            return_code = process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return_code = process.poll()
        reader.join(timeout=2)
        if not reader_done.is_set() and process.stdout is not None:
            process.stdout.close()
            reader.join(timeout=1)
    finally:
        if job is not None:
            job.close()
        elif os.name == "posix" and _group_exists_posix(process.pid):
            # Also cover KeyboardInterrupt and unexpected runner failures.
            _terminate_posix_tree(process)
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()

    output_was_truncated = overflow.is_set() or total_output > len(output)
    raw_output = bytes(output)
    if output_was_truncated:
        # Drop the incomplete final line so a token cut at the byte cap cannot
        # be exposed as an unrecognizable partial credential.
        line_end = raw_output.rfind(b"\n")
        raw_output = raw_output[: line_end + 1] if line_end >= 0 else b""
    safe = _safe_output(raw_output)
    return TestRunResult(
        status=status,
        return_code=return_code,
        output=safe,
        output_bytes=total_output,
        output_truncated=output_was_truncated,
    )


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    marker = "\n[分享输出已截断]"
    payload_limit = max(0, limit - len(marker.encode("utf-8")))
    return encoded[:payload_limit].decode("utf-8", errors="ignore") + marker


def run_test_profile_tool(
    workspace: Workspace,
    profile_store: TestProfileStore,
    profile_id: str,
    *,
    approve_action: ApprovalCallback,
    cancellation_event: threading.Event | None = None,
    emit_output: Any | None = None,
    share_output: bool = True,
) -> str:
    try:
        profile = profile_store.get(profile_id)
        argv, cwd = _command_for(profile, workspace)
    except TestProfileError as exc:
        return (
            f"测试配置无法运行：{exc} 可用 /测试 列表查看状态，"
            "或用 /测试 添加 <ID> 重新配置。未启动测试进程。"
        )
    except WorkspaceError:
        return "测试配置路径不可用；未启动测试进程。可用 /测试 列表检查配置，或重新添加配置。"
    preview = (
        f"用户配置 ID：{profile.profile_id}\n"
        f"Python 解释器：{argv[0]}\n"
        f"argv：{json.dumps(argv, ensure_ascii=False)}\n"
        f"工作目录：{cwd}\n"
        f"运行时限：{MAX_TEST_RUNTIME_SECONDS:g} 秒\n"
        f"合并标准输出/错误上限：{MAX_TEST_OUTPUT_BYTES} bytes\n"
        "使用 shell=False；不执行模型提供的命令或代码。\n"
        "测试代码以当前用户权限运行，不是沙箱；网络访问没有被屏蔽，"
        "不会继承凭据或其他运行时环境变量；Windows 仅传递系统路径和临时目录。\n"
        "每次执行均需单独批准；本次提问最多启动一个测试进程。"
    )
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProviderCancelled("test run cancelled")
    if not approve_action(
        ActionRequest(
            operation="run_test",
            path=profile.profile_id,
            diff=preview,
            source="test_runner",
        )
    ):
        return "用户拒绝测试运行；没有启动测试进程。"
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProviderCancelled("test run cancelled")
    try:
        approved_profile = profile_store.get(profile.profile_id)
    except TestProfileError:
        return "审批后测试配置已更改或失效；没有启动测试进程。"
    if approved_profile != profile:
        return "审批后测试配置已更改；没有启动测试进程。"
    try:
        result = execute_profile(profile, workspace, cancellation_event=cancellation_event)
    except (TestProfileError, WorkspaceError, ValueError):
        return "测试进程未能安全启动；系统错误详情已隐藏。"

    summary = _output_summary(result)
    if emit_output is not None:
        body = result.output or "（无标准输出或错误输出）"
        emit_output(f"\n本地测试结果：{summary}\n{body}\n")

    if result.status == "cancelled":
        return (
            f"{summary} 用户取消后的输出仅在本地显示；没有发送给模型。"
            f"可用 /测试 运行 {profile.profile_id} 重新开始；不会自动重试。"
        )

    pytest_hint = ""
    if profile.framework == "pytest" and result.status == "failed" and _reports_missing_pytest(result.output):
        pytest_hint = (
            "所选外部 Python 环境没有 pytest；Harness 不会自动安装依赖。"
            "请在该解释器环境中自行准备 pytest，或改用 unittest 配置后重试。"
        )

    if not share_output:
        return f"{summary} 测试输出仅在本地显示；没有发送给模型。{pytest_hint}"

    shared = _truncate_utf8(result.output, MAX_SHARED_TEST_OUTPUT_BYTES)
    if result.output_truncated and "[分享输出已截断]" not in shared:
        shared = _truncate_utf8(shared + "\n[测试进程输出已达到本地上限]", MAX_SHARED_TEST_OUTPUT_BYTES)
    if not shared:
        return f"{summary} 用户未提供测试输出给模型。{pytest_hint}"
    share_preview = (
        f"测试配置：{profile.profile_id}\n{summary}\n"
        f"{pytest_hint}\n"
        "以下内容已经进行凭据脱敏和终端控制字符转义。"
        "批准后将把至多 16 KiB 输出发送给当前模型：\n\n"
        f"{shared}"
    )
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProviderCancelled("test output sharing cancelled")
    if not approve_action(
        ActionRequest(
            operation="share_test_output",
            path=profile.profile_id,
            diff=share_preview,
            source="test_runner",
        )
    ):
        return f"{summary} 用户选择仅在本地查看；测试输出没有发送给模型。{pytest_hint}"
    if cancellation_event is not None and cancellation_event.is_set():
        raise ProviderCancelled("test output sharing cancelled")
    return f"{summary}\n{pytest_hint}\n用户另行批准的脱敏输出：\n{shared}"
