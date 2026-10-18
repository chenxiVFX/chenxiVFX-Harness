from __future__ import annotations

from dataclasses import dataclass

MAX_PROPOSED_TASKS = 3
MAX_TASK_CHARS = 240
MAX_CHILD_DEPTH = 0
MAX_CHILD_TURNS = 4


@dataclass(frozen=True)
class DelegationPlan:
    tasks: tuple[str, ...]
    max_child_depth: int = MAX_CHILD_DEPTH
    max_turns_per_task: int = MAX_CHILD_TURNS
    execution_enabled: bool = False
    shell_enabled: bool = False


class DelegationPlanError(ValueError):
    pass


def build_delegation_plan(raw: str) -> DelegationPlan:
    """Parse a bounded proposal only; this prototype never starts child agents."""
    tasks = tuple(part.strip() for part in raw.split("|") if part.strip())
    if not tasks:
        raise DelegationPlanError("请用竖线分隔至少一项子任务。")
    if len(tasks) > MAX_PROPOSED_TASKS:
        raise DelegationPlanError(f"最多可列出 {MAX_PROPOSED_TASKS} 项任务。")
    if any(len(task) > MAX_TASK_CHARS for task in tasks):
        raise DelegationPlanError(f"每项任务最多 {MAX_TASK_CHARS} 个字符。")
    return DelegationPlan(tasks=tasks)
