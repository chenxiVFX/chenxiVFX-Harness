from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PYINSTALLER_VERSION = "6.22.3"
_CURRENT_STAGE = "初始化"
EXPECTED_ARTIFACT_FILES = {
    "cvfx-harness.exe",
    "README-standalone-zh-CN.txt",
    "SHA256SUMS.txt",
    "THIRD_PARTY_NOTICES.txt",
}
FORBIDDEN_BUNDLE_NAMES = {
    ".env",
    "agents.md",
    "architecture.md",
    "readme.md",
    "roadmap.md",
    "settings.json",
}
FORBIDDEN_BUNDLE_PARTS = {
    ".agents",
    ".cache",
    ".codex",
    ".git",
    ".harness",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "tests",
}


def _fail(message: str) -> None:
    safe_message = f"阶段：{_CURRENT_STAGE}；{message}"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8", newline="\n") as summary:
                summary.write(f"\n### Windows 单文件自检失败\n\n{safe_message}\n")
        except OSError:
            pass
    print(f"::error title=Windows 单文件自检失败::{safe_message}", file=sys.stderr)
    raise SystemExit(f"Windows 单文件包构建失败：{safe_message}")


def _record_stage(name: str) -> None:
    global _CURRENT_STAGE
    _CURRENT_STAGE = name
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8", newline="\n") as summary:
            summary.write(f"- Windows 单文件阶段：{name}\n")
    except OSError:
        pass


def _process_error_signature(result: subprocess.CompletedProcess[str]) -> str:
    """Expose only an exception class and optional module name from a local smoke test."""
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        match = re.match(
            r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Interrupt|Failure))(?::|$)",
            candidate,
        )
        if not match:
            continue
        signature = match.group(1)
        module = re.search(r"No module named ['\"]?([A-Za-z0-9_.]+)", candidate)
        if module:
            signature += f" (module {module.group(1)})"
        dll = re.search(r"while importing ([A-Za-z0-9_.]+)", candidate, re.IGNORECASE)
        if dll:
            signature += f" (module {dll.group(1)})"
        codec = re.search(r"'([A-Za-z0-9._-]+)' codec can't encode", candidate)
        if codec:
            signature += f" (codec {codec.group(1)})"
        return signature
    marker_present = "TUI 合成输入、命令补全、F2 映射和退出自检通过。" in output
    return f"return code {result.returncode}; success marker {'present' if marker_present else 'absent'}"


def _minimal_environment() -> dict[str, str]:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    temp_directory = os.environ.get("TEMP") or os.environ.get("TMP")
    user_profile = os.environ.get("USERPROFILE")
    if not system_root or not temp_directory or not user_profile:
        _fail("Windows runner 缺少系统目录、临时目录或用户目录")
    environment = {
        "PATH": str(Path(system_root) / "System32"),
        "SYSTEMROOT": system_root,
        "WINDIR": system_root,
        "TEMP": temp_directory,
        "TMP": temp_directory,
        "USERPROFILE": user_profile,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    return environment


def _run_executable(
    executable: Path,
    arguments: list[str],
    *,
    environment: dict[str, str],
    input_text: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            input=input_text,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("独立可执行文件未能在 60 秒内启动")
    return result, (time.perf_counter() - started) * 1000


def _audit_bundle(executable: Path) -> int:
    try:
        from PyInstaller.archive.readers import pkg_archive_contents

        members = pkg_archive_contents(str(executable), recursive=True)
    except Exception:
        _fail("无法读取 PyInstaller 内嵌清单")
    forbidden_content_found = False
    for member in members:
        normalized = str(member).replace("\\", "/").casefold()
        parts = set(normalized.split("/"))
        if Path(normalized).name in FORBIDDEN_BUNDLE_NAMES or parts & FORBIDDEN_BUNDLE_PARTS:
            forbidden_content_found = True
            break
    if forbidden_content_found:
        _fail("内嵌清单含不应分发的项目文档或配置")
    normalized_members = [str(member).replace("\\", "/").casefold() for member in members]
    required_components = ("cvfx_harness.tui", "prompt_toolkit", "wcwidth")
    if any(
        not any(component in member for member in normalized_members)
        for component in required_components
    ):
        _fail("单文件包未包含中文 TUI 与其固定运行依赖")
    return len(members)


def _write_github_output(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8", newline="\n") as output:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                _fail("拒绝写入格式异常的 GitHub Actions 输出")
            output.write(f"{name}={value}\n")


def _write_artifact_readme(path: Path) -> None:
    path.write_text(
        """chenxiVFX Harness · Windows x64 单文件试验包

此包是 Windows x64 试验构建，不是正式 Release；内含运行 Harness 所需的 Python，无需为 Harness 安装系统 Python。

首次试用：从你核对过的成功 GitHub Actions 运行下载 artifact，解压后在此目录打开 PowerShell。
先按清单核对可执行文件，再指定一个已存在的项目目录：

  $expected = ((Get-Content .\\SHA256SUMS.txt -Raw).Trim() -split '\\s+')[0]
  $actual = (Get-FileHash .\\cvfx-harness.exe -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw 'cvfx-harness.exe 的 SHA-256 不匹配' }
  $workspace = 'D:\\Projects\\My Project'
  if (-not (Test-Path -LiteralPath $workspace -PathType Container)) { throw '工作区目录不存在' }
  .\\cvfx-harness.exe --workspace "$workspace" --provider mock

Mock 完全离线，不读取密钥或连接模型。输入“你好”试用中文，再用 /状态核对工作区，/退出结束。
若工作区配置损坏或字段无效，可加 --ignore-config --provider mock 离线启动，再用 /配置 编辑从默认值重新审阅；该启动方式不会读取损坏文件，只有确认保存后才替换它。
在 Mock 会话中可用 /配置 编辑 保存非敏感的提供商、模型与端点；选择 openai 表示 DeepSeek OpenAI
兼容接口。设置保存在工作区 .harness/config/settings.json，不包含密钥；Harness 不会修改项目 .gitignore。
TUI 可在 /配置 编辑 中审阅设置有限的 F2–F8 中文命令快捷键；映射只填入本地只读命令草稿，仍需按 Enter；
纯文本 CLI 直接输入命令。快捷键在下次 TUI 启动生效，/快捷键可查看当前映射。
提供商/模型/端点在下次启动生效。真实 DeepSeek 请求需要在启动前
由你信任的安全密钥注入方式向进程提供 DEEPSEEK_API_KEY；不要把密钥粘贴到聊天、命令行或配置。
本地包没有 Windows Credential Manager 集成，普通环境变量会进入进程并对该进程可见。没有安全
注入方式时请继续使用 Mock。真实模式重启时不要带 --provider mock。

需要修改文件时，核对目标路径和完整差异后再批准；TUI 默认拒绝，选“是”并按 Enter 才批准一次。
若模型需先读取文件，读取也会单独审批。没有永久授权。

项目测试只支持 unittest 和 pytest。运行 Harness 无需外部 Python；运行项目测试时用 /测试 添加 <ID>
选择一个外部 python.exe，再用 /测试 运行 <ID>。不能选择本 Harness EXE。pytest 必须已在所选
解释器中安装，Harness 不会安装 Python、pytest 或项目依赖。每次运行都要核对解释器、参数和工作目录
并审批。测试代码以当前用户权限运行，不是 OS 沙箱，可访问该用户能访问的文件与网络；只运行你信任的项目测试。

可用 /保存 首次试用 保存安全的普通对话，然后 /退出。会话是工作区 .harness/sessions/ 中未加密的
JSON；不要存入敏感信息。提交项目之前，确认项目自身的 .gitignore 已忽略 .harness/。

计划步骤中断后不会自动续跑。先检查当前工作区，再用 /继续 <编号> 审阅本次可用上下文并明确确认；冷启动不会恢复旧目标、聊天、问卷或工具结果。

交互式终端会使用全屏中文界面。输入区中 Enter 提交、Ctrl+J 换行、Ctrl+C 清空草稿或
取消当前请求；Esc 先关闭建议，再取消输入/请求。审批默认“否”，选择“是”并按 Enter
才会授权单次操作；鼠标点击 Yes 只切换选项，不会直接批准。命令菜单只提供固定命令，
不会扫描工作区路径。中文 IME 行为依赖终端，未完成 Windows Terminal、PowerShell、
ConPTY 与各输入法组合的人工兼容验收。无法使用全屏输入时，可添加 --plain 启动纯文本界面。

THIRD_PARTY_NOTICES.txt 包含随包依赖的许可证通知。

此 CI 文件未签名，Windows 可能显示 SmartScreen 或 Defender 提示。请保持系统
保护开启，并按组织策略处理；不要为运行本文件关闭系统保护。SHA256SUMS.txt 可检测下载/解压损坏；
验证来源时还应核对成功的 workflow 运行及其 artifact。

此单文件包是体验试验，不是正式发布。对应 GitHub Actions 文件会在 14 天后过期。
""",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    if sys.platform != "win32" or sys.maxsize <= 2**32:
        _fail("必须在 64 位 Windows 上构建")
    try:
        import PyInstaller
    except ImportError:
        _fail("未安装锁定的构建期 PyInstaller")
    if PyInstaller.__version__ != PYINSTALLER_VERSION:
        _fail("PyInstaller 版本与构建锁不符")

    repository_root = Path(__file__).resolve().parents[1]
    package_search_path = repository_root / "src"
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        _fail("输出目录必须为空，避免覆盖现有文件")
    output_dir.mkdir(parents=True, exist_ok=True)

    minimal_environment = _minimal_environment()
    with tempfile.TemporaryDirectory(prefix="cvfx-harness-build-") as temporary:
        build_root = Path(temporary)
        entry = build_root / "entry.py"
        entry.write_text(
            "import sys\n"
            "if sys.argv[1:] == ['--_tui-selfcheck']:\n"
            "    from tui_selfcheck import main\n"
            "    raise SystemExit(main())\n"
            "from cvfx_harness.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
            newline="\n",
        )
        distribution_dir = build_root / "dist"
        work_dir = build_root / "work"
        work_dir.mkdir()
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--console",
            "--noupx",
            "--name",
            "cvfx-harness",
            "--paths",
            str(package_search_path),
            "--paths",
            str(repository_root / "scripts"),
            "--collect-all",
            "prompt_toolkit",
            "--collect-all",
            "wcwidth",
            "--distpath",
            str(distribution_dir),
            "--workpath",
            str(work_dir),
            "--specpath",
            str(work_dir),
            str(entry),
        ]
        _record_stage("PyInstaller 编译")
        result = subprocess.run(
            command,
            check=False,
            env=minimal_environment,
            timeout=300,
        )
        if result.returncode != 0:
            _fail("PyInstaller 返回失败状态")

        executable = distribution_dir / "cvfx-harness.exe"
        if not executable.is_file():
            _fail("PyInstaller 未生成预期的 .exe")
        distribution_files = {item.name for item in distribution_dir.iterdir()}
        if distribution_files != {executable.name}:
            _fail("PyInstaller dist 目录含意外文件")

        _record_stage("内嵌依赖与仓库文件清单审计")
        archive_member_count = _audit_bundle(executable)

        _record_stage("EXE 帮助启动自检")
        help_timings: list[float] = []
        for _ in range(5):
            help_result, elapsed_ms = _run_executable(
                executable,
                ["--help"],
                environment=minimal_environment,
            )
            if help_result.returncode != 0:
                _fail("打包程序 --help 返回失败状态")
            if "--workspace" not in help_result.stdout or "--provider" not in help_result.stdout:
                _fail("打包程序 --help 缺少工作区或提供商选项")
            help_timings.append(elapsed_ms)

        _record_stage("EXE TUI 合成键盘自检")
        tui_result, _ = _run_executable(
            executable,
            ["--_tui-selfcheck"],
            environment=minimal_environment,
        )
        if tui_result.returncode != 0 or "TUI 合成输入、命令补全、F2 映射和退出自检通过。" not in tui_result.stdout:
            signature = _process_error_signature(tui_result)
            _fail(f"打包程序 TUI 启动、命令补全或退出自检失败（{signature}）")

        _record_stage("EXE 离线 Mock 与中文工作区路径自检")
        with tempfile.TemporaryDirectory(prefix="cvfx harness 中文 ") as temporary_workspace:
            workspace = Path(temporary_workspace) / "工作区 with spaces 中文"
            workspace.mkdir()
            mock_result, _ = _run_executable(
                executable,
                [
                    "--workspace",
                    str(workspace),
                    "--ignore-config",
                    "--provider",
                    "mock",
                ],
                environment=minimal_environment,
                input_text="/帮助\n单文件中文合成测试\n/退出\n",
            )
        expected_answer = "[模拟模式] 已收到：单文件中文合成测试"
        if mock_result.returncode != 0:
            _fail("打包程序 Mock 中文交互返回失败状态")
        if "/检查点 <名称>" not in mock_result.stdout or expected_answer not in mock_result.stdout:
            _fail("打包程序未完成离线 Mock 中文交互")
        if workspace.name.casefold() not in mock_result.stdout.casefold():
            _fail("打包程序未正确处理含空格和中文的工作区路径")

        _record_stage("EXE 冷启动计划续跑与有限上下文审阅")
        with tempfile.TemporaryDirectory(prefix="cvfx plan resume 中文 ") as temporary_workspace:
            workspace = Path(temporary_workspace) / "计划续跑工作区"
            workspace.mkdir()
            create_plan_result, _ = _run_executable(
                executable,
                [
                    "--workspace", str(workspace), "--ignore-config", "--provider", "mock",
                ],
                environment=minimal_environment,
                input_text="/计划 添加 单文件重启后核验合成步骤\n/退出\n",
            )
            if create_plan_result.returncode != 0:
                _fail("打包 EXE 未能建立合成计划续跑 fixture")

            plan_path = workspace / ".harness" / "plans" / "current.json"
            try:
                plan_snapshot = json.loads(plan_path.read_text(encoding="utf-8"))
                if not isinstance(plan_snapshot, dict):
                    raise ValueError("plan snapshot must be an object")
                plan_steps = plan_snapshot.get("steps")
                if not isinstance(plan_steps, list) or len(plan_steps) != 1:
                    raise ValueError("expected one synthetic plan step")
                if not isinstance(plan_steps[0], dict):
                    raise ValueError("synthetic plan step must be an object")
                plan_steps[0]["status"] = "blocked"
                plan_snapshot["progress"] = {
                    "step_index": 1,
                    "phase": "provider_request",
                    "last_tool": "",
                    "last_result": "none",
                    "interruption_cause": "process_restarted",
                }
                plan_path.write_bytes(
                    json.dumps(
                        plan_snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                _fail("无法准备单文件包冷启动续跑的合成中断计划")

            resume_result, _ = _run_executable(
                executable,
                [
                    "--workspace", str(workspace), "--ignore-config", "--provider", "mock",
                ],
                environment=minimal_environment,
                input_text="/继续 1\n\n是\n/退出\n",
            )
            if resume_result.returncode != 0:
                _fail("打包 EXE 冷启动续跑合成流程返回失败状态")
            expected_resume_markers = (
                "当前进程目标为空",
                "补充本次续跑需要的目标/约束",
                "聊天历史、问卷答案和工具结果没有恢复",
                "确认在仅掌握已保存的第 1 步上下文时启动新的 Agent 并继续？",
                "第 1 步保持 in-progress",
            )
            if any(marker not in resume_result.stdout for marker in expected_resume_markers):
                _fail("打包 EXE 未显示并确认有限的冷启动计划上下文")
            try:
                resumed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                _fail("打包 EXE 冷启动续跑后无法读取合成计划状态")
            resumed_steps = resumed_plan.get("steps") if isinstance(resumed_plan, dict) else None
            if (
                not isinstance(resumed_steps, list)
                or len(resumed_steps) != 1
                or not isinstance(resumed_steps[0], dict)
                or resumed_steps[0].get("status") != "in-progress"
            ):
                _fail("打包 EXE 冷启动续跑没有将合成步骤设为进行中")

        _record_stage("EXE 外部解释器合成 unittest/pytest profile 闭环")
        fixtures = repository_root / "tests" / "fixtures" / "test_profile_runner"
        with tempfile.TemporaryDirectory(prefix="cvfx test runner 中文 ") as temporary_workspace:
            workspace = Path(temporary_workspace) / "用户测试工作区 with spaces"
            workspace.mkdir()
            shutil.copytree(
                fixtures / "basic", workspace / "unit-tests",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copytree(
                fixtures / "pytest", workspace / "pytest-tests",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            profile_input = "\n".join([
                "/测试 添加 ci-unit",
                "unittest",
                sys.executable,
                ".",
                "unit-tests",
                "/测试 添加 ci-pytest",
                "pytest",
                sys.executable,
                ".",
                "pytest-tests",
                "/测试 运行 ci-unit",
                "是",
                "/测试 运行 ci-pytest",
                "是",
                "/退出",
                "",
            ])
            runner_result, _ = _run_executable(
                executable,
                [
                    "--workspace", str(workspace), "--ignore-config", "--provider", "mock",
                ],
                environment=minimal_environment,
                input_text=profile_input,
            )
        if runner_result.returncode != 0:
            _fail("打包 EXE 的用户测试 profile 运行失败")
        if (
            "合成测试通过" not in runner_result.stdout
            or "1 passed" not in runner_result.stdout
            or runner_result.stdout.count("退出码为 0") < 2
            or "pytest" not in runner_result.stdout
        ):
            _fail("打包 EXE 未用用户选择的外部解释器完成 unittest 与 pytest fixture")

        shutil.copy2(executable, output_dir / executable.name)

    final_executable = output_dir / "cvfx-harness.exe"
    digest = hashlib.sha256(final_executable.read_bytes()).hexdigest()
    (output_dir / "SHA256SUMS.txt").write_text(
        f"{digest}  {final_executable.name}\n",
        encoding="ascii",
        newline="\n",
    )
    _write_artifact_readme(output_dir / "README-standalone-zh-CN.txt")
    shutil.copy2(repository_root / "THIRD_PARTY_NOTICES.txt", output_dir / "THIRD_PARTY_NOTICES.txt")

    _record_stage("最终文件白名单与 SHA-256 生成")
    final_entries = list(output_dir.iterdir())
    final_files = {item.name for item in final_entries if item.is_file()}
    if (
        final_files != EXPECTED_ARTIFACT_FILES
        or any(not item.is_file() or item.is_symlink() for item in final_entries)
    ):
        _fail("最终文件清单与白名单不符")

    exe_size = final_executable.stat().st_size
    median_ms = statistics.median(help_timings)
    values = {
        "exe_bytes": str(exe_size),
        "exe_mib": f"{exe_size / (1024 * 1024):.2f}",
        "exe_sha256": digest,
        "archive_members": str(archive_member_count),
        "help_first_ms": f"{help_timings[0]:.1f}",
        "help_median_ms": f"{median_ms:.1f}",
        "help_min_ms": f"{min(help_timings):.1f}",
        "help_max_ms": f"{max(help_timings):.1f}",
    }
    _write_github_output(values)
    print("最终分发文件清单：cvfx-harness.exe、README-standalone-zh-CN.txt、THIRD_PARTY_NOTICES.txt、SHA256SUMS.txt")
    print(f"内嵌清单审查：{archive_member_count} 项；未发现禁止分发的文档或配置名称。")
    print(f"可执行文件大小：{exe_size} 字节（{values['exe_mib']} MiB）")
    print(f"可执行文件 SHA-256：{digest}")
    print(
        "--help 启动实测（Windows CI runner，包含本次进程与输出耗时）："
        f"首次 {values['help_first_ms']} ms；5 次中位数 {values['help_median_ms']} ms；"
        f"范围 {values['help_min_ms']}–{values['help_max_ms']} ms。"
    )
    print("单文件 --help、UTF-8 Mock、中文路径、TUI、冷启动计划续跑上下文审阅和外部解释器 unittest/pytest profile 合成自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
