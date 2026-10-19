# 功能矩阵与可恢复任务清单

状态定义：**已实现**表示功能存在；**离线可测**表示通过本地模拟/单元测试；**占位**表示只有受限提案或显示；**待实现**表示尚未交付。真实 API、链接逃逸和 shell 隔离不可由模拟结果代替。

| 能力 | 状态 | 当前边界 |
| --- | --- | --- |
| Windows 中文 CLI、帮助与 UTF-8 | 已实现 / 离线可测 | PowerShell 启动脚本、中文命令、UTF-8 I/O；Windows Python 3.13 本机运行 |
| Mock provider | 已实现 / 离线可测 | 默认离线，不发请求、不读密钥 |
| OpenAI 兼容 Chat Completions | 已实现 / 离线可测 | stdlib HTTP、`reasoning_content` 工具往返、usage、`max_tokens` 上限、429 不重试、脱敏错误和边界取消；真实 DeepSeek API 未测 |
| DeepSeek API key 读取 | 已实现 / 离线可测 | OpenAI 模式读取 `DEEPSEEK_API_KEY`，只允许 HTTPS `api.deepseek.com:443`；Mock 不查询该值，配置页不显示。Codex Cloud Network secret 的 Bearer 头代理替换仍未真实验证 |
| 受限真实 DeepSeek smoke | 待用户配置并确认 | 用户自行在 Codex Cloud Network secrets 添加 `DEEPSEEK_API_KEY`，Allowed domains 限定 `api.deepseek.com`；最多 128 tokens、合成短文本、无工具；本轮没有真实 API 调用 |
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

## 本轮迭代验收记录（2026-10-19）

- Debian 13 / Python 3.12 云端：按实际平台文件系统区分路径大小写断言；Windows 分支仍要求 Harness 解析回磁盘目录项实际大小写，POSIX 分支验证 Harness 保留输入大小写并按实际卷行为读文件。
- OpenAI provider 仅在选择 OpenAI 模式时读取 `DEEPSEEK_API_KEY`，该环境密钥限定 HTTPS `api.deepseek.com:443`；Mock 不查询该变量。认证值不出现在 `/配置`、消息体或 provider 的默认表示中。
- 用合成值、stub 响应及 `127.0.0.1` 本地 HTTP fixture 验证了请求构造与响应解析；没有读取本任务环境的密钥值、没有配置凭据、没有访问外部 API。
- Codex Cloud Network secret 文档说明 placeholder 由 HTTPS 443 代理按 Allowed domains 替换；DeepSeek 对 Bearer Authorization 头的代理兼容性尚未由本项目验证。用户保存环境配置后需 Republish，并在新 task 中验证。
- `compileall` 与中文 mock CLI 冒烟通过；`python3 -m unittest discover -s tests -v` 共 53 项，50 项通过，3 项 Windows 链接创建测试按策略跳过，无失败。
- Windows Python 3.13 本轮未运行；Debian 下 3 项 Windows 专属链接创建测试因平台/opt-in 条件跳过，没有尝试创建链接。

## 当前恢复点

- 当前迭代基于 `main` / `origin/main` 的 `73d9bf5`；本轮提交及远端状态以 `main` / `origin/main` 指针核对。产品代码变更经测试后以中文提交，只允许普通快进推送。
- 本轮只更改 Harness 源码、测试和说明文档；`.harness/`、`.test-tmp/` 和 Python 缓存均忽略，不放入版本控制。
- 本轮没有新增 Windows 链接集成测试，也没有调整系统链接权限；需在允许创建链接的 Windows 环境继续验收。

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
