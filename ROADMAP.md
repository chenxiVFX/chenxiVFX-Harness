# 功能矩阵与可恢复任务清单

状态定义：**已实现**表示功能存在；**离线可测**表示通过本地模拟/单元测试；**占位**表示只有受限提案或显示；**待实现**表示尚未交付。真实 API、链接逃逸和 shell 隔离不可由模拟结果代替。

| 能力 | 状态 | 当前边界 |
| --- | --- | --- |
| Windows 中文 CLI、帮助与 UTF-8 | 已实现 / 离线可测 | PowerShell 启动脚本、中文命令、UTF-8 I/O；Windows Python 3.13 本机运行 |
| Mock provider | 已实现 / 离线可测 | 默认离线，不发请求、不读密钥 |
| OpenAI 兼容 Chat Completions | 已实现 / 离线可测 | stdlib HTTP、`reasoning_content` 工具往返、usage、`max_tokens` 上限、429 不重试、脱敏错误和边界取消；真实 DeepSeek API 未测 |
| 安全 API 密钥输入与受限真实 smoke | 待实现 | CLI 不读取密钥环境变量；云端迭代需加入隐藏终端输入，单次合成短文本、无工具、最多 128 tokens 的 smoke；不得复用聊天中提供的密钥 |
| Anthropic provider / 更多供应商 | 待实现 | 尚无独立协议适配；当前用户要求的 `deepseek-flash` 只配置了兼容接口 |
| 工作区文件引用与读写 | 已实现 / 离线可测 | 路径与敏感路径检查，单次人工审批，写入差异预览、撤销备份 |
| junction / symlink / hard link 逃逸 | 部分实现 / 未实测 | 检测逻辑和普通路径回归在；本机策略拒绝创建链接，逃逸集成测试跳过 |
| 计划模式 | 已实现 / 离线可测 | 拒绝工具写入；没有 shell 工具 |
| 目标模式 | 已实现（有限） | 目标仅作为不可信上下文；没有计划编排或权限变化 |
| 安全会话保存/恢复 | 已实现 / 离线可测 | `.harness/sessions/`；默认存安全完整轮次；`/检查点` 另存用户手写脱敏摘要，预览确认后才保存；不复制工具结果 |
| 可审核上下文压缩 | 已实现 / 离线可测 | 本地确定性摘要，用户确认后应用；不可信 `user` 消息，工具/引用/敏感轮次不概括 |
| Token / 预算统计 | 已实现 / 离线可测 | 字符启发式估算与可选 provider usage 报告；预算只提示、不限制远端生成 |
| Cache-friendly 稳定前缀 | 部分实现 | 静态 system prompt 和固定工具 schema；没有缓存命中率承诺，也未测远端缓存 |
| 子代理 | 已实现 / 离线可测（Mock 限定） | 最多 3 项、逐项工具层批准；独立输入、一次调用、输入/输出预算、5 秒超时和取消；无工具/递归；结果标为不可信 |
| PTC（Programmatic Tool Calling） | 待实现 / 术语假设 | 暂按“模型组合调用工具/程序化工具调用”记录；未实现脚本或工具执行链 |
| PowerShell/cmd 命令执行 | 待实现 | 当前严格关闭；需要先设计工作区隔离和 Windows 真实进程权限边界 |
| 完整 TUI、快捷键、恢复中断任务 | 待实现 | 当前是标准行式终端循环 |
| 通用模型配置文件、Provider 插件与 PTC 配置 | 待实现 | 目前仅环境变量配置 OpenAI 兼容提供商；密钥不入仓 |

## 本轮验收记录（2026-10-18）

- Windows Python 3.13.15：`python -m compileall -q src tests` 成功；`python -m unittest discover -s tests -v` 共 48 项，45 项通过，3 项链接创建集成用例按策略跳过。（本轮复测后更新）
- `run.ps1 --help` 和无凭据中文模拟 CLI 通过；真实 API 未调用。
- 会话存档只存允许的结构；疑似凭据文本、工具调用/工具结果、文件引用轮次与不完整轮次排除。恢复先校验全部文件后才修改 Agent 状态。
- 压缩由本地确定性摘录完成，预览需明确确认；摘要位于不可信用户消息，最近完整轮次保留，过期工具轮次可在确认后整体丢弃。
- 子代理测试使用 Mock 与故障注入 provider，无真实网络；每个任务在工具层单独审批，失败/超时/取消/预算超限状态相互隔离。
- 检查点只接收用户手写文本并预览脱敏结果，不从工具结果或主会话自动拼接内容；取消确认不落盘。
- 远端 Git 同步仍受 DNS/HTTPS 解析失败限制；本地中文提交会保留，不强推。

## 当前恢复点

- 分支 `main`；第四轮实现已生成中文本地提交，工作树在保全后保持干净。任务目录中仓库外有经 `git bundle verify` 校验的完整历史 bundle。
- 仓库最初经授权克隆尝试失败，随后只在新建独立目录中依据 GitHub 公共页面确认的空仓库状态启动；无其他项目文件被改动。
- 当前工作树为目标仓库唯一开发目标。`.harness/`、`.test-tmp/` 和 Python 缓存均忽略，不放入版本控制。
- Git 工作区 owner 与执行身份不同；每条 Git 命令仅用一次性 `-c safe.directory=<本仓库绝对路径>`。不修改全局 Git 配置。
- 本机 `git ls-remote` 仍报 `getaddrinfo() thread failed to start`。GitHub 授权连接器确认仓库可 push，但 `main` ref 查询返回 409 `Git Repository is empty`。连接器没有 Git pack push 能力；使用文件/对象 API 创建快照会改变原有提交链，因此未走该路径。网络恢复后用普通非强推 Git 同步并核验远端 SHA。

## 风险与未验证项

- 普通 Windows 路径、中文路径、UTF-8 内容、UNC/ADS/遍历拒绝与路径边界已有本机测试。junction、symlink、hard link 测试需创建链接 fixture，当前系统拒绝这些操作；按要求不再尝试创建，不宣称逃逸已实测。
- `pip install -e .` 曾因本地缺少 `setuptools>=68` 且网络不可用而未验证；可继续使用 `run.ps1` 或 `python -m cvfx_harness`。
- 真实 API 需要用户提供安全配置和具体端点；当前没有 API 请求，也没有读取聊天历史或从本机搜索密钥。
- 本地会话文件为明文 JSON，不含工具返回正文或密钥；共享工作区前仍应检查用户主动输入的普通对话内容。

## 下一轮优先级

1. 云端迭代实现终端隐藏密钥输入和单次有界真实 smoke；使用用户重新签发并在本地输入的密钥，不读旧聊天记录或环境变量。
2. 继续测试子代理 Provider 取消的实际中断能力；Mock 已有逐项取消，OpenAI provider 仍仅在 HTTP 请求前/响应后检查。
3. 增加完整的会话恢复/检查点覆盖策略与 schema migration；保留检查点只来自用户手写摘要的边界。
4. 先设计 Windows 沙箱/进程隔离模型，再决定是否实现 PowerShell/cmd。Shell 执行未授权之前保持关闭。
5. 与用户确认 PTC 的确切语义，再选择静态工具组合、脚本受限执行或其他模型；当前语义假设不阻塞基础 CLI。

## 参考项目复用边界

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：公开 GitHub 页面标示 MIT；这里只借鉴公开的模块边界和流程概念，不复制源码。
- [ZCode](https://github.com/zai-org/ZCode)：公开 GitHub 页面标示 Apache-2.0；这里只参考公开 CLI/Agent 概念，不复制源码。
- [OpenCode](https://github.com/anomalyco/opencode)：公开 GitHub 页面标示 MIT；这里只参考 plan/build 权限分层思路，不复制源码。
- `chenxiVFX/chenxiVFX-Harness` 页面曾显示仓库为空、无许可证文件；当前提交均为新写实现。本清单不代替逐文件许可证审查。
