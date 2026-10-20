# 确定性项目交互验收

## 可复现范围

运行 `python scripts/system_acceptance.py` 会通过真实 `run_console`、主 `Agent`、工作区审批、计划存档和外部测试 runner 驱动一次确定性离线流程。脚本只把 `provider_from_environment` 替换为固定回复 fixture；没有提供访问模型网络的调用路径，也不读取凭据。每个案例都在新建的临时目录中运行，完成后删除。

主案例建立含中文和空格目录名的合成 Python 项目，包含 `商城/金额.py`、`商城/订单.py` 和 `测试 用例/test_invoice.py`。项目约定 `AGENTS.md` 规定每行 Decimal 含税金额按 `ROUND_HALF_UP` 取整到分，再汇总已取整行。现有 unittest 验证 0.005 的半入规则及两行总额。流程依次确认项目说明、提交仅本次使用的结构化问卷回答、生成并接受计划、读取并搜索文件、审阅一次两文件变更集、运行既有 unittest。fixture 的第一版故意使用 `ROUND_HALF_EVEN`；验收要求用户界面显示真实的 exit 1 与断言失败，随后主 Agent 读取现有文件、提交当前 SHA-256 对应的精确补丁并重跑同一测试，最后只在实际 exit 0 / `OK` 后报告通过。

独立案例验证：

- 拒绝两文件变更集后，两文件内容仍为原始字节且没有生成备份；模型收到拒绝结果，不得声称写入成功。
- 审批提示期间通过本地输入 fixture 修改第二个文件；整批提交应因版本冲突中止，外部版本保留，第一文件也不变。
- 在审批阶段设置取消事件；没有写入文件，也没有发起后续 provider 请求。

主案例还会保存一个合成会话并直接检查 JSON：未获准持久化的问卷答案和临时项目说明正文不能进入会话存档。`/计划` 显示步骤仍为待办；主会话中的独立修复和测试不会自动把计划标成已完成。

## 平台验证

脚本接入 `.github/workflows/offline-ci.yml`，在 Ubuntu/Windows 和 Python 3.11/3.12 四格运行；也接入 Windows standalone workflow。Windows standalone workflow 先用源码流程执行本脚本，再构建并运行现有的 EXE smoke：帮助、UTF-8 Mock 与中文路径、单次 JSON、合成 TUI、冷启动计划续跑，以及通过明确选择的外部 Python 运行合成 unittest/pytest。两层验收互补：确定性主 Agent/provider 注入只在源码测试进程中进行；独立 EXE 不开放脚本注入接口，EXE smoke 验证它当前公开支持的离线功能。

当前本地 Linux 验收记录：主流程首轮 unittest exit 1、定向修复后 exit 0；拒绝、并发冲突、取消三例均保留预期磁盘状态。脚本和完整离线测试由精确 SHA 的 CI 再提供 Linux/Windows 及两个 Python 版本证据。

## 限制

这是合成项目和固定 provider 回复，不是模型质量评估、真实 API/usage 验证、用户项目测试或 OS 沙箱证明。子进程只运行刚创建的 unittest fixture；没有运行仓库本身以外的用户测试。它验证的是 CLI/Agent/审批/test-runner 集成闭环，并不声称跨文件写入具有事务原子性。计划步骤的状态仍需用户检查并显式更新。
