from __future__ import annotations

import argparse
import hashlib
import os
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

运行要求：Windows x64；此包内含所需 Python 运行时，无需安装系统 Python。

PowerShell 示例：
  .\\cvfx-harness.exe --help
  .\\cvfx-harness.exe --workspace . --provider mock

默认 mock 模式完全离线，不读取密钥或访问网络。此试验的构建与自检也只使用
Mock 和合成输入；真实模型连接不在本试验范围内。

SHA256SUMS.txt 列出可执行文件 SHA-256。可用以下命令核对：
  Get-FileHash .\\cvfx-harness.exe -Algorithm SHA256

交互式终端会使用全屏中文界面。输入区中 Enter 提交、Ctrl+J 换行、Ctrl+C 清空草稿或
取消当前请求；Esc 先关闭建议，再取消输入/请求。审批默认“否”，选择“是”并按 Enter
才会授权单次操作；鼠标点击 Yes 只切换选项，不会直接批准。命令菜单只提供固定命令，
不会扫描工作区路径。中文 IME 行为依赖终端，未完成 Windows Terminal、PowerShell、
ConPTY 与各输入法组合的人工兼容验收。

THIRD_PARTY_NOTICES.txt 包含随包依赖的许可证通知。

此 CI 文件未签名，Windows 可能显示 SmartScreen 或 Defender 提示。请保持系统
保护开启，并按组织策略处理；不要为运行本文件关闭系统保护。

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
            if "Windows 优先" not in help_result.stdout or "--provider" not in help_result.stdout:
                _fail("打包程序 --help 缺少中文说明或提供商选项")
            help_timings.append(elapsed_ms)

        _record_stage("EXE TUI 合成键盘自检")
        tui_result, _ = _run_executable(
            executable,
            ["--_tui-selfcheck"],
            environment=minimal_environment,
        )
        if tui_result.returncode != 0 or "TUI 合成输入、命令补全和退出自检通过。" not in tui_result.stdout:
            _fail("打包程序 TUI 启动、命令补全或退出自检失败")

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
        if "命令：" not in mock_result.stdout or expected_answer not in mock_result.stdout:
            _fail("打包程序未完成离线 Mock 中文交互")
        if workspace.name.casefold() not in mock_result.stdout.casefold():
            _fail("打包程序未正确处理含空格和中文的工作区路径")

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
    print("单文件 --help、UTF-8 Mock 对话、中文/空格工作区路径与 TUI 合成键盘自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
