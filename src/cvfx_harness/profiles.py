from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .sessions import contains_likely_secret
from .tools import Workspace, WorkspaceError

TEST_PROFILE_VERSION = 2
MAX_TEST_PROFILES = 20
MAX_TEST_PROFILE_BYTES = 32 * 1024
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_PYTHON_EXECUTABLE_NAME = re.compile(r"^(?:python(?:3(?:\.\d+)?)?|pypy3(?:\.\d+)?)$")
_PROFILE_FIELDS_V1 = {"id", "framework", "cwd", "target"}
_PROFILE_FIELDS = {*_PROFILE_FIELDS_V1, "python"}


class TestProfileError(RuntimeError):
    """Safe-to-display test profile configuration error."""


@dataclass(frozen=True)
class TestProfile:
    __test__ = False

    profile_id: str
    framework: str
    cwd: str
    target: str
    python: str | None


class TestProfileStore:
    """User-managed Python test profiles; model tools cannot access this file."""

    __test__ = False

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @staticmethod
    def validate_id(profile_id: str) -> str:
        if not isinstance(profile_id, str) or _PROFILE_ID.fullmatch(profile_id) is None:
            raise TestProfileError("测试配置 ID 只能使用英文字母、数字、下划线或连字符，长度为 1–40。")
        return profile_id

    def _path(self, *, create: bool) -> Path | None:
        try:
            directory = self.workspace.internal_storage_dir("config", create=create)
        except WorkspaceError:
            raise TestProfileError("测试配置目录不可用；没有读取或修改配置。") from None
        return None if directory is None else directory / "test_profiles.json"

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TestProfileError("测试配置包含重复字段。")
            result[key] = value
        return result

    def _check_file(self, path: Path) -> bool:
        try:
            if self.workspace._is_reparse_point(path):
                raise TestProfileError("测试配置文件不能是 symlink/junction。")
            if not path.exists():
                return False
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
                raise TestProfileError("测试配置必须是普通文件且不能有多个硬链接。")
            if info.st_size > MAX_TEST_PROFILE_BYTES:
                raise TestProfileError("测试配置超过本地大小上限。")
            return True
        except TestProfileError:
            raise
        except (OSError, WorkspaceError):
            raise TestProfileError("无法安全检查测试配置文件。") from None

    @staticmethod
    def _validate_interpreter_path(value: Any, *, require_file: bool) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or "\x00" in value
            or any(ord(char) < 32 for char in value)
            or contains_likely_secret(value)
            or not Path(value).is_absolute()
            or (os.name == "nt" and Path(value).suffix.casefold() != ".exe")
        ):
            raise TestProfileError("Python 解释器必须是无凭据的绝对可执行文件路径。")
        if not require_file:
            return value
        try:
            resolved = Path(value).resolve(strict=True)
            info = resolved.stat()
            # resolve() canonicalizes links/junctions, so reject a reparse final target.
            usable = stat.S_ISREG(info.st_mode) and not Workspace._is_reparse_point(resolved)
            if os.name == "nt":
                usable = usable and os.access(resolved, os.R_OK)
            else:
                usable = usable and os.access(resolved, os.X_OK)
            if not usable:
                raise OSError("not an executable regular file")
            if getattr(sys, "frozen", False):
                try:
                    harness_executable = Path(sys.executable).resolve(strict=True)
                except (OSError, RuntimeError):
                    harness_executable = None
                if harness_executable == resolved:
                    raise TestProfileError(
                        "该路径是 Harness 自身；请另选外部 Python 可执行文件。"
                    )
            executable_name = Path(value).stem if os.name == "nt" else Path(value).name
            if _PYTHON_EXECUTABLE_NAME.fullmatch(executable_name.casefold()) is None:
                raise TestProfileError("解释器文件名不像受支持的 Python 可执行文件；未保存配置。")
            # Keep the selected path spelling: on POSIX, venv/bin/python is
            # commonly a symlink whose name is required for pyvenv.cfg discovery.
            return os.path.abspath(os.path.normpath(value))
        except TestProfileError:
            raise
        except (OSError, RuntimeError, UnicodeError, WorkspaceError):
            raise TestProfileError("Python 解释器不可用；请选择现存的外部 Python 可执行文件。") from None

    def _validate_profile(self, value: Any, *, require_interpreter: bool = False) -> TestProfile:
        if not isinstance(value, dict) or not (
            set(value) == _PROFILE_FIELDS or set(value) == _PROFILE_FIELDS_V1
        ):
            raise TestProfileError("测试配置结构无效；仅支持用户定义的固定 Python 测试配置。")
        profile_id = self.validate_id(value.get("id"))
        framework = value.get("framework")
        if not isinstance(framework, str) or framework not in {"unittest", "pytest"}:
            raise TestProfileError("测试框架只允许 unittest 或 pytest。")
        cwd = value.get("cwd")
        target = value.get("target")
        if not isinstance(cwd, str) or not isinstance(target, str):
            raise TestProfileError("工作目录和测试目标必须是工作区相对路径。")
        interpreter = self._validate_interpreter_path(
            value.get("python"), require_file=require_interpreter
        )
        if any(
            not item
            or len(item) > 4096
            or "\x00" in item
            or any(ord(char) < 32 for char in item)
            or contains_likely_secret(item)
            or Path(item).is_absolute()
            or PureWindowsPath(item).is_absolute()
            or PureWindowsPath(item).drive
            or ":" in item
            or ".." in re.split(r"[\\/]+", item)
            for item in (cwd, target)
        ):
            raise TestProfileError("测试路径必须是无控制字符、无凭据的工作区相对路径。")
        try:
            cwd_path = self.workspace.resolve_directory(cwd)
            raw_target = self.workspace.root / target
            target_is_directory = raw_target.is_dir()
            target_path = (
                self.workspace.resolve_directory(target)
                if target_is_directory
                else self.workspace.resolve(target)
            )
            current = self.workspace.root
            for component in Path(target).parts:
                if component in {"", "."}:
                    continue
                current = current / component
                if self.workspace._is_reparse_point(current):
                    raise TestProfileError("测试目标不能经过 symlink/junction。")
            if not target_path.is_dir():
                info = target_path.stat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
                    raise TestProfileError("测试目标必须是普通文件或目录，且不能有多个硬链接。")
        except TestProfileError:
            raise
        except (OSError, WorkspaceError):
            raise TestProfileError("测试工作目录或目标不可用；只允许工作区内的现存文件或目录。") from None
        if framework == "unittest" and not target_path.is_dir():
            raise TestProfileError("unittest 的测试目标必须是工作区内现存目录。")
        return TestProfile(
            profile_id=profile_id,
            framework=framework,
            cwd=cwd_path.relative_to(self.workspace.root).as_posix() or ".",
            target=target_path.relative_to(self.workspace.root).as_posix() or ".",
            python=interpreter,
        )

    def load(self) -> list[TestProfile]:
        path = self._path(create=False)
        if path is None or not self._check_file(path):
            return []
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_TEST_PROFILE_BYTES:
                raise TestProfileError("测试配置超过本地大小上限。")
            text = raw.decode("utf-8")
            if contains_likely_secret(text):
                raise TestProfileError("测试配置疑似包含凭据；为避免使用不可信命令配置，已拒绝读取。")
            payload = json.loads(text, object_pairs_hook=self._unique_json_object)
        except TestProfileError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise TestProfileError("测试配置损坏或不可读取；没有使用其中的配置。") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "profiles"}
            or type(payload.get("version")) is not int
            or payload.get("version") not in {1, TEST_PROFILE_VERSION}
            or not isinstance(payload.get("profiles"), list)
            or len(payload["profiles"]) > MAX_TEST_PROFILES
        ):
            raise TestProfileError("测试配置版本、结构或条目数量无效。")
        profiles = [self._validate_profile(item) for item in payload["profiles"]]
        folded_ids = [profile.profile_id.casefold() for profile in profiles]
        if len(set(folded_ids)) != len(folded_ids):
            raise TestProfileError("测试配置 ID 重复。")
        return sorted(profiles, key=lambda item: item.profile_id.casefold())

    def _save(self, profiles: list[TestProfile]) -> None:
        if len(profiles) > MAX_TEST_PROFILES:
            raise TestProfileError(f"最多保存 {MAX_TEST_PROFILES} 个测试配置。")
        payload = {
            "version": TEST_PROFILE_VERSION,
            "profiles": [
                {
                    "id": profile.profile_id,
                    "framework": profile.framework,
                    "cwd": profile.cwd,
                    "target": profile.target,
                    "python": profile.python,
                }
                for profile in profiles
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_TEST_PROFILE_BYTES or contains_likely_secret(encoded.decode("utf-8")):
            raise TestProfileError("测试配置超出大小上限或疑似含凭据；没有保存。")
        path = self._path(create=True)
        assert path is not None
        self._check_file(path)
        try:
            self.workspace._atomic_write_bytes(path, encoded)
        except WorkspaceError:
            raise TestProfileError("测试配置保存失败；工作区内部目录未修改。") from None

    def add(
        self, profile_id: str, framework: str, python: str, cwd: str, target: str
    ) -> TestProfile:
        profile = self._validate_profile({
            "id": profile_id,
            "framework": framework,
            "python": python,
            "cwd": cwd,
            "target": target,
        }, require_interpreter=True)
        profiles = self.load()
        matching = [
            item for item in profiles
            if item.profile_id.casefold() == profile.profile_id.casefold()
        ]
        if any(item.python is not None for item in matching):
            raise TestProfileError("该测试配置 ID 已存在；请先移除或选择其他 ID。")
        # A version-1 profile cannot run because it has no independently selected
        # interpreter. Let the user re-enter the same ID to complete that profile.
        profiles = [
            item for item in profiles
            if item.profile_id.casefold() != profile.profile_id.casefold()
        ]
        self._save([*profiles, profile])
        return profile

    def remove(self, profile_id: str) -> None:
        safe_id = self.validate_id(profile_id)
        profiles = self.load()
        filtered = [item for item in profiles if item.profile_id.casefold() != safe_id.casefold()]
        if len(filtered) == len(profiles):
            raise TestProfileError("找不到该测试配置。")
        self._save(filtered)

    def get(self, profile_id: str) -> TestProfile:
        safe_id = self.validate_id(profile_id)
        for profile in self.load():
            if profile.profile_id.casefold() == safe_id.casefold():
                if profile.python is None:
                    raise TestProfileError(
                        "该旧测试配置尚未选择外部 Python；用 /测试 添加 <同一ID> 重新配置。"
                    )
                return profile
        raise TestProfileError(
            "没有名为该 ID 的用户测试配置；可用 /测试 列表查看，或用 /测试 添加 <ID> 创建。"
        )

    def runnable(self) -> list[TestProfile]:
        profiles: list[TestProfile] = []
        for profile in self.load():
            if profile.python is None:
                continue
            try:
                resolved = self._validate_interpreter_path(profile.python, require_file=True)
            except TestProfileError:
                continue
            profiles.append(TestProfile(
                profile.profile_id, profile.framework, profile.cwd, profile.target, resolved
            ))
        return profiles
