from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path


def _fail(message: str) -> None:
    raise SystemExit(f"安装自检失败：{message}")


def _subprocess_environment(script_directory: Path) -> dict[str, str]:
    path_entries = [str(script_directory)]
    path_entries.extend(os.environ.get("PATH", "").split(os.pathsep))
    environment = {
        "PATH": os.pathsep.join(entry for entry in path_entries if entry),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("无法在 20 秒内启动 CLI")
    if result.returncode != 0:
        _fail(f"CLI 退出码为 {result.returncode}")
    return result.stdout


def main() -> int:
    try:
        installed_version = importlib.metadata.version("chenxivfx-harness")
    except importlib.metadata.PackageNotFoundError:
        _fail("请先运行 python -m pip install -e .")
    try:
        import cvfx_harness
    except ImportError:
        _fail("无法导入已安装的 Python 包")
    if installed_version != cvfx_harness.__version__:
        _fail("安装包元数据版本与 Python 包版本不一致")

    script_directory = Path(sysconfig.get_path("scripts"))
    environment = _subprocess_environment(script_directory)
    module_help = _run(
        [sys.executable, "-m", "cvfx_harness", "--help"],
        environment=environment,
    )
    if "--workspace" not in module_help or "--provider" not in module_help:
        _fail("python -m cvfx_harness --help 输出不完整")

    executable_names = ("cvfx-harness.exe", "cvfx-harness")
    console_script = next(
        (
            str(script_directory / name)
            for name in executable_names
            if (script_directory / name).is_file()
        ),
        None,
    )
    if console_script is None:
        console_script = shutil.which("cvfx-harness")
    if console_script is None:
        _fail("找不到已安装的 cvfx-harness 命令")

    console_help = _run([console_script, "--help"], environment=environment)
    if "--workspace" not in console_help or "--provider" not in console_help:
        _fail("cvfx-harness --help 输出不完整")

    with tempfile.TemporaryDirectory(prefix="cvfx-selfcheck-") as workspace:
        help_output = _run(
            [console_script, "--workspace", workspace, "--provider", "mock"],
            environment=environment,
            input_text="/帮助\n/退出\n",
        )
        mock_output = _run(
            [console_script, "--workspace", workspace, "--provider", "mock"],
            environment=environment,
            input_text="自检中文\n/退出\n",
        )
    if "/检查点 <名称>" not in help_output:
        _fail("CLI 命令帮助输出不完整")
    if "[模拟模式] 已收到：自检中文" not in mock_output:
        _fail("Mock 中文交互输出不完整")

    print("安装、UTF-8 CLI 帮助和 Mock 中文交互自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
