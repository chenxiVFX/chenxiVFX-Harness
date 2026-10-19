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
PLAN_FORMAT_VERSION = 3
LEGACY_PLAN_FORMAT_VERSION = 1
MAX_PLAN_STEPS = 3
MAX_STEP_CHARS = 400
MAX_PLAN_BYTES = 8192
MAX_REQUIRED_TOOLS = 5
PLAN_TOOLS = frozenset({"read_file", "list_directory", "write_file", "patch_file", "run_test_profile"})
PLAN_PROGRESS_TOOLS = PLAN_TOOLS | {"search_text", "apply_change_set", "ask_user"}
PLAN_PROGRESS_PHASES = frozenset({
    "provider_request", "tool_approval", "local_interaction", "tool_execution",
    "tool_result_returned", "finished", "unknown"
})
PLAN_PROGRESS_RESULTS = frozenset({
    "none", "approval_pending", "not_executed", "outcome_unknown", "returned_redacted", "user_verified"
})
PLAN_INTERRUPTION_CAUSES = frozenset({
    "none", "cancelled", "provider_or_tool_error", "project_guidance_changed",
    "process_restarted", "legacy_state_unknown"
})


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanStep:
    text: str
    status: PlanStatus = "pending"
    completion_criteria: str = "由用户检查实际输出后确认完成。"
    required_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanProgress:
    """Bounded execution milestones; never stores tool arguments, paths, or outputs."""

    step_index: int
    phase: str = "provider_request"
    last_tool: str = ""
    last_result: str = "none"
    interruption_cause: str = "none"


@dataclass(frozen=True)
class PlanSnapshot:
    steps: tuple[PlanStep, ...]
    progress: PlanProgress | None = None
    # Legacy plans lack execution milestones. Each unfinished step must be
    # reviewed by the user before any new Agent can start.
    unverified_steps: tuple[int, ...] = ()


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
    """Persist bounded plan text/status and fixed execution milestones, never tool data."""

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
        if not isinstance(data, dict):
            raise PlanError("计划存档格式无效或超过步骤上限。")
        version = data.get("version")
        root_fields = (
            {"version", "steps", "progress", "unverified_steps"}
            if version == PLAN_FORMAT_VERSION
            else {"version", "steps"}
        )
        if (
            set(data) != root_fields
            or type(data.get("version")) is not int
            or data.get("version") not in {LEGACY_PLAN_FORMAT_VERSION, 2, PLAN_FORMAT_VERSION}
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
        progress = cls._validate_progress(data.get("progress"), len(steps)) if version == PLAN_FORMAT_VERSION else None
        if active and progress is not None and progress.step_index != next(
            index for index, step in enumerate(steps, 1) if step.status == "in-progress"
        ):
            raise PlanError("计划执行记录与进行中步骤不匹配。")
        if version == PLAN_FORMAT_VERSION:
            raw_unverified = data["unverified_steps"]
            if not isinstance(raw_unverified, list) or len(raw_unverified) > len(steps):
                raise PlanError("旧版待核验步骤索引无效。")
            unverified: list[int] = []
            for index in raw_unverified:
                if (
                    type(index) is not int
                    or not 1 <= index <= len(steps)
                    or index in unverified
                    or steps[index - 1].status == "completed"
                ):
                    raise PlanError("旧版待核验步骤索引无效。")
                unverified.append(index)
        else:
            # Older releases could reset an interrupted step to pending. There
            # is no safe way to tell it from work that never started, so treat
            # every unfinished legacy step as having an unknown outcome.
            unverified = [
                index for index, step in enumerate(steps, 1) if step.status != "completed"
            ]
        return PlanSnapshot(tuple(steps), progress, tuple(unverified))

    @staticmethod
    def _validate_progress(value: object, step_count: int) -> PlanProgress | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {
            "step_index", "phase", "last_tool", "last_result", "interruption_cause"
        }:
            raise PlanError("计划中断记录格式无效。")
        step_index = value["step_index"]
        phase = value["phase"]
        tool = value["last_tool"]
        result = value["last_result"]
        cause = value["interruption_cause"]
        if (
            type(step_index) is not int
            or not 1 <= step_index <= step_count
            or not isinstance(phase, str)
            or phase not in PLAN_PROGRESS_PHASES
            or not isinstance(tool, str)
            or (tool and tool not in PLAN_PROGRESS_TOOLS)
            or not isinstance(result, str)
            or result not in PLAN_PROGRESS_RESULTS
            or not isinstance(cause, str)
            or cause not in PLAN_INTERRUPTION_CAUSES
            or (result in {"approval_pending", "not_executed", "outcome_unknown", "returned_redacted"} and not tool and phase != "unknown")
            or (phase == "tool_approval" and result not in {"approval_pending", "not_executed"})
            or (phase == "local_interaction" and result not in {"approval_pending", "not_executed"})
            or (phase == "tool_execution" and result != "outcome_unknown")
            or (phase == "tool_result_returned" and result != "returned_redacted")
        ):
            raise PlanError("计划中断记录字段无效。")
        return PlanProgress(step_index, phase, tool, result, cause)

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
            "progress": (
                None
                if snapshot.progress is None
                else {
                    "step_index": snapshot.progress.step_index,
                    "phase": snapshot.progress.phase,
                    "last_tool": snapshot.progress.last_tool,
                    "last_result": snapshot.progress.last_result,
                    "interruption_cause": snapshot.progress.interruption_cause,
                }
            ),
            "unverified_steps": list(snapshot.unverified_steps),
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
        """Record an interrupted step as blocked; never silently make it runnable."""
        snapshot = self.load()
        if snapshot.unverified_steps:
            steps = list(snapshot.steps)
            for index in snapshot.unverified_steps:
                step = steps[index - 1]
                if step.status in {"pending", "in-progress"}:
                    steps[index - 1] = PlanStep(
                        step.text, "blocked", step.completion_criteria, step.required_tools
                    )
            active_index = next(
                (index for index, step in enumerate(steps, 1) if step.status == "in-progress"),
                None,
            )
            progress = snapshot.progress
            if active_index is not None:
                progress = PlanProgress(
                    active_index, "unknown", "", "outcome_unknown", "legacy_state_unknown"
                )
            elif progress is None or progress.last_result != "outcome_unknown":
                index = snapshot.unverified_steps[0]
                progress = PlanProgress(index, "unknown", "", "outcome_unknown", "legacy_state_unknown")
            recovered = PlanSnapshot(tuple(steps), progress, snapshot.unverified_steps)
            if recovered != snapshot:
                self._write(recovered)
            return recovered
        active_index = next(
            (index for index, step in enumerate(snapshot.steps, 1) if step.status == "in-progress"),
            None,
        )
        if active_index is None:
            return snapshot
        progress = snapshot.progress
        if progress is None or progress.step_index != active_index:
            progress = PlanProgress(active_index, "unknown", "", "outcome_unknown", "legacy_state_unknown")
        elif progress.phase in {"tool_approval", "local_interaction"} and progress.last_result == "approval_pending":
            progress = PlanProgress(
                active_index, progress.phase, progress.last_tool, "not_executed", "process_restarted"
            )
        else:
            progress = PlanProgress(
                progress.step_index,
                progress.phase,
                progress.last_tool,
                progress.last_result,
                "process_restarted",
            )
        steps = list(snapshot.steps)
        step = steps[active_index - 1]
        steps[active_index - 1] = PlanStep(
            step.text, "blocked", step.completion_criteria, step.required_tools
        )
        recovered = PlanSnapshot(tuple(steps), progress, snapshot.unverified_steps)
        self._write(recovered)
        return recovered

    def record_progress(self, index: int, event: str, tool: str | None = None) -> PlanSnapshot:
        """Persist a fixed, non-content execution milestone before/after side effects."""
        snapshot = self.load()
        if type(index) is not int or not 1 <= index <= len(snapshot.steps):
            raise PlanError("步骤编号不存在。")
        if snapshot.steps[index - 1].status != "in-progress":
            raise PlanError("只有进行中的步骤可以记录执行状态。")
        previous = snapshot.progress
        if previous is None or previous.step_index != index:
            previous = PlanProgress(index)
        if event == "provider_request":
            progress = PlanProgress(
                index, "provider_request", previous.last_tool, previous.last_result, "none"
            )
        elif event == "tool_approval":
            if tool not in PLAN_PROGRESS_TOOLS:
                raise PlanError("工具名称不在允许的中断状态列表中。")
            progress = PlanProgress(index, "tool_approval", tool, "approval_pending", previous.interruption_cause)
        elif event == "tool_interaction":
            if tool != "ask_user":
                raise PlanError("本地交互状态只允许用于问卷工具。")
            progress = PlanProgress(index, "local_interaction", tool, "approval_pending", previous.interruption_cause)
        elif event == "tool_denied":
            if tool not in PLAN_PROGRESS_TOOLS:
                raise PlanError("工具名称不在允许的中断状态列表中。")
            progress = PlanProgress(index, "tool_approval", tool, "not_executed", previous.interruption_cause)
        elif event == "tool_execution":
            if tool not in PLAN_PROGRESS_TOOLS:
                raise PlanError("工具名称不在允许的中断状态列表中。")
            progress = PlanProgress(index, "tool_execution", tool, "outcome_unknown", previous.interruption_cause)
        elif event == "tool_error":
            if tool not in PLAN_PROGRESS_TOOLS:
                raise PlanError("工具名称不在允许的中断状态列表中。")
            progress = PlanProgress(index, "tool_execution", tool, "outcome_unknown", previous.interruption_cause)
        elif event == "tool_result":
            if tool not in PLAN_PROGRESS_TOOLS:
                raise PlanError("工具名称不在允许的中断状态列表中。")
            progress = PlanProgress(index, "tool_result_returned", tool, "returned_redacted", previous.interruption_cause)
        elif event == "tool_not_executed":
            if tool not in PLAN_PROGRESS_TOOLS:
                raise PlanError("工具名称不在允许的中断状态列表中。")
            progress = PlanProgress(index, "tool_approval", tool, "not_executed", previous.interruption_cause)
        elif event == "finished":
            progress = PlanProgress(
                index, "finished", previous.last_tool, previous.last_result, previous.interruption_cause
            )
        else:
            raise PlanError("未知的计划执行事件。")
        updated = PlanSnapshot(snapshot.steps, progress, snapshot.unverified_steps)
        self._write(updated)
        return updated

    def mark_interrupted(self, index: int, *, cause: str, cancelled: bool) -> PlanSnapshot:
        if cause not in {"cancelled", "provider_or_tool_error", "project_guidance_changed"}:
            raise PlanError("中断原因无效。")
        snapshot = self.load()
        if type(index) is not int or not 1 <= index <= len(snapshot.steps):
            raise PlanError("步骤编号不存在。")
        step = snapshot.steps[index - 1]
        if step.status != "in-progress":
            raise PlanError("计划步骤状态已变化；没有写入中断记录。")
        progress = snapshot.progress
        if progress is None or progress.step_index != index:
            progress = PlanProgress(index, "unknown", "", "outcome_unknown", "legacy_state_unknown")
        progress = PlanProgress(
            progress.step_index,
            progress.phase,
            progress.last_tool,
            progress.last_result,
            cause,
        )
        steps = list(snapshot.steps)
        steps[index - 1] = PlanStep(
            step.text,
            "cancelled" if cancelled else "blocked",
            step.completion_criteria,
            step.required_tools,
        )
        updated = PlanSnapshot(tuple(steps), progress, snapshot.unverified_steps)
        self._write(updated)
        return updated

    def prepare_resume(
        self,
        index: int,
        *,
        expected_snapshot: PlanSnapshot,
        verified_unknown_outcome: bool = False,
    ) -> PlanSnapshot:
        current = self.load()
        if current != expected_snapshot:
            raise PlanError("计划在恢复审阅期间已变化；请重新查看状态。")
        if type(index) is not int or not 1 <= index <= len(current.steps):
            raise PlanError("步骤编号不存在。")
        step = current.steps[index - 1]
        if step.status not in {"blocked", "cancelled"}:
            raise PlanError("只有受阻、取消或重启中断的步骤可以继续。")
        progress = current.progress
        independently_unknown = (
            progress is not None
            and progress.step_index == index
            and progress.last_result == "outcome_unknown"
        )
        if current.unverified_steps and index not in current.unverified_steps and not independently_unknown:
            raise PlanError(
                f"旧版第 {current.unverified_steps[0]} 步仍需核验；请先使用 /继续 核验 {current.unverified_steps[0]}。"
            )
        legacy_unverified = index in current.unverified_steps
        if legacy_unverified and not verified_unknown_outcome:
            raise PlanError("旧版步骤没有执行阶段记录；请核验工作区后使用 /继续 核验 <编号>。")
        if (
            progress is not None
            and progress.last_result == "outcome_unknown"
            and progress.step_index != index
        ):
            raise PlanError(
                f"第 {progress.step_index} 步仍有未知工具结果；请先核验该步骤，再继续其他步骤。"
            )
        if legacy_unverified:
            progress = PlanProgress(
                index, "unknown", "", "user_verified", "legacy_state_unknown"
            )
        elif progress is not None and progress.step_index == index and progress.last_result == "outcome_unknown":
            if not verified_unknown_outcome:
                raise PlanError("上次工具结果未知；请先检查当前工作区，再使用 /继续 核验 <编号>。")
            progress = PlanProgress(
                index, "provider_request", progress.last_tool, "user_verified", progress.interruption_cause
            )
        steps = list(current.steps)
        remaining_unverified = tuple(
            candidate for candidate in current.unverified_steps if candidate != index
        )
        if remaining_unverified:
            # The user verified this historical step, but other legacy steps
            # still need independent review before execution can resume.
            updated = PlanSnapshot(tuple(steps), progress, remaining_unverified)
        else:
            steps[index - 1] = PlanStep(step.text, "pending", step.completion_criteria, step.required_tools)
            updated = PlanSnapshot(tuple(steps), progress, ())
        self._write(updated)
        return updated

    def add(self, text: str) -> PlanSnapshot:
        snapshot = self.load()
        if len(snapshot.steps) >= MAX_PLAN_STEPS:
            raise PlanError(f"计划最多包含 {MAX_PLAN_STEPS} 个步骤。")
        step = PlanStep(self._validate_text(text))
        updated = PlanSnapshot((*snapshot.steps, step), snapshot.progress, snapshot.unverified_steps)
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
        if snapshot.unverified_steps and status in {"pending", "in-progress"}:
            first = snapshot.unverified_steps[0]
            raise PlanError(
                f"旧版第 {first} 步仍需核验；请先使用 /继续 核验 {first}。"
            )
        if (
            status == "pending"
            and snapshot.progress is not None
            and snapshot.progress.last_result == "outcome_unknown"
        ):
            if snapshot.progress.step_index == index:
                raise PlanError("上次工具结果未知；请先检查当前工作区，再使用 /继续 核验 <编号>。")
            raise PlanError(
                f"第 {snapshot.progress.step_index} 步仍有未知工具结果；请先核验该步骤，再重试其他步骤。"
            )
        if (
            status == "in-progress"
            and snapshot.progress is not None
            and snapshot.progress.last_result == "outcome_unknown"
            and snapshot.progress.step_index != index
        ):
            raise PlanError(
                f"第 {snapshot.progress.step_index} 步仍有未知工具结果；请先核验该步骤，再执行其他步骤。"
            )
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
        remaining_unverified = tuple(
            candidate for candidate in snapshot.unverified_steps
            if not (candidate == index and status == "completed")
        )
        progress = snapshot.progress
        if status == "completed" and index in snapshot.unverified_steps:
            progress = PlanProgress(index, "unknown", "", "user_verified", "legacy_state_unknown")
        updated = PlanSnapshot(tuple(steps), progress, remaining_unverified)
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
        if current.unverified_steps or (
            current.progress is not None and current.progress.last_result == "outcome_unknown"
        ):
            raise PlanError("当前计划仍有未经核验的工具结果；请先逐步核验或清空计划。")
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
