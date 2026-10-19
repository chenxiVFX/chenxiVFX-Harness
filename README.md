# chenxiVFX Harness（最小原型）

Windows 优先、中文交互的 AI 编程命令行工具。当前版本包含离线模拟模型、工作区持久化运行配置、DeepSeek OpenAI 兼容 Chat Completions 适配、受限多轮工具调用、工作区文件读取与目录列表、经逐次批准的文件写入、差异预览和撤销、文件引用、有界用户计划步骤、本地会话存档、可审核的上下文压缩、Token 估算和受限只读子代理。

这不是 Claude Code 或 Codex CLI 的完整复制品。功能状态和后续清单见[功能矩阵与可恢复任务清单](ROADMAP.md)，架构边界见[架构说明](ARCHITECTURE.md)。

## 环境与安装

需要 Python 3.11 或更新版本。交互式 TTY 使用固定版本的 `prompt-toolkit` 与 `wcwidth`；核心 provider、工作区与审批逻辑仍使用 Python 标准库。Windows PowerShell 中，在仓库目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\cvfx-harness.exe --help
.\.venv\Scripts\cvfx-harness.exe --workspace . --provider mock
```

无需激活虚拟环境或修改 PowerShell 执行策略。启动后可输入 `你好` 测试中文交互，再输入 `/退出` 结束；`mock` 模式完全离线，不读取密钥，也不会发送网络请求。安装后可运行自检：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck.py
```

也可以直接用当前 Python 启动仓库中的脚本：

```powershell
.\run.ps1 --help
.\run.ps1 --workspace . --provider mock
```

安装后可运行 `cvfx-harness --workspace . --provider mock` 或 `python -m cvfx_harness`。默认提供商为 `mock`，不会读取密钥或发起网络请求。双向 TTY 默认启动全屏中文界面；非 TTY、`TERM=dumb` 或 `--plain` 使用原纯文本 CLI。

### 交互式终端

输入区固定在底部，上方保留会话输出；新文本会跟随到底部，手动向上滚动后不会被新输出拉回。行首输入 `/` 会在输入提示上方的专用行区筛选中文命令，终端高度不足时裁剪候选项并保留输入提示/状态栏。↑/↓ 选择，Tab 填入，Enter 提交输入；Esc 先关闭建议，再取消当前输入。`/模式 ` 只补全固定选项。输入文件引用时，TUI 可按工作区相对路径建议安全的文件和目录名称；补全有扫描/结果上限，只显示名称与类型，不读取正文、不批准读取。敏感、内部、链接和硬链接项不会显示。

空会话在输出区中央显示两行简短引导，首条输入提交后收起。界面用统一的青绿色强调品牌、输入焦点与当前选择；候选简介使用低对比辅助色，状态栏只保留当前关键操作，并按窄终端宽度缩短。

Enter 提交当前输入，Ctrl+J 插入换行；TUI 生成期间会暂停输入，Ctrl+C 取消当前请求，不会吞掉后台输入草稿。空闲时 Ctrl+C 首次清空草稿，再次按下会取消当前输入或请求。审批面板显示精确动作和 diff，单次操作默认拒绝；“当前”会写在按钮文字中，↑/↓、Tab 或鼠标可选择按钮，并同步审批焦点，再按 Enter 确认。审批不接受文字或多行粘贴，Esc 取消。点击建议只会填入候选，点击审批选项只切换选择，不会执行动作。按钮文字同时标记选择状态，因此无色终端也能辨认。不会保存输入历史或审批答案，也没有永久授权模式。

启用 TTY 全屏渲染后，终端的 Unicode 字符宽度、鼠标能力与输入法组合仍取决于宿主。当前项目在 CI 中使用 prompt_toolkit 的合成输入和 Linux PTY 验证键位、布局启动/退出；Windows runner 使用其 Win32 输入模拟验证。尚未完成 Windows Terminal、PowerShell/ConPTY 和各中文 IME 组合的人工兼容验收。

查看 [实际 Linux PTY 合成会话录屏](evidence/tui-synthetic-session.cast)及其[关键帧联系表](evidence/tui-keyframes.png)，其中包含主界面、命令候选高亮、多行草稿、审批预览和拒绝结果；各张 PNG 原图与回放说明见[证据说明](evidence/README.md)。

### Windows 独立程序试验

仓库的 `Windows standalone packaging experiment` Actions 工作流会在 Windows x64 runner 上构建一个含 Python 运行时的单文件 `.exe`，随后在不依赖系统 Python 的子进程环境中检查 `--help`、离线 Mock 中文交互和含中文/空格的工作区路径。构建工具仅用于 CI，版本和 Windows 轮子哈希锁定在 [`packaging/windows-build-requirements.txt`](packaging/windows-build-requirements.txt)；应用的 TUI 运行依赖固定在 `pyproject.toml`。

从对应 Actions 运行页面下载 `windows-x64-cvfx-harness-py312` 文件，解压后可运行 `cvfx-harness.exe --help` 或 `cvfx-harness.exe --workspace . --provider mock`。单文件启动时会把依赖解压到系统临时目录，启动会比源码或目录包慢一些，且需要临时目录可写。包中包含可执行文件、中文说明、第三方许可证通知和 SHA-256 校验文件；CI 构建不打包仓库文档、配置或工作区文件。构建与自检仅使用 Mock 和合成输入，不连接真实模型。这个未签名包只是试验，不是正式发布；Windows 可能显示 SmartScreen 或 Defender 提示，请保持系统保护开启并遵循组织策略。Actions 文件保留 14 天，不上传到发布页或包注册表。

## 可用命令

在交互提示符输入 `/帮助` 查看完整说明。

| 命令 | 用途 |
| --- | --- |
| `/命令` | 列出可用中文命令；`/帮助` 显示参数与交互说明 |
| `/状态`、`/配置` | 查看运行配置；`/配置` 还可设置提供商、模型、端点和持久预算；密钥值不显示 |
| `/模式 计划`、`/模式 执行` | 计划模式会阻止工具写入；执行模式仍逐次询问 |
| `/目标 <内容>`、`/目标 清空` | 设置或清除仅作为上下文的会话目标 |
| `/计划 添加 <步骤>`、`/计划` | 添加/查看最多 3 条用户编写的步骤及 `pending`、`in-progress`、`completed`、`blocked`、`cancelled` 状态 |
| `/计划 执行 <编号>`、`/计划 完成 <编号>` | 确认后只读执行一步；结果需用户检查后才能标记完成 |
| `/计划 阻塞|取消|重试 <编号>`、`/计划 清空` | 更新步骤状态、重试失败步骤或确认清除计划 |
| `/保存 [名称]`、`/恢复 <名称>`、`/会话` | 保存安全对话、确认后恢复、列出工作区会话；TUI 输入 `/恢复 ` 后可用 Tab/方向键选择会话 |
| `/检查点 <名称>` | 手写进度摘要，经脱敏预览和确认后保存；不自动收集工具结果 |
| `/压缩 [保留轮数]` | 先预览本地摘要和将丢弃的旧轮次，确认后再应用 |
| `/统计`、`/预算 [tokens]` | 查看当前估算上下文、累计用量和提供商报告；`/预算` 临时设置本轮预算 |
| `/子代理 提交 [--读取 路径 ... --] 任务甲 \| 任务乙` | 每项单独批准后提交最多 3 个隔离子任务；最多 4 个精确只读路径 |
| `/子代理 列表 [编号]` | 查看子任务状态和本会话共享预算 |
| `/子代理 取消 <编号>` | 取消排队或运行中的子任务 |
| `/子代理 收取 [编号\|全部]` | 将终态结果汇总为不可信上下文加入主对话 |
| `/子代理 时限 <秒>` | 设置之后提交的子任务运行时限，范围 5–120 秒 |
| `/撤销` | 预览并确认恢复最近一次 Harness 文件写入 |
| `/多行` | 逐行撰写一个问题；单独输入 `/提交` 发送，`/取消` 或 Ctrl+C 丢弃 |
| `/退出` | 结束会话 |

`/多行` 以换行合并文本，最多 64 KiB UTF-8；只有单独一行 `/提交` 会发送。无 TTY 时可直接把命令和多行块逐行写入 stdin；EOF 会结束会话，未结束的多行块会丢弃。Harness 不保存输入历史或审批回答。纯文本模式不做补全。交互 TUI 补全固定中文命令、固定枚举、安全工作区相对路径，以及 `/恢复 ` 后的本地会话名；路径和会话名建议最多扫描 1000 个目录项、显示 50 个候选，不读取会话内容。Tab 只填入选择，按 Enter 提交恢复命令；鼠标只选择，不会自动提交。恢复的是经过校验的历史上下文，旧审批和运行授权不会恢复，旧工具调用不会执行；替换已有上下文前仍需确认。文件路径补全不会执行命令，也不会批准动作。

在提问里用 `@相对路径` 引用文件；路径含空格时写 `@{资料/需求 文档.txt}`，多行问题中也可使用。TUI 中输入 `@{资料/需` 后按 Tab 可选中文件或目录；目录补全后继续输入，文件补全会补上 `}`。不含空格的名称也支持 `@资料/READ`。补全只列安全名称，不读取文件正文；选中的文件仍需单独批准后才读取，内容随后会发送给所选提供商。模型也可请求逐次确认的目录列表，列表只包含名称与类型，不读取条目正文。EOF 时文件/目录审批默认拒绝。敏感路径、工作区外路径、链接逃逸和非 UTF-8 文本默认拒绝。当前没有 shell 执行工具，PowerShell/cmd 命令不会执行。

## 工作区配置

运行设置保存在工作区 `.harness/config/settings.json`，该目录由 `.gitignore` 排除且模型文件工具无法读取。配置只接收非敏感字段，不接受 API Key 或其他凭据。优先级从低到高为：内置默认值、工作区配置、`HARNESS_*` 环境变量、命令行参数。支持的环境变量为 `HARNESS_PROVIDER`、`HARNESS_MODEL`、`HARNESS_BASE_URL`、`HARNESS_TOKEN_BUDGET` 和 `HARNESS_MAX_OUTPUT_TOKENS`。

交互配置示例：

```text
/配置 提供商 openai
/配置 模型 deepseek-flash
/配置 端点 https://api.deepseek.com
/配置 预算 32768
/配置 输出上限 2048
/配置
```

`提供商`、`模型`和`端点`保存后需重启才会切换本轮 provider；预算配置在没有更高优先级覆盖时立即应用。`/预算 16384` 只改当前会话。用 `/配置 模型 默认` 或 `/配置 端点 默认` 清除相应工作区覆盖；`/配置 重置` 写回默认值，但环境变量和命令行仍优先。若配置文件损坏，可用 `--ignore-config` 忽略它启动，再通过 `/配置` 保存新设置。

当前支持离线 `mock`、DeepSeek OpenAI 兼容模式，以及独立的 `deepseek-anthropic` DeepSeek Anthropic 兼容模式。后者固定使用 `https://api.deepseek.com/anthropic` 路径，只接受 `deepseek-flash` 与 `deepseek-v4-pro`，不会连接官方 Claude 服务，也不接受 Claude 模型别名。两种 DeepSeek provider 只读取 `DEEPSEEK_API_KEY`；Anthropic 兼容模式不会查询 `ANTHROPIC_API_KEY`。凭据仍只从环境读取，绝不写进配置文件；请求仅发往固定 DeepSeek HTTPS 主机与协议路径，不接受重定向。不得把密钥放入 URL、命令行、项目文件、聊天或 Git。

同样的非敏感设置也可在启动时用 `--provider`、`--model`、`--base-url`、`--token-budget` 和 `--max-output-tokens` 覆盖。超长或格式错误的输入不会被错误消息回显。配置文件损坏时可这样绕过并继续使用 Mock：

```powershell
.\.venv\Scripts\cvfx-harness.exe --workspace . --ignore-config --provider mock
```

## 会话、压缩和 Token 统计

存档放在工作区 `.harness/sessions/`，为 UTF-8 JSON。`/保存` 只保存完整且未触发工具的普通用户/助手对话、受标记的本地历史摘要、非敏感目标文本和计数。工具调用及结果、文件引用轮次、不完整轮次、疑似凭据文本不会存档；为避免助手复述工具结果，引用或工具调用后的回复也会从默认保存中排除。恢复前会先完整校验；损坏存档不会替换当前上下文。存档是本地明文文件，未加密，请勿共享含敏感内容的工作区。

如需保留这类会话的进度，可运行 `/检查点 名称`，然后手写一段摘要。Harness 不会从当前消息、文件引用或工具结果生成该摘要；已知密钥/Token 形态会先脱敏，程序展示脱敏后的确切文本并再次要求确认。检查点只保存这段摘要和 token 计数，不保存目标、原始工具输出或其他上下文。检查点恢复后仍是带不可信标记的用户消息。

`/压缩` 会展示旧对话摘要和估算变化，用户确认后才修改上下文。摘要保留当前目标（对疑似凭据形态先脱敏）和可识别的完整用户限制；其他旧对话只留短摘录，摘要最多 32 KiB UTF-8，超限时拒绝压缩。最近轮次作为完整消息组保留，旧工具轮次只会整轮丢弃；遇到未完成请求或工具调用/结果配对异常时拒绝压缩。摘要始终作为带不可信标记的 `user` 消息，并明确历史审批不延续为当前授权。系统提示和工具 schema 的顺序保持固定，便于复用稳定请求前缀；程序不声称或保证任何缓存命中率。

Token 数由本地字符启发式估算，不是提供商 tokenizer 的精确数值。`/统计` 另显示固定系统提示与工具 schema、会话消息的独立估算分项；分项不相加，也不是缓存命中量。provider 明确返回 `prompt_cache_hit_tokens` 时才统计缓存输入，否则显示暂无报告。`--token-budget`（默认 16384）和 `/预算` 只是上下文提示；`--max-output-tokens`（默认 2048）为单次提问的所有模型回复共享输出预算，`--max-model-requests` 可把每次提问限制为 1–4 次请求（默认 4），不会自动重试。每次请求按剩余预算和剩余请求数分配 `max_tokens`。`--request-timeout` 为每个 SSE 请求设置从建连开始的总时限，默认 45 秒、硬限 120 秒。若 provider 返回 usage，统计会记录报告值；缺失时按该请求完整 `max_tokens` 额度保守扣除。字符估算不是 provider tokenizer，不能保证远端计量精确；当前不计算金额或推测动态价格。启动参数示例：`.\run.ps1 --token-budget 32000 --max-output-tokens 2048`。

委派计划每个会话最多 3 项，每项在启动前单独审批；任务最多 240 字符、最多 2 个并发子代理、每项最多 2 轮模型请求、首轮最多 2 个工具调用、单任务输出最多 256 tokens、单请求输入估算最多 512 tokens、共享输入估算最多 3072 tokens。子代理运行时限默认为 60 秒，可用 `--subagent-timeout` 或 `/子代理 时限 <5–120>` 调整；这是每项任务的硬上限，从该任务首个 provider 请求开始计时，审批与执行前排队不计入。单个 SSE 请求仍受 `--request-timeout` 限制，默认 45 秒、上限 120 秒；取消在响应读取期间最多约 250 ms 检查一次。Mock、OpenAI 兼容和 DeepSeek Anthropic 兼容 provider 共用 `complete_subagent` 边界；Anthropic 兼容路径的远端服务行为尚未在线验证。子代理默认不获文件工具；用户可用 `--读取` 明确预授权最多 4 个工作区内现存文件或目录，每次委派审批会展示精确任务、工具和时限。文件只允许 `read_file`；目录只允许列名称与类型。子代理不能读范围外路径、写入、执行 shell、递归委派，也不会继承主会话、目标、文件引用、工具结果或父代理的审批。共享输入上限用本地估算执行，失败请求会保留输入预留；获批任务的输出上限会预留、不因失败释放。结果会先脱敏，再以不可信 JSON 汇总；`收取` 将它作为不可存档的 user 上下文加入父对话。失败、超时、取消和预算耗尽会显示为不同状态。

示例：`/子代理 提交 --读取 src/app.py -- 检查路由边界 | 梳理配置校验`。如任务不需要文件，可用旧写法 `/子代理 检查路由边界 | 梳理配置校验`。提交后用 `/子代理 列表` 查询，`/子代理 取消 S01` 取消单项，`/子代理 收取 全部` 收取已结束结果。子代理文件范围必须在当前工作区内，且不递归扩展目录；需要其他文件时重新提交明确范围并再次审批。

### 用户计划步骤

`/目标` 设置的内容仍只是上下文，不会让模型自行拆分目标或增加步骤。用户用 `/计划 添加 <步骤>` 编写最多 3 个、每个最多 400 字符的步骤；整个 UTF-8 计划存档上限为 4 KiB。步骤文本和状态保存在工作区 `.harness/plans/current.json`；不会保存模型响应、文件内容、工具调用结果或审批记录。状态只能按工作流转换，完成状态必须由用户审阅后显式设置。

`/计划 执行 <编号>` 会先展示该步并要求确认。每次只启动一个全新 Agent，其中只包含脱敏后的当前目标上下文和该条步骤；最多 2 次模型请求，输出上限 512 tokens。读取文件或列目录仍逐次批准；写入在工具层拦截，不会请求写入审批；没有 shell、撤销或子代理入口。正常执行后步骤留在 `in-progress`，用户检查输出后再 `/计划 完成 <编号>`。模型/传输失败会标为 `blocked`，用户或请求取消会标为 `cancelled`，可 `/计划 重试 <编号>` 后再次确认。重启时遗留的 `in-progress` 自动退回 `pending`，不会自动继续；恢复执行也要重新确认。每次重试仍有相同单步预算。

## DeepSeek OpenAI 兼容模式

适配器默认端点为 `https://api.deepseek.com/chat/completions`，模型默认为 `deepseek-flash`；工作区配置、环境变量和 CLI 参数可设置非敏感字段。选择 OpenAI 模式后，程序只读取 `DEEPSEEK_API_KEY`；Mock 模式不会查询这个变量。非空 DeepSeek 密钥无论来自环境还是显式覆盖，都只允许发往 `https://api.deepseek.com:443`；带认证的 OpenAI 兼容与 Anthropic 兼容请求均拒绝 HTTP 重定向，避免认证头转发到重定向目标。`/配置` 会显示变量名，不显示值；认证头不会进入模型消息、会话或 Harness 日志。HTTP 仅对本机 loopback 开放且仅供本地合成 fixture 使用；无凭据 provider 只允许 loopback。请勿把密钥放进 URL、命令行、项目文件、聊天或 Git。

本地 CLI 尚无隐藏输入界面；若直接使用普通环境变量，原值会进入 Harness 进程。若以后要在 Codex Cloud 使用 DeepSeek，用户可自行在该环境的 **Network secrets** 中添加变量 `DEEPSEEK_API_KEY`，Allowed domains 设为 `api.deepseek.com`。[Codex Cloud 官方说明](https://learn.chatgpt.com/docs/environments/cloud-environments)指出，Network secret 会以 placeholder 交给程序，并由代理在允许目标的 HTTPS 443 请求中替换原值，不把原始值放入本地进程或文件；普通 Environment variable 的值对 task 可见。保存环境改动后需要 Republish 并新建 task。网络密钥须使用自己的 Key，不能与同名直接环境变量冲突。

离线回归使用合成数据和本机 HTTP fixture。曾完成无工具连接 smoke 和受限只读目录闭环，只验证当时的认证与授权路径；测试只使用合成提示和合成目录信息，不代表完整产品或其他工具已通过。真实调用用量单独跟踪。HTTP 429 不自动重试；适配器只统计 provider 返回的 Token usage，不核算费用。直接调用保留的非流式 JSON `complete()` 时，取消信号只在发送前和收到响应后检查；CLI SSE 流在响应体读取期间每 250 ms 检查一次，建连/响应头仍受 provider 超时限制。

## DeepSeek Anthropic 兼容模式

`--provider deepseek-anthropic` 使用 DeepSeek 官方记录的 Anthropic-compatible API 入口 `https://api.deepseek.com/anthropic`，内部只请求 `/v1/messages`；模型限制为 `deepseek-flash` 和 `deepseek-v4-pro`。选择该模式时 Harness 只读取 `DEEPSEEK_API_KEY` 并通过 `x-api-key` 请求头发送，不会读取 `ANTHROPIC_API_KEY`。固定目标 HTTPS 使用标准证书校验，HTTP 重定向不会跟随。官方 Claude 服务、Claude 账号、Claude 模型别名、自定义主机和其他 API 路径均不支持。详情参见 [DeepSeek Anthropic API 指南](https://api-docs.deepseek.com/guides/anthropic_api/)；DeepSeek 文档说明了兼容功能，但没有枚举其 SSE 事件序列和 usage 响应字段，因此事件序列按[Anthropic 官方 SSE 规范](https://platform.claude.com/docs/en/build-with-claude/streaming)实现。定向远端验收只核对过一次固定合成提示的基础文本对话；工具循环和其他事件组合尚未远端验证。

请求使用 Anthropic Messages 的 `system`、文本消息、工具 schema、`tool_use` / `tool_result` 往返与 `auto` 工具选择。模型侧 thinking 默认关闭（见 [DeepSeek thinking mode 文档](https://api-docs.deepseek.com/guides/thinking_mode/)）；若响应仍含标准 `thinking` 块，Harness 不显示其内容，并在当前内存上下文中原样保留文本和签名供工具轮次使用。只接受文本、带有效签名的 thinking 和标准 `tool_use` 块；图片、多模态、`server_tool_use`、`redacted_thinking`、文档/搜索/MCP 等内容，以及未知 SSE 事件、字段或增量均明确拒绝。DeepSeek 文档列出部分这些 block 为已支持或忽略，但 Harness 当前没有对应本地执行或保真路径，因此不会静默丢弃。通过完整消息结束标记、参数 JSON 与现有 Agent 预算校验后，工具仍逐次走本地审批；只读子代理限定原有精确路径规则。Mock 不会被拿来模拟该 provider；若某个 provider 没有流式能力，CLI 会明确拒绝流式请求。

Anthropic SSE 使用与现有 provider 相同的有界 UTF-8/SSE 解析器：跨网络块处理中文和 LF/CRLF 边界，限制 4 MiB 响应、2 MiB 助手内容、512 KiB 单行/事件、16,384 个事件及最多 120 秒总时限；响应读取时约每 250 ms 检查取消。只有收到完整 `message_stop` 才返回工具调用；错误或截断响应不会触发该轮工具。标准 `input_tokens` / `output_tokens` 字段若出现会用于统计，但 DeepSeek 尚未在该兼容指南中明示这两个响应字段；usage 缺失或无效时按该请求完整 `max_tokens` 额度保守扣除，不推测缓存命中。终端输出沿用控制字符可见转义与疑似凭据脱敏；网络错误不显示原始异常、请求头、URL 或响应体。非流式 `complete()` 存在，但它仅在发送前和收到响应后检查取消，CLI 使用有界 SSE。

本地协议回归使用 loopback 合成 HTTP fixture 与离线测试。另做过有限范围的真实用户流程核对：OpenAI 兼容 SSE 对一个精确批准的合成文件执行写入并逐字节读回；只读子代理仅获准读取一个合成文件，结果被收取并加入父对话的不可信、不可存档上下文；Anthropic 兼容 SSE 完成基础合成文本对话并返回 usage。流程没有使用项目文件、用户数据或私人聊天，也没有执行生成代码。它们只覆盖上述路径，不构成完整产品验收；逐请求用量留在私有账本，不提交到仓库。

### 有界 SSE 流式交互

CLI 的 OpenAI 兼容模式和 DeepSeek Anthropic 兼容模式均使用有界 SSE 逐段显示中文文本；UTF-8 解码器和事件解析器可跨任意网络分块处理中文及 LF/CRLF 行界。OpenAI 兼容流要求完成状态及 `[DONE]`；Anthropic 兼容流按 `message_start`、content block、`message_delta`、`message_stop` 校验。响应上限为 4 MiB、助手文本和推理合计上限为 2 MiB、单行/单事件上限为 512 KiB、最多 16,384 个事件、总时限最多 120 秒。取消事件在响应体读取期间每 250 ms 检查一次；建连和响应头等待受 provider 超时限制。网络错误只附加固定分类、阶段、HTTP 状态、已收字节数和耗时；POSIX `ETIMEDOUT` / `EAGAIN` 和 Winsock `10060` / `10035` 均按超时阶段归类；不显示原始异常、请求头、URL 或响应体。响应由上下文管理器关闭，HTTP 状态错误也会显式关闭。

SSE 保留 Python `urllib` 的 HTTP 代理 CONNECT 和 TLS 验证路径，调用线程用 `read1()` 读取，单个 watchdog 在取消或截止时间到达时 shutdown 当前响应 socket，并在退出时停止和 join。正常生成停顿不会触发短超时后重读，真正的读取超时直接终止；未使用 `select`。若响应不提供可用于有界读取的 socket，会明确报错。建连、代理 CONNECT、TLS 和响应头仍依赖网络栈超时，取消需等这些调用返回才能检查；这些阶段不能保证与响应体同样的主动硬截止边界。离线测试通过本机 CONNECT 代理和测试专用证书检查代理隧道、TLS 主机名与证书校验、正常停顿、分块头慢滴、中文、取消与截断；测试证书只为 `sse-fixture.invalid` 使用，客户端仍要求有效证书且开启主机名检查。普通 JSON `complete()` 仍走非流式 `read()`，其取消检查只在发送前和收到响应后执行。

工具调用片段会先在内存中累积并校验，收到协议完整结束标记后才交给既有 Agent 循环；工具仍逐次审批。截断、无效或超限流不会返回不完整工具调用，因此不能触发该轮工具。当前支持 OpenAI 兼容的单候选文本、`reasoning_content` 和函数工具调用，以及 DeepSeek Anthropic 兼容的文本、签名 thinking 与标准工具块；不支持多模态、`logprobs`、未签名/未知增量和其他 Anthropic 内容块。Mock 默认离线且不使用 SSE；请求流式输出的 provider 若没有 `complete_streaming` 会明确拒绝。流式文本可能在完整状态尚未到达前已显示；取消或传输失败后不完整轮次不会写入会话，已显示片段不会自动撤回。控制字符会以可见转义显示，疑似凭据片段在终端输出前脱敏。单元与本机 fixture 测试不等于完整产品验收；Anthropic 在线证据目前只覆盖一次基础合成对话，工具轮次、thinking 和错误/取消事件仍未远端验证。

## 权限与边界

- 文件工具只接受工作区内相对路径；拒绝绝对路径、UNC、ADS、父目录遍历、敏感路径、reparse point 和可识别的硬链接逃逸。只读目录工具逐次审批、限制扫描量和返回条目数、不跟随目录链接。
- 模型提出 `write_file` 后，本地先检查路径和 UTF-8 文本内容，再逐项展示完整差异；提示明确标为“新建文件”或“覆盖已有文件”，只有本次输入 `y`、`yes` 或 `是` 才批准。多项写入各自询问，不会沿用上一次确认。EOF、Ctrl+C、拒绝或其他输入都不会写入。
- 写入拒绝删除/清空已有文件、无效 UTF-8、NUL/控制字符二进制内容、超 64 KiB 内容、缺失父目录、链接路径、敏感路径和工作区外路径。审批后会重新解析路径并核对原文件字节；完成核对后保存撤销备份，再次检查文件版本，并通过同目录原子替换写入。`/撤销` 自身也需要单独预览和确认。
- POSIX 写入通过不跟随链接的目录文件描述符定位父目录并相对原子替换。Windows 使用 reparse-point 检查和路径原子替换；Python 标准库没有对应的目录句柄相对替换接口，因此无法完全消除同一用户进程并发替换目录时的竞态。
- 文件读取、引用、目录列表、写入和撤销均由本地执行代码逐次检查；计划模式在工具层拒绝模型写入。工作区私有 `.harness/` 状态不能由模型工具改动，模型没有删除、shell 或任意代码执行工具。

实际修改时，在 `/模式 执行` 下直接描述单文件更改。若模型提出 `write_file`，CLI 会先显示新建/覆盖类型及完整 diff，再等待本次单独确认；检查差异后输入 `y` 写入，其他输入都拒绝。要恢复已批准写入，运行 `/撤销` 并再次审阅差异确认。该工具只编辑文本，不会运行或验证项目代码。
- 模型输出、文件内容、工具结果、目标文本和历史摘要都是不可信数据，不能修改权限规则。未知工具一律拒绝。
- 单次提问最多 4 轮模型回复、每轮最多执行 4 个工具调用、每个模型响应最多接收 16 个调用；输出预算默认 2048 个 provider completion tokens。按剩余请求槽分配每次 `max_tokens`；缺少 usage 时完整扣除该请求额度。重复调用编号、格式错误参数或超过单次额度的响应会在任何工具执行前终止当前轮次。
- 当前没有 shell、通用命令执行、隔离 worktree、PTC 执行或官方 Claude provider。Anthropic 消息协议只作为 DeepSeek 的兼容接口使用。子代理只允许两种只读工具，并且仅限用户预先批准的精确路径。
- Windows 路径/中文文件的普通读写测试在本机通过。当前 Debian 测试覆盖 symlink 与 hard link 逃逸；junction 和 Windows 链接策略由跨平台 CI 验证。测试不会更改系统链接权限。

## 离线回归

```powershell
python scripts/selfcheck.py
python -m compileall -q src tests scripts
python -m unittest discover -s tests -v
.\run.ps1 --help
```

`scripts/selfcheck.py` 需先安装项目；它会核对包版本、模块与命令行帮助，并用独立的 Mock 子进程验证 UTF-8 中文交互。测试只使用本地临时工作区、模拟提供商、loopback HTTP fixture 和 stub HTTP 响应，不读取实际环境密钥或访问外部 API。GitHub Actions 的 Linux/Windows、Python 3.11/3.12 四项矩阵已全部通过（[DeepSeek Anthropic provider 提交的运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39225734297)）。符号链接和硬链接逃逸测试在支持的平台默认运行；junction 仅在 Windows 测试。若 Windows 策略拒绝创建符号链接或文件系统拒绝创建硬链接 fixture，对应 Windows 用例会明确跳过并说明原因。
