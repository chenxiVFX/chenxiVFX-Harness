# chenxiVFX Harness（最小原型）

Windows 优先、中文交互的 AI 编程命令行工具。当前版本包含离线模拟模型、工作区持久化运行配置、DeepSeek OpenAI 兼容 Chat Completions 适配、受限多轮工具调用、工作区文件读取与目录列表、经逐次批准的文件写入、差异预览和撤销、文件引用、计划模式、目标上下文、本地会话存档、可审核的上下文压缩、Token 估算和受限只读 Mock 子代理。

这不是 Claude Code 或 Codex CLI 的完整复制品。功能状态和后续清单见[功能矩阵与可恢复任务清单](ROADMAP.md)，架构边界见[架构说明](ARCHITECTURE.md)。

## 环境与安装

需要 Python 3.11 或更新版本。项目没有第三方运行时依赖。Windows PowerShell 中，在仓库目录运行：

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

安装后可运行 `cvfx-harness --workspace . --provider mock` 或 `python -m cvfx_harness`。默认提供商为 `mock`，不会读取密钥或发起网络请求。终端使用 UTF-8；Ctrl+C 会取消当前交互或请求。

## 可用命令

在交互提示符输入 `/帮助` 查看完整说明。

| 命令 | 用途 |
| --- | --- |
| `/状态`、`/配置` | 查看运行配置；`/配置` 还可设置提供商、模型、端点和持久预算；密钥值不显示 |
| `/模式 计划`、`/模式 执行` | 计划模式会阻止工具写入；执行模式仍逐次询问 |
| `/目标 <内容>`、`/目标 清空` | 设置或清除仅作为上下文的会话目标 |
| `/保存 [名称]`、`/恢复 <名称>`、`/会话` | 保存安全对话、确认后恢复、列出工作区会话 |
| `/检查点 <名称>` | 手写进度摘要，经脱敏预览和确认后保存；不自动收集工具结果 |
| `/压缩 [保留轮数]` | 先预览本地摘要和将丢弃的旧轮次，确认后再应用 |
| `/统计`、`/预算 [tokens]` | 查看当前估算上下文、累计用量和提供商报告；`/预算` 临时设置本轮预算 |
| `/子代理 任务甲 \| 任务乙` | 每项单独批准后执行最多 3 个隔离的一次性 Mock 子任务 |
| `/撤销` | 预览并确认恢复最近一次 Harness 文件写入 |
| `/退出` | 结束会话 |

在提问里用 `@相对路径` 引用文件；路径含空格时写 `@{资料/需求 文档.txt}`。每次引用读取都要确认；内容随后会发送给所选提供商。模型也可请求逐次确认的目录列表，列表只包含名称与类型，不读取条目正文。敏感路径、工作区外路径、链接逃逸和非 UTF-8 文本默认拒绝。当前没有 shell 执行工具，PowerShell/cmd 命令不会执行。

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

当前只支持离线 `mock` 和 DeepSeek 的 OpenAI 兼容模式；Anthropic 和其他服务尚未实现。Base URL 必须是 HTTPS `api.deepseek.com:443`，不得包含 userinfo、查询参数、片段或看起来像凭据的文本；只允许配置服务地址，不要把密钥放入 URL。`DEEPSEEK_API_KEY` 只从环境读取，绝不写进配置文件；使用 Network secret 时目标主机限制仍为 `api.deepseek.com:443`。

同样的非敏感设置也可在启动时用 `--provider`、`--model`、`--base-url`、`--token-budget` 和 `--max-output-tokens` 覆盖。超长或格式错误的输入不会被错误消息回显。配置文件损坏时可这样绕过并继续使用 Mock：

```powershell
.\.venv\Scripts\cvfx-harness.exe --workspace . --ignore-config --provider mock
```

## 会话、压缩和 Token 统计

存档放在工作区 `.harness/sessions/`，为 UTF-8 JSON。`/保存` 只保存完整且未触发工具的普通用户/助手对话、受标记的本地历史摘要、非敏感目标文本和计数。工具调用及结果、文件引用轮次、不完整轮次、疑似凭据文本不会存档；为避免助手复述工具结果，引用或工具调用后的回复也会从默认保存中排除。恢复前会先完整校验；损坏存档不会替换当前上下文。存档是本地明文文件，未加密，请勿共享含敏感内容的工作区。

如需保留这类会话的进度，可运行 `/检查点 名称`，然后手写一段摘要。Harness 不会从当前消息、文件引用或工具结果生成该摘要；已知密钥/Token 形态会先脱敏，程序展示脱敏后的确切文本并再次要求确认。检查点只保存这段摘要和 token 计数，不保存目标、原始工具输出或其他上下文。检查点恢复后仍是带不可信标记的用户消息。

`/压缩` 会展示旧对话摘要和估算变化，用户确认后才修改上下文。最近轮次作为完整消息组保留，工具调用和对应结果不会被拆开；较旧工具轮次可以在预览确认后丢弃，但不会被概括。摘要始终作为带不可信标记的 `user` 消息，不能提升为系统指令。系统提示和工具 schema 的顺序保持固定，便于缓存友好的稳定前缀；程序不声称或保证任何缓存命中率。

Token 数由本地字符启发式估算，不是提供商 tokenizer 的精确数值。`--token-budget`（默认 16384）和 `/预算` 只是上下文提示；`--max-output-tokens`（默认 2048）为单次提问的所有模型回复共享输出预算，默认最多 4 次模型请求；每次请求按剩余预算和剩余请求数分配 `max_tokens`。如果 provider 没有返回 usage，会保守扣除该请求的完整分配额度，避免字符估算低估累计输出。每次请求的完整 `max_tokens` 额度都会预留，保证各请求上限之和不超过总预算；usage 用于统计和检查单次上限。超出单次请求上限的响应不会继续触发工具。字符估算不是 provider tokenizer，不能保证远端计量精确。提供商若返回 usage，统计会同时显示这些报告值；当前不计算金额或推测动态价格。启动参数示例：`.\run.ps1 --token-budget 32000 --max-output-tokens 2048`。

委派计划最多列 3 项，每项在工具审批层分别展示并确认；子代理只收到对应任务文字，不继承主会话或文件/工具结果。每个子代理有独立消息、输入/输出预算、超时和取消事件，只运行一次无工具调用的 Mock provider；深度为 0，Shell 关闭。失败、超时、取消和预算耗尽分别汇总，结果带不可信标记。OpenAI provider 当前不支持子代理执行。目标模式目前也只是把用户目标作为上下文，不授予模型额外权限。

## DeepSeek OpenAI 兼容模式

适配器默认端点为 `https://api.deepseek.com/chat/completions`，模型默认为 `deepseek-flash`；工作区配置、环境变量和 CLI 参数可设置非敏感字段。选择 OpenAI 模式后，程序只读取 `DEEPSEEK_API_KEY`；Mock 模式不会查询这个变量。环境密钥只允许发往 `https://api.deepseek.com:443`。`/配置` 会显示变量名，不显示值；认证头不会进入模型消息、会话或 Harness 日志。请勿把密钥放进 URL、命令行、项目文件、聊天或 Git。

本地 CLI 尚无隐藏输入界面；若直接使用普通环境变量，原值会进入 Harness 进程。若以后要在 Codex Cloud 使用 DeepSeek，用户可自行在该环境的 **Network secrets** 中添加变量 `DEEPSEEK_API_KEY`，Allowed domains 设为 `api.deepseek.com`。[Codex Cloud 官方说明](https://learn.chatgpt.com/docs/environments/cloud-environments)指出，Network secret 会以 placeholder 交给程序，并由代理在允许目标的 HTTPS 443 请求中替换原值，不把原始值放入本地进程或文件；普通 Environment variable 的值对 task 可见。保存环境改动后需要 Republish 并新建 task。网络密钥须使用自己的 Key，不能与同名直接环境变量冲突。

离线回归使用合成数据和本机 HTTP fixture。另一次无工具 DeepSeek smoke 返回 HTTP 200 和“连接成功”（usage 输入 36、输出 43、总计 79）。本轮在全新临时目录完成了有限真实工具闭环：向 `api.deepseek.com:443` 的 `deepseek-flash` 发出 2 次请求，单次 `max_tokens` 为 256、合计上限 512；模型仅获 `list_directory` schema，并获准列出 `synthetic/hello.txt`，两次 HTTP 均为 200，usage 输入 1022、输出 82、合计 1104。只传了合成提示和列表结果，没有发送仓库或用户文件；这只验证了该受限路径，不代表完整产品或其他工具已通过。HTTP 429 不自动重试；适配器只统计 provider 返回的 Token usage，不核算费用。取消信号会在发送前和收到响应后检查；标准库阻塞中的 HTTP 请求只能由配置的超时结束，不能保证即时中断。

## 权限与边界

- 文件工具只接受工作区内相对路径；拒绝绝对路径、UNC、ADS、父目录遍历、敏感路径、reparse point 和可识别的硬链接逃逸。只读目录工具逐次审批、限制扫描量和返回条目数、不跟随目录链接。
- 文件读取、引用、目录列表、写入和撤销均由本地执行代码逐次检查；每次都要人工确认。写入前显示差异，确认后写入 Harness 撤销备份。
- 模型输出、文件内容、工具结果、目标文本和历史摘要都是不可信数据，不能修改权限规则。未知工具一律拒绝。
- 单次提问最多 4 轮模型回复、每轮最多执行 4 个工具调用、每个模型响应最多接收 16 个调用；输出预算默认 2048 个 provider completion tokens。按剩余请求槽分配每次 `max_tokens`；缺少 usage 时完整扣除该请求额度。重复调用编号、格式错误参数或超过单次额度的响应会在任何工具执行前终止当前轮次。
- 当前没有 shell、通用命令执行、隔离 worktree、PTC 执行、可调用文件工具的子代理或 Anthropic provider；子代理只支持隔离的一次性 Mock 请求。
- Windows 路径/中文文件的普通读写测试在本机通过。当前 Debian 测试覆盖 symlink 与 hard link 逃逸；junction 和 Windows 链接策略由跨平台 CI 验证。测试不会更改系统链接权限。

## 离线回归

```powershell
python scripts/selfcheck.py
python -m compileall -q src tests scripts
python -m unittest discover -s tests -v
.\run.ps1 --help
```

`scripts/selfcheck.py` 需先安装项目；它会核对包版本、模块与命令行帮助，并用独立的 Mock 子进程验证 UTF-8 中文交互。测试只使用本地临时工作区、模拟提供商、loopback HTTP fixture 和 stub HTTP 响应，不读取实际环境密钥或访问外部 API。GitHub Actions 的 Linux/Windows、Python 3.11/3.12 四项矩阵已全部通过（[运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39203303235)）。符号链接和硬链接逃逸测试在支持的平台默认运行；junction 仅在 Windows 测试。若 Windows 策略拒绝创建符号链接或文件系统拒绝创建硬链接 fixture，对应 Windows 用例会明确跳过并说明原因。
