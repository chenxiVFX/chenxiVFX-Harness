# 功能矩阵与可恢复任务清单

状态定义：**已实现**表示功能存在；**离线可测**表示通过本地模拟/单元测试；**占位**表示只有受限提案或显示；**待实现**表示尚未交付。真实 API、链接逃逸和 shell 隔离不可由模拟结果代替。

| 能力 | 状态 | 当前边界 |
| --- | --- | --- |
| Windows 中文 CLI、帮助与 UTF-8 | 已实现 / 离线可测 | PowerShell 启动脚本、中文命令、UTF-8 I/O；Windows Python 3.13 本机运行 |
| 工作区模型配置与启动优先级 | 已实现 / 离线可测 | `.harness/config/settings.json` 仅存非敏感设置；默认 < 工作区配置 < 环境变量 < CLI；DeepSeek 凭据仍只从环境读取 |
| Mock provider | 已实现 / 离线可测 | 默认离线，不发请求、不读密钥 |
| OpenAI 兼容 Chat Completions | 已实现 / 离线可测 | stdlib HTTP、`reasoning_content` 工具往返、usage、`max_tokens` 上限、429 不重试、脱敏错误；CLI 默认 SSE 流式输出，具备大小/时限/事件边界和取消检查 |
| 有界 SSE 中文流式交互 | 已实现 / 离线可测 | UTF-8 跨块、CRLF/LF 与多行 data；最多 4 MiB 响应、2 MiB 助手文本、512 KiB 行/事件、16,384 个事件、120 秒；工具调用累积并完整校验后沿用 Agent 审批循环；未完成流不执行工具 |
| DeepSeek API key 读取 | 已实现 / 离线可测 | OpenAI 模式读取 `DEEPSEEK_API_KEY`，只允许 HTTPS `api.deepseek.com:443`；Mock 不查询该值，配置页不显示。Network secret 的基本对话与合成目录只读工具闭环均已有限在线验证；其他工具与完整产品流程仍未验收 |
| 受限真实 DeepSeek smoke | 已完成（有限验收） | 独立新环境通过 `api.deepseek.com:443` 的网络 secret 代理，以 `deepseek-flash` 发起一次、128-token 上限、无工具的合成请求；HTTP 200，答复“连接成功”，usage 输入 36 / 输出 43 / 总计 79。只证明认证与基本对话通路，不证明多轮工具闭环 |
| 受限真实目录工具闭环 | 已完成（有限验收） | 两次 `api.deepseek.com:443` / `deepseek-flash` 请求，单次 `max_tokens=256`、合计上限 512；仅批准合成 `synthetic/hello.txt` 的只读列表，HTTP 200×2，usage 输入 1022 / 输出 82 / 总计 1104；不代表完整产品通过 |
| Anthropic provider / 更多供应商 | 待实现 | 尚无独立协议适配；当前用户要求的 `deepseek-flash` 只配置了兼容接口 |
| 多轮工具循环 | 已实现 / 离线可测 | 最多 4 轮、每轮最多执行 4 项，回填工具结果；重复调用编号、格式错误或超预算时关闭当前轮次 |
| 工作区文件引用、目录列表与读写 | 已实现 / 离线可测 | 目录只返回名称/类型且逐次批准；文件读取与写入逐次批准；路径和敏感路径检查、写入差异预览与撤销备份 |
| junction / symlink / hard link 逃逸 | 部分实现 / 跨平台 CI 通过 | Linux 默认运行 symlink / hard link 集成用例；Windows 运行 junction 与可创建的 symlink / hard link 用例；Windows 系统策略拒绝 fixture 时会明确跳过 |
| 计划模式 | 已实现 / 离线可测 | 拒绝工具写入；没有 shell 工具 |
| 目标模式 | 已实现（有限） | 目标仅作为不可信上下文；没有计划编排或权限变化 |
| 安全会话保存/恢复 | 已实现 / 离线可测 | `.harness/sessions/`；默认存安全完整轮次；`/检查点` 另存用户手写脱敏摘要，预览确认后才保存；不复制工具结果 |
| 可审核上下文压缩 | 已实现 / 离线可测 | 本地确定性摘要，用户确认后应用；不可信 `user` 消息，工具/引用/敏感轮次不概括 |
| Token / 预算统计 | 已实现 / 离线可测 | 上下文预算只提示；每次提问的输出预算按请求槽预留并传入 provider `max_tokens`，usage 缺失时仍保证各请求上限之和不超过预算 |
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
- Codex Cloud Network secret placeholder 代理通路已由独立新环境的一次 DeepSeek 认证/对话 smoke 有限验证；工具调用与多轮消息仍未在线验证。
- `compileall` 与中文 mock CLI 冒烟通过；`python3 -m unittest discover -s tests -v` 共 53 项，50 项通过，3 项 Windows 链接创建测试按策略跳过，无失败。
- Windows Python 3.13 本轮未运行；Debian 下 3 项 Windows 专属链接创建测试因平台/opt-in 条件跳过，没有尝试创建链接。

## 多轮工具循环迭代（2026-10-19）

- 增加受批准的只读目录列表：最多扫描 1000 个目录项、最多返回 100 项；跳过敏感/内部路径和链接项，不读取条目正文。
- Agent 逐轮把尚余输出上限传给兼容 provider，最多运行 4 轮、每轮最多执行 4 项；超预算响应、重复调用编号、畸形参数及取消会停止当前轮次并清理未完成上下文。
- 工具结果按调用编号回填；DeepSeek `reasoning_content` 在下一轮请求中保留。末轮工具请求不执行，并回填未执行状态。
- 使用本地模拟 provider、stub HTTP 与临时工作区完成离线覆盖；未调用真实模型、未读取或配置任何密钥。
- Debian / Python 3 环境：`compileall` 通过；`python3 -m unittest discover -s tests -v` 共 60 项，57 项通过，3 项 Windows 链接集成测试跳过，无失败。Windows Python 3.13 未运行。
- 真实 DeepSeek smoke 在独立新环境已由用户确认完成：一次 128-token 上限、无工具的合成短请求成功返回固定中文答复；本轮没有再次调用真实 API，也没有接触凭据。工具调用往返仍只有离线 fixture 覆盖。

## 当前恢复点

- 当前恢复点为本轮提交 `3f2f670`，基于 `c6dab6e`，再基于 `8c7a1e8`；已普通快进推送到 `origin/main`，未重写历史。
- 本轮只更改 Harness 源码、测试和说明文档；`.harness/`、`.test-tmp/` 和 Python 缓存均忽略，不放入版本控制。
- 本轮启用现有 symlink / hard link 集成用例的跨平台自动执行，并让 Windows junction 用例在 Windows 上自动运行；没有调整系统链接权限。

## 风险与未验证项

- 普通 Windows 路径、中文路径、UTF-8 内容、UNC/ADS/遍历拒绝与路径边界已有测试。本轮 Debian 测试实际创建并验证了 symlink 与 hard link 逃逸；Windows junction 和 Windows 链接策略仍待 CI 确认。
- `pip install -e .` 曾因本地缺少 `setuptools>=68` 且网络不可用而未验证；本轮增加独立安装自检和 GitHub Actions 的 Windows/Linux 安装矩阵，具体 CI 结果记录在下方。
- 本轮没有 API 请求，也没有读取聊天历史或搜索、读取、配置任何密钥。之前独立 smoke 只覆盖基本认证对话，不能代替工具循环验收。
- 本地会话文件为明文 JSON，不含工具返回正文或密钥；共享工作区前仍应检查用户主动输入的普通对话内容。

## 下一轮优先级

1. 在新 task 中基于本 checkpoint 验证真实模型的多轮工具请求；先限输出、无写入或逐项人工批准，明确记录在线行为与本地离线结果的差异。
2. 继续测试子代理 Provider 取消的实际中断能力；Mock 已有逐项取消，OpenAI provider 仍仅在 HTTP 请求前/响应后检查。
3. 增加完整的会话恢复/检查点覆盖策略与 schema migration；保留检查点只来自用户手写摘要的边界。
4. 先设计 Windows 沙箱/进程隔离模型，再决定是否实现 PowerShell/cmd。Shell 执行未授权之前保持关闭。
5. 与用户确认 PTC 的确切语义，再选择静态工具组合、脚本受限执行或其他模型；当前语义假设不阻塞基础 CLI。

## 跨平台安装与离线 CI（2026-10-19）

- 新增 `scripts/selfcheck.py`：核对发行包与导入包版本，检查 `python -m cvfx_harness` 和已安装命令的帮助内容，并以隔离环境运行 UTF-8 中文 Mock 会话。不会继承 `DEEPSEEK_API_KEY`，不调用真实 provider。
- GitHub Actions 使用只读仓库权限，在 Windows/Linux 与 Python 3.11/3.12 矩阵中安装项目、运行自检、编译检查和完整 unittest；测试阶段不访问外部模型 API。
- Windows 快速开始通过虚拟环境中的 Python 和 CLI 可执行文件直接运行，不需激活脚本或修改执行策略。
- Debian 13 / Python 3.12：editable install、自检、compileall 和 65 项 unittest 通过（64 通过，1 项 Windows-only junction 用例跳过）；symlink 与 hard link 集成用例均执行通过。
- GitHub Actions 的 `checkout` / `setup-python` tag 均固定到已核对的官方 commit SHA。运行 [39203303235](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39203303235) 的 Ubuntu/Windows、Python 3.11/3.12 四项 job 均以 `completed / success` 结束。
- 安装自检与 CI 的实现提交为 `3f2f67009ec95e74cff8482f69e3110e597d3ba4`；记录 CI 结果的后续提交号见 Git 历史。

## 工作区模型配置（2026-10-19）

- 补齐 provider/model/Base URL/上下文预算/输出上限的工作区持久配置；支持中文 `/配置` 子命令、启动来源展示、环境变量与 CLI 高优先级覆盖、损坏配置的 `--ignore-config` 恢复入口。
- 配置路径为 `.harness/config/settings.json`，严格 schema，不保存 API key；拒绝未知/凭据字段、userinfo、query、fragment、危险 scheme、非 DeepSeek host 和非 443 端口，错误不回显输入值。
- 目前仅支持 Mock 和 DeepSeek OpenAI 兼容模式；Anthropic/其他服务明确未实现。DeepSeek 网络密钥的 HTTPS `api.deepseek.com:443` 目标限制保持强制。
- 配置测试仅使用临时工作区、合成数据和 patched provider；不读取或写入密钥、不发真实请求。
- 测试统计审计：`c6dab6e`、`3f2f670`、`cacfb7c` 均有 65 个测试方法，比较中没有删除或合并；旧版三个链接用例受 opt-in gate 跳过，因此“65 通过、3 跳过”措辞错误，按代码口径应是 65 总、62 通过、3 跳过。当前添加 14 个配置测试后共 79 项。
- Debian 13 / Python 3.12 当前本地结果：79 总、78 通过、1 跳过（Windows-only junction）；symlink 与 hardlink 集成测试均执行通过。远端 Windows/Linux 矩阵将在推送后记录；未调用真实 API。

## SSE 流式交互（2026-10-19）

- OpenAI 兼容 CLI 改为有界 SSE 流式输出；增量 UTF-8 解码处理跨块中文，解析 LF/CRLF、跨块行结束符和多行 `data`，要求完整 `[DONE]` 及 finish reason。
- 上限：4 MiB 流响应、2 MiB 助手文本与推理、512 KiB 单行/事件、16,384 个事件、120 秒总时限；响应体读取期间最多每 250 ms 检查取消。建连和响应头等待使用 provider timeout。
- 文本先做终端控制字符可见转义及疑似凭据脱敏。错误只显示固定安全说明/HTTP 状态，不显示响应体、请求头、URL 或原始异常。
- 支持文本、内部保留但不显示的 `reasoning_content`、单候选项与函数工具调用。工具增量完整组装和 JSON 校验后才返回 Agent；截断/畸形流会清除当前未完成轮次，不执行未完成工具。
- 明确拒绝 Mock/自定义 provider 的 SSE 请求、多个候选项、多模态/logprobs/未知增量字段和缺少完成标记的流。流式文本可能先于完成标记显示；错误后不自动撤回已显示片段。完整产品验收仍不在单元测试范围内。
- 新增 loopback HTTP fixture 与纯解析器用例，涵盖跨字节中文、工具往返、截断不审批、错误脱敏、大小/时间上限和取消；fixture 不含 Authorization 头。未访问真实模型或读取配置凭据。
- Debian / Python 3.12：`python -m unittest discover -s tests -v` 共 90 项，89 通过、1 项 Windows-only junction 跳过；`compileall`、editable install、安装自检及 `git diff --check` 通过。
- 当前工作树未调用真实模型 API、未读取环境凭据；SSE HTTP 用例只访问本机 loopback，且不发送 Authorization 头。四矩阵 GitHub Actions 链接待推送后补录。
- 真实 API 用量账本（父任务累计）：历史已用 1,222 tokens（79 + 1,104 + 39）；本轮预分配上限 20,000，实际请求 0 次、失败 0 次、用量 0；累计仍为 1,222 / 2,000,000，余 1,998,778。未读取凭据或 Network secret。

## 参考项目复用边界

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：公开 GitHub 页面标示 MIT；这里只借鉴公开的模块边界和流程概念，不复制源码。
- [ZCode](https://github.com/zai-org/ZCode)：公开 GitHub 页面标示 Apache-2.0；这里只参考公开 CLI/Agent 概念，不复制源码。
- [OpenCode](https://github.com/anomalyco/opencode)：公开 GitHub 页面标示 MIT；这里只参考 plan/build 权限分层思路，不复制源码。
- `chenxiVFX/chenxiVFX-Harness` 页面曾显示仓库为空、无许可证文件；当前提交均为新写实现。本清单不代替逐文件许可证审查。
