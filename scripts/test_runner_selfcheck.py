from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cvfx_harness.profiles import TestProfileStore
from cvfx_harness.test_runner import execute_profile
from cvfx_harness.tools import Workspace


def main() -> int:
    fixtures = ROOT / "tests" / "fixtures" / "test_profile_runner"
    with tempfile.TemporaryDirectory(prefix="cvfx-runner-selfcheck-") as temporary:
        root = Path(temporary)
        workspace = Workspace(root)
        shutil.copytree(
            fixtures / "basic", root / "unit-tests",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copytree(
            fixtures / "pytest", root / "pytest-tests",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        store = TestProfileStore(workspace)
        profiles = (
            store.add("synthetic-unit", "unittest", sys.executable, ".", "unit-tests"),
            store.add("synthetic-pytest", "pytest", sys.executable, ".", "pytest-tests"),
        )
        results = [execute_profile(profile, workspace, timeout_seconds=15) for profile in profiles]
        expected = ("合成测试通过", "1 passed")
        for profile, result, marker in zip(profiles, results, expected):
            if result.status != "passed" or marker not in result.output:
                print(f"{profile.framework} 合成 profile 自检失败：{result.status}")
                return 1
            print(f"{profile.framework} 合成 profile 自检通过（外部解释器已显式选择）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
