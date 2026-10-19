from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .sessions import contains_likely_secret
from .tools import Workspace, WorkspaceError

PlanStatus = Literal["pending", "in-progress", "completed", "blocked", "cancelled"]
PLAN_STATUSES = frozenset({"pending", "in-progress", "completed", "blocked", "cancelled"})
PLAN_FORMAT_VERSION = 1
MAX_PLAN_STEPS = 3
MAX_STEP_CHARS = 400
MAX_PLAN_BYTES = 4096


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanStep:
    text: str
    status: PlanStatus = "pending"


@dataclass(frozen=True)
class PlanSnapshot:
    steps: tuple[PlanStep, ...]


class PlanStore:
    """Persist only user-authored step text and workflow status, never execution data."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, *, create: bool) -> Path | None:
        try:
            directory = self.workspace.internal_storage_dir("plans", create=create)
            if directory is None:
                return None
            path = directory / "current.json"
            if self.workspace._is_reparse_point(path):
                raise PlanError("计划文件不能是 symlink/junction。")
            return path
        except WorkspaceError as exc:
            raise PlanError(str(exc)) from None

    @staticmethod
    def _validate_text(text: object) -> str:
        if not isinstance(text, str):
            raise PlanError("计划步骤必须是用户输入的文本。")
        value = text.strip()
        if not value:
            raise PlanError("计划步骤不能为空。")
        if len(value) > MAX_STEP_CHARS:
            raise PlanError(f"每个计划步骤最多 {MAX_STEP_CHARS} 个字符。")
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise PlanError("计划步骤包含终端控制或不可见格式字符。")
        if contains_likely_secret(value):
            raise PlanError("计划步骤包含疑似密钥或凭据，未保存。")
        return value

    @classmethod
    def _validate_snapshot(cls, data: object) -> PlanSnapshot:
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "steps"}
            or type(data.get("version")) is not int
            or data.get("version") != PLAN_FORMAT_VERSION
            or not isinstance(data.get("steps"), list)
            or len(data["steps"]) > MAX_PLAN_STEPS
        ):
            raise PlanError("计划存档格式无效或超过步骤上限。")

        steps: list[PlanStep] = []
        active = 0
        for record in data["steps"]:
            if not isinstance(record, dict) or set(record) != {"text", "status"}:
                raise PlanError("计划只允许保存步骤文本和状态。")
            text = cls._validate_text(record["text"])
            status = record["status"]
            if not isinstance(status, str) or status not in PLAN_STATUSES:
                raise PlanError("计划步骤状态无效。")
            active += status == "in-progress"
            steps.append(PlanStep(text=text, status=status))  # type: ignore[arg-type]
        if active > 1:
            raise PlanError("计划最多只能有一个进行中的步骤。")
        return PlanSnapshot(tuple(steps))

    @staticmethod
    def _encode(snapshot: PlanSnapshot) -> bytes:
        payload = {
            "version": PLAN_FORMAT_VERSION,
            "steps": [{"text": step.text, "status": step.status} for step in snapshot.steps],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise PlanError("计划存档无法编码。") from None
        if len(encoded) > MAX_PLAN_BYTES:
            raise PlanError(f"计划存档超过 {MAX_PLAN_BYTES} 字节。")
        return encoded

    def _write(self, snapshot: PlanSnapshot) -> None:
        encoded = self._encode(snapshot)
        path = self._path(create=True)
        assert path is not None
        try:
            if path.exists() and not path.is_file():
                raise PlanError("计划路径不是普通文件。")
            self.workspace._atomic_write_bytes(path, encoded)
        except WorkspaceError as exc:
            raise PlanError(str(exc)) from None

    def load(self) -> PlanSnapshot:
        path = self._path(create=False)
        if path is None or not path.exists():
            return PlanSnapshot(())
        try:
            if not path.is_file():
                raise PlanError("计划路径不是普通文件。")
            if path.stat().st_size > MAX_PLAN_BYTES:
                raise PlanError(f"计划存档超过 {MAX_PLAN_BYTES} 字节。")
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except PlanError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise PlanError("计划存档损坏或不可读取；没有执行任何步骤。") from None
        return self._validate_snapshot(data)

    def recover_unfinished(self) -> PlanSnapshot:
        """Require fresh confirmation after restart by returning active work to pending."""
        snapshot = self.load()
        if not any(step.status == "in-progress" for step in snapshot.steps):
            return snapshot
        recovered = PlanSnapshot(tuple(
            PlanStep(step.text, "pending" if step.status == "in-progress" else step.status)
            for step in snapshot.steps
        ))
        self._write(recovered)
        return recovered

    def add(self, text: str) -> PlanSnapshot:
        snapshot = self.load()
        if len(snapshot.steps) >= MAX_PLAN_STEPS:
            raise PlanError(f"计划最多包含 {MAX_PLAN_STEPS} 个步骤。")
        step = PlanStep(self._validate_text(text))
        updated = PlanSnapshot((*snapshot.steps, step))
        self._write(updated)
        return updated

    def transition(
        self,
        index: int,
        status: PlanStatus,
        *,
        expected_text: str | None = None,
        expected_status: PlanStatus | None = None,
    ) -> PlanSnapshot:
        snapshot = self.load()
        if type(index) is not int or not 1 <= index <= len(snapshot.steps):
            raise PlanError("步骤编号不存在。")
        if status not in PLAN_STATUSES:
            raise PlanError("计划步骤状态无效。")
        steps = list(snapshot.steps)
        current = steps[index - 1].status
        if expected_text is not None and steps[index - 1].text != expected_text:
            raise PlanError("确认后计划步骤文本已变化；请重新查看并确认。")
        if expected_status is not None and current != expected_status:
            raise PlanError("计划步骤状态已变化；请重新查看当前计划。")
        allowed: dict[str, set[str]] = {
            "pending": {"in-progress", "cancelled"},
            "in-progress": {"completed", "blocked", "cancelled"},
            "blocked": {"pending", "cancelled"},
            "cancelled": {"pending"},
            "completed": set(),
        }
        if status not in allowed[current]:
            raise PlanError(f"不允许将 {current} 步骤切换为 {status}。")
        if status == "in-progress" and any(
            step.status == "in-progress" for position, step in enumerate(steps, 1) if position != index
        ):
            raise PlanError("另有进行中的步骤；请先完成、阻塞或取消它。")
        steps[index - 1] = PlanStep(steps[index - 1].text, status)
        updated = PlanSnapshot(tuple(steps))
        self._write(updated)
        return updated

    def clear(self, *, expected_snapshot: PlanSnapshot | None = None) -> None:
        current = self.load()
        if expected_snapshot is not None and current != expected_snapshot:
            raise PlanError("确认后计划已变化；请重新查看后再清除。")
        self._write(PlanSnapshot(()))
