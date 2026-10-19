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
PLAN_FORMAT_VERSION = 2
LEGACY_PLAN_FORMAT_VERSION = 1
MAX_PLAN_STEPS = 3
MAX_STEP_CHARS = 400
MAX_PLAN_BYTES = 8192
MAX_REQUIRED_TOOLS = 5
PLAN_TOOLS = frozenset({"read_file", "list_directory", "write_file", "patch_file", "run_test_profile"})


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanStep:
    text: str
    status: PlanStatus = "pending"
    completion_criteria: str = "由用户检查实际输出后确认完成。"
    required_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanSnapshot:
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True)
class PlanDraft:
    """An in-memory model proposal. It has no effect until the user accepts it."""

    steps: tuple[PlanStep, ...]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_REQUIRED_TOOLS:
        raise PlanError("每步所需工具必须是受限工具列表。")
    tools: list[str] = []
    for tool in value:
        if not isinstance(tool, str) or tool not in PLAN_TOOLS or tool in tools:
            raise PlanError("计划包含未知或重复的工具名。")
        tools.append(tool)
    return tuple(tools)


def _validate_criteria(value: object) -> str:
    if not isinstance(value, str):
        raise PlanError("完成条件必须是文本。")
    value = value.strip()
    if not value or len(value) > MAX_STEP_CHARS:
        raise PlanError(f"完成条件必须为 1 到 {MAX_STEP_CHARS} 个字符。")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise PlanError("完成条件包含终端控制或不可见格式字符。")
    if contains_likely_secret(value):
        raise PlanError("完成条件包含疑似密钥或凭据，未保存。")
    return value


def parse_plan_proposal(text: object) -> PlanDraft:
    """Parse a strict, bounded JSON proposal; reject formatting and extra fields."""
    if not isinstance(text, str):
        raise PlanError("模型计划响应格式无效。")
    if len(text) > MAX_PLAN_BYTES:
        raise PlanError(f"模型计划响应超过 {MAX_PLAN_BYTES} 字节上限。")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanError("模型计划响应不是有效 UTF-8。") from None
    if not encoded or len(encoded) > MAX_PLAN_BYTES:
        raise PlanError(f"模型计划响应超过 {MAX_PLAN_BYTES} 字节上限。")
    try:
        data = json.loads(text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise PlanError("模型计划响应不是有效的唯一键 JSON 对象。") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"steps"}
        or not isinstance(data["steps"], list)
        or not 1 <= len(data["steps"]) <= MAX_PLAN_STEPS
    ):
        raise PlanError(f"计划必须包含 1 到 {MAX_PLAN_STEPS} 个步骤。")
    steps: list[PlanStep] = []
    for entry in data["steps"]:
        if not isinstance(entry, dict) or set(entry) != {
            "description", "completion_criteria", "required_tools"
        }:
            raise PlanError("计划步骤字段无效；只允许说明、完成条件和所需工具。")
        text_value = PlanStore._validate_text(entry["description"])
        criteria = _validate_criteria(entry["completion_criteria"])
        tools = _validate_tools(entry["required_tools"])
        steps.append(PlanStep(text_value, "pending", criteria, tools))
    try:
        PlanStore._encode(PlanSnapshot(tuple(steps)))
    except PlanError:
        raise PlanError(f"已解析计划超过 {MAX_PLAN_BYTES} 字节存档上限。") from None
    return PlanDraft(tuple(steps))


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
            or data.get("version") not in {LEGACY_PLAN_FORMAT_VERSION, PLAN_FORMAT_VERSION}
            or not isinstance(data.get("steps"), list)
            or len(data["steps"]) > MAX_PLAN_STEPS
        ):
            raise PlanError("计划存档格式无效或超过步骤上限。")

        version = data["version"]
        steps: list[PlanStep] = []
        active = 0
        for record in data["steps"]:
            expected_fields = {"text", "status"} if version == LEGACY_PLAN_FORMAT_VERSION else {
                "text", "status", "completion_criteria", "required_tools"
            }
            if not isinstance(record, dict) or set(record) != expected_fields:
                raise PlanError("计划只允许保存步骤文本和状态。")
            text = cls._validate_text(record["text"])
            status = record["status"]
            if not isinstance(status, str) or status not in PLAN_STATUSES:
                raise PlanError("计划步骤状态无效。")
            active += status == "in-progress"
            if version == LEGACY_PLAN_FORMAT_VERSION:
                criteria = "由用户检查实际输出后确认完成。"
                tools: tuple[str, ...] = ()
            else:
                criteria = _validate_criteria(record["completion_criteria"])
                tools = _validate_tools(record["required_tools"])
            steps.append(PlanStep(text, status, criteria, tools))  # type: ignore[arg-type]
        if active > 1:
            raise PlanError("计划最多只能有一个进行中的步骤。")
        return PlanSnapshot(tuple(steps))

    @staticmethod
    def _encode(snapshot: PlanSnapshot) -> bytes:
        payload = {
            "version": PLAN_FORMAT_VERSION,
            "steps": [
                {
                    "text": step.text,
                    "completion_criteria": step.completion_criteria,
                    "required_tools": list(step.required_tools),
                    "status": step.status,
                }
                for step in snapshot.steps
            ],
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
            PlanStep(
                step.text,
                "pending" if step.status == "in-progress" else step.status,
                step.completion_criteria,
                step.required_tools,
            )
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
        expected_step: PlanStep | None = None,
    ) -> PlanSnapshot:
        snapshot = self.load()
        if type(index) is not int or not 1 <= index <= len(snapshot.steps):
            raise PlanError("步骤编号不存在。")
        if status not in PLAN_STATUSES:
            raise PlanError("计划步骤状态无效。")
        steps = list(snapshot.steps)
        current = steps[index - 1].status
        if expected_step is not None and steps[index - 1] != expected_step:
            raise PlanError("确认后计划步骤已变化；请重新查看并确认。")
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
        current_step = steps[index - 1]
        steps[index - 1] = PlanStep(
            current_step.text,
            status,
            current_step.completion_criteria,
            current_step.required_tools,
        )
        updated = PlanSnapshot(tuple(steps))
        self._write(updated)
        return updated

    def replace(self, draft: PlanDraft, *, expected_snapshot: PlanSnapshot) -> PlanSnapshot:
        """Persist a reviewed draft only if the current plan still matches the review."""
        if not isinstance(draft, PlanDraft):
            raise PlanError("待接受计划格式无效。")
        current = self.load()
        if current != expected_snapshot:
            raise PlanError("审阅期间当前计划已变化；请重新查看后再接受。")
        if any(step.status == "in-progress" for step in current.steps):
            raise PlanError("当前计划有进行中的步骤；请先完成、阻塞或取消它，再接受新计划。")
        if not 1 <= len(draft.steps) <= MAX_PLAN_STEPS:
            raise PlanError(f"计划必须包含 1 到 {MAX_PLAN_STEPS} 个步骤。")
        validated: list[PlanStep] = []
        for step in draft.steps:
            if not isinstance(step, PlanStep):
                raise PlanError("待接受步骤格式无效。")
            text = self._validate_text(step.text)
            criteria = _validate_criteria(step.completion_criteria)
            if not isinstance(step.required_tools, (tuple, list)):
                raise PlanError("所需工具必须是受限工具列表。")
            tools = _validate_tools(list(step.required_tools))
            validated.append(PlanStep(text, "pending", criteria, tools))
        accepted = PlanSnapshot(tuple(validated))
        self._write(accepted)
        return accepted

    @classmethod
    def revise_draft(cls, draft: PlanDraft, index: int, field: str, value: object) -> PlanDraft:
        if type(index) is not int or not 1 <= index <= len(draft.steps):
            raise PlanError("步骤编号不存在。")
        steps = list(draft.steps)
        step = steps[index - 1]
        if field == "说明":
            updated = PlanStep(cls._validate_text(value), "pending", step.completion_criteria, step.required_tools)
        elif field in {"条件", "完成条件"}:
            updated = PlanStep(step.text, "pending", _validate_criteria(value), step.required_tools)
        elif field == "工具":
            if not isinstance(value, str):
                raise PlanError("工具清单必须是逗号分隔的工具名。")
            raw_tools = [] if value.strip() in {"", "无", "无工具"} else [item.strip() for item in value.split(",")]
            updated = PlanStep(step.text, "pending", step.completion_criteria, _validate_tools(raw_tools))
        else:
            raise PlanError("修订字段只能是说明、条件或工具。")
        steps[index - 1] = updated
        draft = PlanDraft(tuple(steps))
        cls._encode(PlanSnapshot(draft.steps))
        return draft

    def clear(self, *, expected_snapshot: PlanSnapshot | None = None) -> None:
        current = self.load()
        if expected_snapshot is not None and current != expected_snapshot:
            raise PlanError("确认后计划已变化；请重新查看后再清除。")
        self._write(PlanSnapshot(()))
