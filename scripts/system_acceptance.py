from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cvfx_harness.cli import run_console
from cvfx_harness.plans import PlanStore
from cvfx_harness.profiles import TestProfileStore
from cvfx_harness.providers import AssistantTurn, ToolCall
from cvfx_harness.sessions import SessionStore
from cvfx_harness.tools import Workspace


GUIDE_TEXT = "验收约定：使用 Decimal；每行含税金额按 ROUND_HALF_UP 取整到分，再汇总已取整行。"
PRIVATE_ANSWER = "每行先取整到分，订单总计只加已取整的行。"
AMOUNT_PATH = "商城/金额.py"
ORDER_PATH = "商城/订单.py"
TEST_TARGET = "测试 用例"
PROFILE_ID = "synthetic-unittest"

INITIAL_AMOUNT = '''from decimal import Decimal


def round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))
'''

INITIAL_ORDER = '''from decimal import Decimal


def line_total(net_amount: Decimal, tax_rate: Decimal) -> Decimal:
    return net_amount * (Decimal("1") + tax_rate)


def invoice_total(lines: list[tuple[Decimal, Decimal]]) -> Decimal:
    return sum((line_total(net, rate) for net, rate in lines), Decimal("0.00"))
'''

FIRST_AMOUNT = '''from decimal import Decimal, ROUND_HALF_EVEN


def round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
'''

FIRST_ORDER = '''from decimal import Decimal

from .金额 import round_money


def line_total(net_amount: Decimal, tax_rate: Decimal) -> Decimal:
    return round_money(net_amount * (Decimal("1") + tax_rate))


def invoice_total(lines: list[tuple[Decimal, Decimal]]) -> Decimal:
    return sum((line_total(net, rate) for net, rate in lines), Decimal("0.00"))
'''

TEST_SOURCE = '''from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from 商城.订单 import invoice_total, line_total


class InvoiceRoundingTests(unittest.TestCase):
    def test_each_taxed_line_uses_half_up_before_invoice_sum(self):
        self.assertEqual(line_total(Decimal("0.005"), Decimal("0")), Decimal("0.01"))
        lines = [(Decimal("0.005"), Decimal("0")), (Decimal("0.005"), Decimal("0"))]
        self.assertEqual(invoice_total(lines), Decimal("0.02"))


if __name__ == "__main__":
    unittest.main()
'''

PLAN_JSON = json.dumps(
    {
        "steps": [{
            "description": "读取并搜索金额与订单模块，按项目约定审阅两文件修改",
            "completion_criteria": "两个现有模块的整批差异经过批准，既有 unittest 配置退出码为 0",
            "required_tools": ["read_file", "search_text", "apply_change_set", "run_test_profile"],
        }]
    },
    ensure_ascii=False,
    separators=(",", ":"),
)

ASK_MARKER = "先澄清取整约定"
IMPLEMENT_MARKER = "按已接受计划实现"
CORRECT_MARKER = "根据上一轮实际失败输出"
REJECT_MARKER = "拒绝路径验收"
RACE_MARKER = "并发版本验收"
CANCEL_MARKER = "取消边界验收"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _turn(content: str, calls: list[ToolCall] | None = None) -> AssistantTurn:
    return AssistantTurn(content, calls or [])


def _tool_messages(messages: list[dict[str, object]]) -> list[str]:
    return [
        message.get("content", "")
        for message in messages
        if message.get("role") == "tool" and isinstance(message.get("content", ""), str)
    ]


def _last_user(messages: list[dict[str, object]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


class DeterministicScenarioProvider:
    """Offline model fixture that drives the real run_console/Agent tool loop."""

    label = "synthetic-acceptance"
    model = "deterministic-fixture"

    def __init__(self, root: Path, scenario: str) -> None:
        self.root = root
        self.scenario = scenario
        self.requests: list[dict[str, object]] = []
        self.question_round = 0
        self.implementation_round = 0
        self.correction_round = 0
        self.failure_round = 0
        self.scenario_round = 0

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        *,
        cancellation_event: threading.Event | None = None,
        max_tokens: int = 2048,
    ) -> AssistantTurn:
        del cancellation_event, max_tokens
        copied_messages = json.loads(json.dumps(messages, ensure_ascii=False))
        copied_tools = json.loads(json.dumps(tools, ensure_ascii=False))
        self.requests.append({"messages": copied_messages, "tools": copied_tools})
        system = str(messages[0].get("content", "")) if messages else ""
        if "你只负责提出一个供用户审阅的工作计划" in system:
            if tools:
                raise AssertionError("计划提议不应提供工具")
            request_text = json.dumps(messages, ensure_ascii=False)
            if GUIDE_TEXT not in request_text:
                raise AssertionError("计划请求没有携带已确认的项目说明")
            return AssistantTurn(PLAN_JSON, assistant_message={"role": "assistant", "content": PLAN_JSON})

        request_text = _last_user(messages)
        if GUIDE_TEXT not in json.dumps(messages, ensure_ascii=False):
            raise AssertionError("主 Agent 请求没有保留当前确认的项目说明")

        if ASK_MARKER in request_text:
            self.question_round += 1
            if self.question_round == 1:
                return _turn("", [ToolCall(
                    "ask-rounding", "ask_user", {
                        "questions": [{
                            "id": "rounding",
                            "question": "每行金额按什么规则取整，再计算订单总计？",
                            "suggestions": ["逐行取整后再汇总"],
                        }]
                    },
                )])
            if self.question_round == 2:
                if PRIVATE_ANSWER not in json.dumps(messages, ensure_ascii=False):
                    raise AssertionError("本轮问卷答案没有交回给 Agent")
                return _turn("已收到取整约定，我会作为本轮上下文使用；不会把回答当作文件或测试授权。")
            raise AssertionError("问卷轮次超出预期")

        if IMPLEMENT_MARKER in request_text:
            self.implementation_round += 1
            results = "\n".join(_tool_messages(messages))
            if self.implementation_round == 1:
                return _turn("", [
                    ToolCall("read-amount", "read_file", {"path": AMOUNT_PATH}),
                    ToolCall("read-order", "read_file", {"path": ORDER_PATH}),
                    ToolCall("search-line-total", "search_text", {
                        "query": "line_total", "path": "商城", "glob": "*.py",
                    }),
                ])
            if self.implementation_round == 2:
                if "SHA256=" not in results or "line_total" not in results:
                    raise AssertionError("主 Agent 未收到实际文件读取与搜索结果")
                return _turn("", [ToolCall("two-file-change", "apply_change_set", {
                    "changes": [
                        {
                            "path": AMOUNT_PATH,
                            "expected_sha256": _sha(INITIAL_AMOUNT),
                            "old_text": INITIAL_AMOUNT,
                            "new_text": FIRST_AMOUNT,
                        },
                        {
                            "path": ORDER_PATH,
                            "expected_sha256": _sha(INITIAL_ORDER),
                            "old_text": INITIAL_ORDER,
                            "new_text": FIRST_ORDER,
                        },
                    ]
                })])
            if self.implementation_round == 3:
                if "已应用全部文件变更" not in results:
                    raise AssertionError("两文件变更没有成功返回")
                return _turn("", [ToolCall("first-test", "run_test_profile", {"profile_id": PROFILE_ID})])
            if self.implementation_round == 4:
                if "退出码为 1" not in results or "FAILED (failures=1)" not in results:
                    raise AssertionError("第一轮测试没有向模型返回真实失败状态和退出码")
                self.failure_round += 1
                return _turn("首轮测试确实失败（退出码 1）：HALF_EVEN 把 0.005 取成了 0.00。功能尚未通过；我会等你确认后修正取整模式。")
            raise AssertionError("实现轮次超出预期")

        if CORRECT_MARKER in request_text:
            self.correction_round += 1
            results = _tool_messages(messages)
            if self.correction_round == 1:
                return _turn("", [ToolCall("read-corrected-amount", "read_file", {"path": AMOUNT_PATH})])
            if self.correction_round == 2:
                last_result = results[-1] if results else ""
                digest = re.match(r"SHA256=([0-9a-f]{64})\n", last_result)
                if digest is None or "ROUND_HALF_EVEN" not in last_result:
                    raise AssertionError("修正前没有读取到当前原文件指纹和舍入实现")
                return _turn("", [ToolCall("correct-rounding", "patch_file", {
                    "path": AMOUNT_PATH,
                    "expected_sha256": digest.group(1),
                    "old_text": FIRST_AMOUNT,
                    "new_text": FIRST_AMOUNT.replace("ROUND_HALF_EVEN", "ROUND_HALF_UP"),
                })])
            if self.correction_round == 3:
                if "已应用精确补丁并创建撤销备份" not in "\n".join(results):
                    raise AssertionError("舍入修正补丁未成功应用")
                return _turn("", [ToolCall("corrected-test", "run_test_profile", {"profile_id": PROFILE_ID})])
            if self.correction_round == 4:
                if "退出码为 0" not in "\n".join(results) or "OK" not in "\n".join(results):
                    raise AssertionError("修正后的既有测试没有报告退出码 0 与 OK")
                return _turn("复测退出码为 0，既有 unittest 全部通过。每行先按 ROUND_HALF_UP 取整到分，再汇总；两处修改均经过审批。")
            raise AssertionError("修正轮次超出预期")

        if REJECT_MARKER in request_text:
            return self._change_set_scenario(messages, "已收到拒绝结果；整批修改未应用，文件保持原样，也没有运行测试。", 3)

        if RACE_MARKER in request_text:
            return self._change_set_scenario(messages, "工作区在审批期间发生外部变化；整批被拒绝，竞争版本没有被覆盖。", 3)

        if CANCEL_MARKER in request_text:
            return _turn("", [ToolCall("cancelled-change", "apply_change_set", {
                "changes": self._changes(),
            })])

        raise AssertionError(f"合成模型收到未编排的用户请求：{request_text[:160]}")

    def _change_set_scenario(
        self, messages: list[dict[str, object]], final_text: str, expected_rounds: int
    ) -> AssistantTurn:
        self.scenario_round += 1
        results = "\n".join(_tool_messages(messages))
        if self.scenario_round == 1:
            return _turn("", [
                ToolCall("scenario-read-amount", "read_file", {"path": AMOUNT_PATH}),
                ToolCall("scenario-read-order", "read_file", {"path": ORDER_PATH}),
                ToolCall("scenario-search", "search_text", {
                    "query": "line_total", "path": "商城", "glob": "*.py",
                }),
            ])
        if self.scenario_round == 2:
            if "SHA256=" not in results or "line_total" not in results:
                raise AssertionError("竞争/拒绝路径没有先经过读取与搜索")
            return _turn("", [ToolCall("scenario-change-set", "apply_change_set", {
                "changes": self._changes(),
            })])
        if self.scenario_round == expected_rounds:
            if self.scenario == "reject" and "用户拒绝整个变更集" not in results:
                raise AssertionError("拒绝结果没有返回给模型")
            if self.scenario == "race" and "操作被工作区策略阻止" not in results:
                raise AssertionError("并发版本冲突没有作为工具失败返回")
            return _turn(final_text)
        raise AssertionError("拒绝/并发场景模型轮次超出预期")

    @staticmethod
    def _changes() -> list[dict[str, str]]:
        return [
            {
                "path": AMOUNT_PATH,
                "expected_sha256": _sha(INITIAL_AMOUNT),
                "old_text": INITIAL_AMOUNT,
                "new_text": FIRST_AMOUNT,
            },
            {
                "path": ORDER_PATH,
                "expected_sha256": _sha(INITIAL_ORDER),
                "old_text": INITIAL_ORDER,
                "new_text": FIRST_ORDER,
            },
        ]


def _create_project(root: Path) -> None:
    (root / "商城").mkdir(parents=True)
    (root / TEST_TARGET).mkdir(parents=True)
    _write_utf8_lf(root / "AGENTS.md", f"# 项目约定\n\n{GUIDE_TEXT}\n")
    _write_utf8_lf(root / "商城" / "__init__.py", "")
    _write_utf8_lf(root / AMOUNT_PATH, INITIAL_AMOUNT)
    _write_utf8_lf(root / ORDER_PATH, INITIAL_ORDER)
    _write_utf8_lf(root / TEST_TARGET / "test_invoice.py", TEST_SOURCE)
    TestProfileStore(Workspace(root)).add(
        PROFILE_ID, "unittest", sys.executable, ".", TEST_TARGET
    )


def _write_utf8_lf(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


class _InteractiveInput:
    def __init__(
        self,
        commands: list[str],
        *,
        scenario: str,
        root: Path,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        self.commands = iter(commands)
        self.scenario = scenario
        self.root = root
        self.cancellation_event = cancellation_event
        self.prompts: list[str] = []
        self.change_set_approval_seen = False

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("你["):
            try:
                return next(self.commands)
            except StopIteration:
                raise AssertionError(f"CLI 请求了预期外的会话输入：{prompt}") from None
        if "确认将这个确切文件版本" in prompt:
            return "是"
        if prompt == "回答> ":
            return PRIVATE_ANSWER
        if "允许问卷随之后的 /保存" in prompt:
            return "否"
        if "确认将回答交给本轮模型" in prompt:
            return "是"
        if "以上所有文件共用这一次审批" in prompt:
            self.change_set_approval_seen = True
            if self.scenario == "reject":
                return "否"
            if self.scenario == "race":
                _write_utf8_lf(
                    self.root / ORDER_PATH,
                    "# external concurrent edit\n" + INITIAL_ORDER,
                )
                return "是"
            if self.scenario == "cancel":
                if self.cancellation_event is None:
                    raise AssertionError("取消场景缺少 cancellation_event")
                self.cancellation_event.set()
                return "是"
            return "是"
        if any(token in prompt for token in (
            "确认读取？", "确认搜索该范围", "确认启动这一次用户配置测试",
            "确认把以上已脱敏输出", "确认应用这个精确补丁",
        )):
            return "是"
        raise AssertionError(f"遇到未处理的真实 CLI 提示：{prompt}")


def _run_console_case(
    *,
    scenario: str,
    commands: list[str],
    cancellation_event: threading.Event | None = None,
) -> tuple[int, Path, tempfile.TemporaryDirectory[str], DeterministicScenarioProvider, list[str], _InteractiveInput]:
    temporary = tempfile.TemporaryDirectory(prefix="cvfx-synthetic-acceptance-")
    root = Path(temporary.name) / "验收 工作区"
    root.mkdir()
    _create_project(root)
    provider = DeterministicScenarioProvider(root, scenario)
    outputs: list[str] = []
    input_driver = _InteractiveInput(
        commands, scenario=scenario, root=root, cancellation_event=cancellation_event
    )
    args = SimpleNamespace(workspace=str(root), provider="mock", model=None, max_model_requests=4)
    with patch("cvfx_harness.cli.provider_from_environment", return_value=provider):
        result = run_console(
            args,
            input_fn=input_driver,
            output=outputs.append,
            cancellation_event=cancellation_event,
        )
    return result, root, temporary, provider, outputs, input_driver


def _assert_happy_path() -> None:
    commands = [
        "/项目说明",
        f"{ASK_MARKER}，请先明确逐行取整规则。",
        "/保存 privacy-check",
        "/计划 生成 修正订单逐行税额取整并运行现有 unittest 配置",
        "/计划 接受",
        "/模式 执行",
        f"{IMPLEMENT_MARKER}：实现逐行取整，先读取和搜索，再一次审阅两文件修改，然后运行现有测试。",
        f"{CORRECT_MARKER}：修正舍入模式，重新运行测试并总结。",
        "/计划",
        "/退出",
    ]
    rc, root, temporary, provider, outputs, input_driver = _run_console_case(
        scenario="success", commands=commands
    )
    try:
        combined = "\n".join(outputs)
        amount = (root / AMOUNT_PATH).read_text(encoding="utf-8")
        order = (root / ORDER_PATH).read_text(encoding="utf-8")
        session = root / ".harness" / "sessions" / "privacy-check.json"
        session_text = session.read_text(encoding="utf-8")
        plan = PlanStore(Workspace(root)).load()

        assert rc == 0
        assert "SHA-256" in combined and GUIDE_TEXT in combined
        assert "工作区搜索" in combined and "多文件变更集" in combined
        assert "确认读取？" in "\n".join(input_driver.prompts)
        assert "确认搜索该范围" in "\n".join(input_driver.prompts)
        assert sum("以上所有文件共用这一次审批" in prompt for prompt in input_driver.prompts) == 1
        assert amount == FIRST_AMOUNT.replace("ROUND_HALF_EVEN", "ROUND_HALF_UP")
        assert "return round_money(net_amount * (Decimal(\"1\") + tax_rate))" in order
        assert "测试进程结束，退出码为 1。" in combined and "FAILED (failures=1)" in combined
        assert "测试进程结束，退出码为 0。" in combined and "OK" in combined
        assert "首轮测试确实失败（退出码 1）" in combined
        assert "复测退出码为 0，既有 unittest 全部通过" in combined
        assert session.exists()
        assert GUIDE_TEXT not in session_text and PRIVATE_ANSWER not in session_text
        assert PRIVATE_ANSWER not in combined
        assert "unsaved_questionnaire_answers" in session_text
        assert len(plan.steps) == 1 and plan.steps[0].status == "pending"
        assert "已接受并保存 1 个 pending 步骤" in combined and "[待办]" in combined
        assert provider.question_round == 2
        assert provider.implementation_round == 4
        assert provider.correction_round == 4
        assert provider.failure_round == 1
        assert input_driver.change_set_approval_seen
        assert not any("错误详情未显示" in line for line in outputs)
        # The last messages actually sent to the deterministic provider include both
        # the approved project guide and the current tool result at each decision point.
        implementation_requests = [
            item for item in provider.requests
            if IMPLEMENT_MARKER in _last_user(item["messages"])
        ]
        assert len(implementation_requests) == 4
        assert "line_total" in "\n".join(_tool_messages(implementation_requests[1]["messages"]))
        assert "退出码为 1" in "\n".join(_tool_messages(implementation_requests[3]["messages"]))
        # The first run remains a reported failure before the correction request; only
        # the later, actually successful process is reported as passing.
        assert combined.index("首轮测试确实失败") < combined.index("复测退出码为 0")
        assert all(item["tools"] for item in implementation_requests)
        print("主流程：已确认项目说明→问卷仅本次→计划审阅→读取/搜索→一次两文件审批→首测失败→定向修正→复测通过。")
        print("核验：首轮 exit 1、修正后 exit 0；会话未含临时说明/问卷回答；计划仍 pending，未自动标完成。")
    finally:
        temporary.cleanup()


def _assert_rejection_path() -> None:
    rc, root, temporary, provider, outputs, input_driver = _run_console_case(
        scenario="reject",
        commands=["/项目说明", f"{REJECT_MARKER}：先读取、搜索并建议两模块修改。", "/退出"],
    )
    try:
        assert rc == 0
        assert input_driver.change_set_approval_seen
        assert (root / AMOUNT_PATH).read_text(encoding="utf-8") == INITIAL_AMOUNT
        assert (root / ORDER_PATH).read_text(encoding="utf-8") == INITIAL_ORDER
        assert not (root / ".harness" / "backups").exists()
        combined = "\n".join(outputs)
        assert "整批修改未应用，文件保持原样" in combined
        assert "测试进程结束" not in combined
        assert any("用户拒绝整个变更集" in tool for call in provider.requests for tool in _tool_messages(call["messages"]))
        print("拒绝路径：整批审批拒绝后两文件字节保持原值，无备份写入，主 Agent 未报告修改成功。")
    finally:
        temporary.cleanup()


def _assert_external_conflict_path() -> None:
    rc, root, temporary, provider, outputs, input_driver = _run_console_case(
        scenario="race",
        commands=["/项目说明", f"{RACE_MARKER}：读取、搜索后审阅两文件变更。", "/退出"],
    )
    try:
        external_order = "# external concurrent edit\n" + INITIAL_ORDER
        assert rc == 0
        assert input_driver.change_set_approval_seen
        assert (root / ORDER_PATH).read_text(encoding="utf-8") == external_order
        assert (root / AMOUNT_PATH).read_text(encoding="utf-8") == INITIAL_AMOUNT
        assert "竞争版本没有被覆盖" in "\n".join(outputs)
        assert "测试进程结束" not in "\n".join(outputs)
        assert any("操作被工作区策略阻止" in tool for call in provider.requests for tool in _tool_messages(call["messages"]))
        print("并发路径：审批提示期间外部替换订单文件；整批未提交，外部版本原样保留，未声称成功。")
    finally:
        temporary.cleanup()


def _assert_cancel_path() -> None:
    cancellation = threading.Event()
    rc, root, temporary, provider, outputs, input_driver = _run_console_case(
        scenario="cancel",
        commands=["/项目说明", f"{CANCEL_MARKER}：准备应用整批文件修改。", "/退出"],
        cancellation_event=cancellation,
    )
    try:
        assert rc == 0 and cancellation.is_set()
        assert input_driver.change_set_approval_seen
        assert len(provider.requests) == 1
        assert (root / AMOUNT_PATH).read_text(encoding="utf-8") == INITIAL_AMOUNT
        assert (root / ORDER_PATH).read_text(encoding="utf-8") == INITIAL_ORDER
        assert "已取消当前模型请求" in "\n".join(outputs)
        assert "测试进程结束" not in "\n".join(outputs)
        print("取消路径：审批阶段收到取消后没有提交文件，也没有再发模型请求。")
    finally:
        temporary.cleanup()


def main() -> int:
    _assert_happy_path()
    _assert_rejection_path()
    _assert_external_conflict_path()
    _assert_cancel_path()
    print("全部合成系统验收通过。仅使用临时项目、确定性 provider 和本地 unittest 进程；没有调用真实 API 或读取凭据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
