# 功能矩阵与可恢复任务清单

状态定义：**已实现**表示功能存在；**离线可测**表示通过本地模拟/单元测试；**占位**表示只有受限提案或显示；**待实现**表示尚未交付。真实 API、链接逃逸和 shell 隔离不可由模拟结果代替。

| 能力 | 状态 | 当前边界 |
| --- | --- | --- |
| Windows 中文 CLI、帮助与 UTF-8 | 已实现 / 离线可测 | PowerShell 启动脚本、中文命令、UTF-8 I/O；Windows Python 3.13 本机运行 |
| 工作区模型配置与启动优先级 | 已实现 / 离线可测 | `.harness/config/settings.json` 仅存非敏感设置；默认 < 工作区配置 < 环境变量 < CLI；DeepSeek 凭据仍只从环境读取 |
| Mock provider | 已实现 / 离线可测 | 默认离线，不发请求、不读密钥 |
| OpenAI 兼容 Chat Completions | 已实现 / 离线可测 | stdlib HTTP、`reasoning_content` 工具往返、usage、`max_tokens` 上限、429 不重试、脱敏错误；CLI 默认 SSE 流式输出，具备大小/时限/事件边界和取消检查 |
| 有界 SSE 中文流式交互 | 已实现 / 离线可测 | UTF-8 跨块、CRLF/LF 与多行 data；最多 4 MiB 响应、2 MiB 助手文本、512 KiB 行/事件、16,384 个事件、120 秒；固定安全分类/阶段/HTTP 状态/字节数/耗时；loopback CONNECT/TLS 代理路径有合成 fixture；工具调用累积并完整校验后沿用 Agent 审批循环；未完成流不执行工具 |
| DeepSeek Anthropic 兼容 Messages API | 已实现 / 离线可测 | 独立 `deepseek-anthropic` provider，固定 `https://api.deepseek.com/anthropic/v1/messages`、仅 DeepSeek 模型与 `DEEPSEEK_API_KEY`；有界 Anthropic SSE、签名 thinking 保留、工具往返沿用 Agent 审批；远端事件与 usage 未在线验证，不接官方 Claude |
| DeepSeek API key 读取 | 已实现 / 离线可测 | 两种 DeepSeek provider 在所选模式下读取 `DEEPSEEK_API_KEY`，只允许 HTTPS `api.deepseek.com:443`；Anthropic 兼容模式另限 `/anthropic` 并拒绝重定向。Mock 不查询密钥，配置页不显示；既有 OpenAI 通路曾有限在线验证，Anthropic 兼容通路只做离线 fixture |
| 受限真实 DeepSeek smoke | 已完成（有限验收） | 合成提示的无工具连接测试成功；只证明认证与基本对话通路，不证明多轮工具闭环。真实调用用量单独跟踪 |
| 受限真实目录工具闭环 | 已完成（有限验收） | 只读目录工具在合成工作区的受限路径测试成功；仅验证该授权路径，不代表完整产品通过。真实调用用量单独跟踪 |
| 多轮工具循环 | 已实现 / 离线可测 | 最多 4 轮、每轮最多执行 4 项，回填工具结果；重复调用编号、格式错误或超预算时关闭当前轮次 |
| 工作区文件引用、目录列表与读写 | 已实现 / 离线可测 | 模型写入复用现有预览/审批/备份/撤销；新建/覆盖分别标识，每项独立确认；UTF-8 文本≤64 KiB，拒绝清空/删除、控制字符二进制、敏感/链接/越界路径；批准后再次核对并原子替换 |
| junction / symlink / hard link 逃逸 | 部分实现 / 跨平台 CI 通过 | Linux 默认运行 symlink / hard link 集成用例；Windows 运行 junction 与可创建的 symlink / hard link 用例；Windows 系统策略拒绝 fixture 时会明确跳过 |
| 计划模式 | 已实现 / 离线可测 | 拒绝工具写入；没有 shell 工具 |
| 用户计划步骤 | 已实现 / 离线可测 | 用户自写最多 3 步，五种可见状态；单次单步、最多 2 次请求/512 输出 tokens；崩溃恢复要求重新确认；不存模型输出/工具结果 |
| 目标模式 | 已实现（有限） | 目标作为不可信上下文提供给当前步骤；模型不能拆步、扩大任务或改变权限 |
| 安全会话保存/恢复 | 已实现 / 离线可测 | `.harness/sessions/`；默认存安全完整轮次；`/检查点` 另存用户手写脱敏摘要，预览确认后才保存；不复制工具结果 |
| 可审核上下文压缩 | 已实现 / 离线可测 | 本地有损摘要，确认后应用；纳入当前目标与可识别的完整用户限制，摘要上限 32 KiB；工具组校验配对后整组保留/丢弃；拒绝不完整轮次；不可信 `user` 消息，疑似凭据脱敏，历史审批不延续为当前授权 |
| Token / 预算统计 | 已实现 / 离线可测 | 上下文预算只提示；`/统计` 分别显示固定前缀和会话消息的独立启发式估算；provider usage 可选报告缓存输入命中数；usage 缺失时仍保证各请求上限之和不超过预算 |
| Cache-friendly 稳定前缀 | 部分实现 / 离线可测 | 静态 system prompt 与有序工具 schema 保持固定；本地分项不是缓存命中量；只显示 provider 明确报告的缓存 tokens；没有缓存命中率承诺或远端命中验证 |
| 子代理 | 已实现 / 离线可测（Mock 与两个 DeepSeek 协议适配器） | 每会话最多 3 项、并发 2、最多 2 轮；独立审批与取消；只读工具限精确预授权路径；无写入/shell/递归；Anthropic 兼容远端行为未在线验证 |
| PTC（Programmatic Tool Calling） | 待实现 / 术语假设 | 暂按“模型组合调用工具/程序化工具调用”记录；未实现脚本或工具执行链 |
| PowerShell/cmd 命令执行 | 待实现 | 当前严格关闭；需要先设计工作区隔离和 Windows 真实进程权限边界 |
| 全屏中文 TUI | 已实现 / 离线可测 | 固定底部多行输入、实时固定命令/枚举建议、键盘与鼠标导航、逐次审批默认拒绝、非 TTY / `--plain` 纯文本回退；不保存输入历史；终端与中文 IME 兼容仍需人工验收 |
| 通用模型配置文件、Provider 插件与 PTC 配置 | 待实现 | 现有 provider 设置由非敏感工作区/环境/CLI 字段解析；没有插件框架或 PTC 配置，密钥不入仓 |

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
- DeepSeek OpenAI 与 Anthropic 兼容 provider 只在选择对应 DeepSeek 模式时读取 `DEEPSEEK_API_KEY`，并限定 HTTPS `api.deepseek.com:443`；Anthropic 兼容路径还固定 `/anthropic` 且禁用重定向。Mock 不查询密钥，兼容 provider 不读取 `ANTHROPIC_API_KEY`；认证值不出现在 `/配置`、消息体或 provider 的默认表示中。
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
- 真实 DeepSeek smoke 在独立新环境已由用户确认完成：无工具的合成短请求成功返回固定中文答复；本轮没有再次调用真实 API，也没有接触凭据。工具调用往返仍只有离线 fixture 覆盖。

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

1. 按当前离线开发约束，继续用 Mock 和 loopback 合成 fixture 覆盖 SSE 边界、取消与错误路径；不发起真实模型 API 请求。
2. 子代理真实服务行为没有线上验收；当前只用 Mock 和 loopback fixture 覆盖 OpenAI SSE 子代理路径。
3. 增加完整的会话恢复/检查点覆盖策略与 schema migration；保留检查点只来自用户手写摘要的边界。
4. 先设计 Windows 沙箱/进程隔离模型，再决定是否实现 PowerShell/cmd。Shell 执行未授权之前保持关闭。
5. 与用户确认 PTC 的确切语义，再选择静态工具组合、脚本受限执行或其他模型；当前语义假设不阻塞基础 CLI。

## 会话压缩与稳定前缀统计（2026-10-19）

- 压缩摘要保留当前目标（疑似凭据形态脱敏），并对包含明确约束标记的旧用户轮次保留完整原文；其他用户/助手内容仍是短摘录，摘要有损且最多 32 KiB UTF-8，超限时拒绝应用。
- 工具 turn 必须通过 tool-call ID 与 tool-result 完整配对校验，压缩只整轮保留或丢弃；遇到任何未完成轮次或畸形工具组时拒绝预览。压缩摘要说明旧审批/同意不构成当前授权，工具结果和子代理结果沿用既有隔离规则。
- 固定 system prompt 与三项工具 schema 的名称/顺序保持稳定。`/统计` 显示总上下文、固定前缀及会话消息的独立字符启发式估算；分项不相加。只有 provider usage 带有有效 `prompt_cache_hit_tokens` 时才累计缓存输入报告，不从本地字数估算推断命中。
- 离线合成回归覆盖长中文、晚出现约束、敏感目标脱敏、摘要标记注入、字节上限、过期/未完成工具协议和 provider usage 字段解析；没有真实模型请求或凭据读取。测试不等同于完整产品验收。

## 中文 CLI 多行输入与交互发现（2026-10-19）

- 增加 `/命令` 命令发现和 `/多行` 输入；逐行合并为单条请求，单独一行 `/提交` 发送、`/取消` 或 Ctrl+C 丢弃，最大 64 KiB UTF-8。多行中的 `/退出`、`/撤销` 等文本按普通提示处理，不派发 CLI 动作。
- TTY 且 Python `readline` 可用时，Tab 补全命令名和固定枚举值（当前 `/模式 计划|执行`）；不补路径、自由文本或凭据，也不接受快捷执行。自动历史关闭，并在每次输入/审批后清空进程内 readline history；不写 history 文件。无 readline 的 Windows TTY 用 `/命令` 发现命令。
- Ctrl+C 取消当前输入/多行草稿/模型请求并回到提示符；EOF 结束会话，多行未提交时丢弃，审批输入 EOF 默认拒绝。非 TTY 可逐行脚本化，不会等待额外终端交互。
- 文件引用继续支持 `@{含空格的相对路径}`，多行内容中同样适用；读取和写入仍逐项审批。
- 回归覆盖真实 CLI subprocess（脚本化 Unicode、多行、带空格文件路径、取消、EOF）和 POSIX PTY（Tab、Ctrl+C、无输入历史）；Windows 的 PTY 功能不适用，跨平台 subprocess 用例由 CI 执行。测试不等同于完整产品验收。
- 本地 Debian / Python 3.12：136 项，135 通过、1 项 Windows-only junction 跳过；editable install 自检、`compileall`、UTF-8 Mock CLI 与 `git diff --check` 通过。此次真实 API 请求为 0，未读取凭据；真实调用用量单独跟踪。

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
- 在该轮配置实现提交时，仅支持 Mock 和 DeepSeek OpenAI 兼容模式；Anthropic 及其他服务当时未实现。DeepSeek 网络密钥的 HTTPS `api.deepseek.com:443` 目标限制保持强制。本文件后续章节记录新增的 DeepSeek Anthropic 兼容入口；该入口仍不代表官方 Claude provider。
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
- 当前工作树未调用真实模型 API、未读取环境凭据；SSE HTTP 用例只访问本机 loopback，且不发送 Authorization 头。代码提交 `cf6d4401e26c0ba988b7551a69c52656f2ce0030` 的四矩阵 GitHub Actions 均以 `completed / success` 结束（[运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39206980429)）。
- 本轮离线工作没有真实 API 请求，未读取凭据或 Network secret；真实调用用量单独跟踪。

## 用户自编计划步骤（2026-10-19）

- 新增 `/计划` 工作流：用户可编写最多 3 个步骤（每步最多 400 字符）；状态显式显示为 `pending`、`in-progress`、`completed`、`blocked`、`cancelled`。`/目标` 只作为不可信上下文提供，不让模型拆分目标或扩展授权。
- 每次运行只执行一条用户步骤，先确认精确文本，再启动全新 Agent；最多 2 次 provider 请求、512 输出 tokens。只读文件/列目录沿用原有逐次审批，写入在工具层拦截。成功后须用户审阅并手动完成。
- `.harness/plans/current.json` 只存用户步骤和状态；每步 400 字符，存档总计 4 KiB；校验 schema/大小/凭据形态/控制字符，并对确认后文件内容变化做二次校验。模型输出、工具结果、会话消息和审批不存档。重启时 `in-progress` 回到 `pending`，继续运行必须重新确认。
- 本地离线回归：101 项，100 通过、1 项 Linux 平台跳过 junction 用例；`compileall`、安装自检、UTF-8 Mock 交互和 `git diff --check` 通过。只用合成数据、Mock/provider fixture；没有真实模型 API 请求、没有读取或打印凭据。测试结果不等同于完整产品验收。

## 模型提出的文件写入审批（2026-10-19）

- 普通模式中的模型 `write_file` 继续复用现有 `preview_write`、`approve_action`、`commit_write`、撤销备份与 `/撤销`，没有第二套权限路径。差异完整显示；新文件和覆盖已有文件分别标识；每个调用单独询问。
- 预检查拒绝空内容清空已有文件、NUL/控制字符及无效 UTF-8、64 KiB 以上内容、缺失父目录、reparse/link 路径、硬链接、敏感路径、越界路径和未声明的 `approval` 等额外参数。计划模式照旧在工具层拦截写入。
- 写审批后、备份创建前后都核对目标路径和原字节；备份后版本有变化则清理新备份并拒绝覆盖。目标写入沿用同目录临时文件、fsync 和原子替换。
- 本地离线回归：107 项，106 通过、1 项 Linux 平台跳过 Windows junction 用例；覆盖 Mock 多轮写调用、loopback SSE 合成写闭环、分别确认新建/覆盖、拒绝/EOF/Ctrl+C/无效输入、路径/文本限制及备份期间取消或并发变动。只使用合成工作区、patched provider 和本机 HTTP fixture；未发出真实模型请求、未读取或打印凭据。测试不等同于完整产品验收。

## 会话内受限子代理（2026-10-19）

- `/子代理 提交` 在 CLI 中创建最多 3 个用户子任务；每项显示确切任务与预授权读取范围，并分别询问同意。`列表` 查看状态/预算，`取消` 支持排队或运行任务，`收取` 一次聚合完成与失败结果并以不可信 user 上下文加入父对话。
- Mock 与 OpenAI 兼容 provider 通过同一个有界子代理入口。OpenAI 路径复用有界 SSE；只发送独立子代理上下文。无预授权路径时没有工具；用户可以逐任务授权最多 4 个现存工作区相对路径，文件只启用 `read_file`、目录只启用 `list_directory`。即使模型请求工具，执行前仍校验工具、操作和规范化路径；超出范围、写入、shell、递归委派均拒绝。
- 会话上限：3 项任务、最多 2 并发；每项最多 2 轮，首轮最多 2 个只读调用，第二轮只用于最终答复；估算每请求输入最多 512 tokens、跨任务输入总上限 3072、每项输出最多 256 tokens、预留总输出上限 768。运行时限默认 60 秒，可设为 5–120 秒，从每项首个模型请求开始计时，审批与执行前排队不计；单次请求默认总时限 45 秒、硬限 120 秒，SSE 取消轮询 250 ms。每个子任务的预算、取消事件、状态和结果隔离；失败/超时/取消不会标为成功。父会话未继承文件引用、目标、工具结果或审批；收取内容标为不可存档不可信数据。
- 测试使用故障注入 Mock、恶意工具/结果、线程取消及本机 `127.0.0.1` HTTP SSE fixture；没有连接真实模型 API，没有读取或配置凭据。OpenAI fixture 覆盖唯一允许的子代理只读 schema 和第二轮中的精确文件数据回填。子代理真实服务行为和 tokenizer 精确计量仍未线上验证；本地测试不等同完整产品验收。
- Linux / Python 3.12 本地离线结果：116 项，115 通过、1 项 Windows-only junction 跳过；`compileall`、安装自检、UTF-8 CLI Mock、自测和 `git diff --check` 通过。推送后的 Windows/Linux × Python 3.11/3.12 CI 状态待记录。

## 可配置子代理时限与受限实测（2026-10-19）

- 旧 5 秒是 provider 校验与 manager 默认共同构成的硬上限，不是单纯测试设置。现改为默认 60 秒、用户可设 5–120 秒；每任务总 deadline 从首个 provider 请求开始，逐项审批和执行前队列不计入。单个 SSE 请求默认 45 秒总时限，可设 5–120 秒；`--max-model-requests` 限定每次提问最多 1–4 次，无自动重试。CLI 和审批预览显示实际时限与计时边界；已提交任务不随新设置改变。
- 子代理共享输入预算实际按本地 token 估算在每次发送前预留，跨任务上限 3072；失败调用仍保留输入预留。输出上限在获批时每项预留 256、总计 768；provider 有 usage 时用其 completion 数校验，usage 缺失时按完整输出额度保守保留。它不是精确的 provider tokenizer 计费。
- 受限服务诊断曾返回“SSE 网络读取失败”，没有获得 usage；按未完成响应即停止规则，没有重试或继续发出文件/子代理请求。因此中文流式答复、合成文件写回与字节读回、真实 provider 子代理均未通过该项验收，故障原因未确认。临时工作区已清理；真实调用用量单独跟踪。未读取或输出密钥值。
- Linux / Python 3.12 本地离线回归：121 项，120 通过、1 项 Windows-only junction 跳过，另有 70 个子测试通过；包括时限、审批不计时、失败输出额度预留、取消与 SSE 超时 fixture。安装自检、UTF-8 CLI Mock、自测与 `git diff --check` 通过。结果不等同完整产品验收。

## SSE 网络诊断与 CONNECT/TLS 离线回归（2026-10-19）

- 对照普通 JSON `complete()` 后确认：SSE 读取额外依赖 urllib 响应的 `fp.raw._sock`，并以短 socket 超时和 `read1()` 轮询取消；普通路径只用 `read()`。本轮保留系统代理 CONNECT 与标准 TLS 校验，不使用 `select`、不绕过证书、不改网络设置。
- 增加固定安全诊断字段：分类、阶段、HTTP 状态、收到的响应字节数和耗时。区分建连超时、读取超时、解析错误、缺少结束标记、协议拒绝、TLS 错误、对端断连、连接重置和 HTTP 状态；POSIX `ETIMEDOUT` / `EAGAIN` 与 Winsock `10060` / `10035` 统一映射，最终 250 ms 读取轮询窗口里的未知平台读错误也归类为读取超时；不包含异常原文、头、URL、响应体或用户内容。成功响应走上下文关闭，HTTP 错误显式关闭；取消与错误路径增加关闭验证。
- 本机合成夹具包含 HTTP/SSE 分块、慢读、截断、CONNECT 隧道和 HTTPS SSE origin。测试专用证书只由 fixture trust store 信任，且主机名校验及 `CERT_REQUIRED` 保持启用；无 API 凭据或真实服务请求。
- fixture 找到并修复一处可复现的分类 bug：解析分块后进入下一次读取时，阶段标签未从 `sse_parse` 复位，因而总时限耗尽会被误报为解析错误。此前那次真实 SSE 失败没有阶段诊断，本轮未重放，所以它的根因仍未知。
- 工具兼容范围：流完整校验后继续走现有 Agent 工具审批循环；未完成/错误 SSE 不会把部分工具调用交给 Agent。Mock、无流式接口 provider、多候选、多模态、logprobs 和未知增量仍明确拒绝。单元测试和本机代理 fixture 不等同完整产品验收。
- Debian / Python 3.12 本地离线回归：125 项，124 通过、1 项 Windows-only junction 跳过；editable install、安装自检、`compileall` 与 `git diff --check` 通过。Windows/Linux × Python 3.11/3.12 四格 CI 均通过（[运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39217266384)）。本机与 CI 单元测试不等同完整产品验收。
- 本轮及后续离线开发没有真实 API 请求；真实调用用量单独跟踪。

## DeepSeek Anthropic 兼容 Messages API（2026-10-19）

- 新增独立 `deepseek-anthropic` provider，固定请求 `https://api.deepseek.com/anthropic/v1/messages`，模型只接受 `deepseek-flash` 和 `deepseek-v4-pro`。只在该模式读取 `DEEPSEEK_API_KEY` 并使用 `x-api-key`；不查询 `ANTHROPIC_API_KEY`，不支持官方 Claude 服务、账号或模型别名。HTTPS 保持标准证书校验，重定向不跟随，错误不显示响应体、URL、请求头或原始异常。
- Messages 请求映射 system、user/assistant 文本和本地函数 schema；工具调用/结果继续走既有 Agent 的预算、路径校验与逐项审批。子代理同样受只读工具和预授权路径限制；任何协议未完成、工具 JSON 不完整或 stop_reason 不匹配的响应都不会执行工具。
- 有界 SSE 使用既有 UTF-8/SSE 解析器，接受 Anthropic `message_start`、content block、`message_delta` 与 `message_stop` 序列；支持 text、`tool_use` 和带签名 `thinking`。请求默认关闭 thinking；若端点仍返回 thinking，不显示且在内存工具上下文保留签名。多模态、redacted/document/search/MCP 内容与未知事件明确拒绝。终端控制字符和疑似凭据沿用既有显示脱敏。
- DeepSeek 兼容指南未列出 SSE 事件细节或响应 usage 字段；实现按 Anthropic Messages 协议解释事件序列，并仅在返回标准 `input_tokens` / `output_tokens` 时记 usage。该两项均为协议推断，尚未用真实 DeepSeek Anthropic endpoint 验证；缺失/无效 usage 时按请求完整 `max_tokens` 保守预留，不报告缓存命中。远端兼容性仍是未实现验收项。
- 回归只使用 loopback HTTP fixture 与合成消息，覆盖跨字节中文、固定 URL 路径和无凭据请求、签名 thinking/工具往返、现有文件审批、缺 usage 预留、重定向拒绝、不完整工具流不审批、未知内容/事件、响应大小限制、总时限和取消。未访问真实模型或读取/设置/输出凭据；测试不等于完整产品验收。
- Linux / Python 3.12 本地离线回归：150 项，149 通过、1 项 Windows-only junction 跳过；loopback fixture 不发送 API 密钥或 Authorization。`compileall`、editable install、安装自检与 `git diff --check` 均通过。Windows/Linux × Python 3.11/3.12 四矩阵 CI 全部成功（[运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39225734297)）；本轮使用合成数据且没有真实模型 usage，真实调用用量单独跟踪。CI 和本地回归均不等同完整产品验收。

## SSE 正常停顿与缓冲读取修复（2026-10-19）

- 确认旧读取器的短 socket 超时会使 urllib 底层 SocketIO 文件缓冲失效；正常生成停顿超过轮询间隔后，继续读取便误报网络传输失败。合成 HTTP 与验证证书的 CONNECT/TLS fixture 均可复现，普通 JSON 读取没有相同的短超时重读步骤。历史远端失败缺少阶段记录，因此不能回溯证明其唯一根因。
- 两种 SSE provider 共用一个响应体读取 guard：调用线程负责读取、协议解析与回调，单个 watchdog 仅在取消或截止时间到达时 shutdown 当前响应 socket，并在所有退出路径停止和 join。正常停顿继续等待，真正超时直接结束；不恢复已超时的文件缓冲、不自动重试、不新增模型工具或执行权限。
- 新增正常停顿恢复、分块头拆分、持续慢滴、取消、KeyboardInterrupt、超时不重读及 watchdog 回收验证，覆盖 OpenAI/Anthropic、HTTP/验证证书的 CONNECT/TLS 两种传输。保留大小限制、完成标记、工具审批和错误脱敏；建连/响应头的网络栈取消与硬截止限制已在架构文档明示。
- Debian / Python 3.12 离线回归：157 项，156 通过、1 项 Windows-only junction 跳过；测试不等同完整产品验收。用量账本不写入仓库，真实工具闭环、子代理及 Anthropic endpoint 的验收范围独立判断。

## 全屏中文 TUI 首发闸门（2026-10-19）

- TTY 默认使用固定版本 `prompt-toolkit 3.0.53` 与 `wcwidth 0.9.2`；非 TTY、`TERM=dumb` 或 `--plain` 保留纯文本入口。Windows 单文件构建锁定轮子哈希、扫描依赖清单并携带第三方许可证通知。
- 实现顶部模型/工作区标识、滚动会话区、固定底部输入和状态栏；`/` 实时列出固定中文命令及简介，`/模式` 仅补固定枚举。Tab 或 Enter 可填入建议；Enter 再提交完整输入，Ctrl+J 换行，Esc 先关建议再取消，Ctrl+C 先清草稿再请求取消。
- 审批面板显示动作预览，默认拒绝；键盘/鼠标只切换“是/否”，必须另按 Enter 才会给当前操作单次授权。多行粘贴拒绝；模型/工具输出会转义终端控制字符、遮蔽疑似凭据并限制缓冲输出长度。
- TUI 把输入桥接到既有 CLI Agent/工具循环，沿用预算、目录/文件逐项审批、计划拦截和取消事件；没有增加 shell 或模型代码执行。PTY 与合成按键用于验证渲染入口和操作流程，不等同 Windows Terminal/ConPTY 或真实中文输入法验收。
- 交互组件测试、隔离合成键盘自检和 POSIX PTY 测试只使用本地合成内容与 Mock；Windows standalone workflow 会在打包 EXE 内运行 TUI 合成键盘自检。中文 IME 和终端缩放/鼠标差异仍待人工兼容测试。
- Linux / Python 3.12 离线回归：177 项，176 通过、1 项 Windows-only junction 跳过；editable install、自检、TUI 合成键盘自检、`compileall` 与 `git diff --check` 通过。Windows/Linux × Python 3.11/3.12 四格 CI 全部通过（[运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39240749409)）；Windows standalone EXE 构建、归档审查、帮助/Mock/TUI 自检和短期 artifact 上传通过（[运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39240749626)）。没有真实模型请求或凭据读取；测试不等同完整产品验收。

## 参考项目复用边界

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：公开 GitHub 页面标示 MIT；这里只借鉴公开的模块边界和流程概念，不复制源码。
- [ZCode](https://github.com/zai-org/ZCode)：公开 GitHub 页面标示 Apache-2.0；这里只参考公开 CLI/Agent 概念，不复制源码。
- [OpenCode](https://github.com/anomalyco/opencode)：公开 GitHub 页面标示 MIT；这里只参考 plan/build 权限分层思路，不复制源码。
- `chenxiVFX/chenxiVFX-Harness` 页面曾显示仓库为空、无许可证文件；当前提交均为新写实现。本清单不代替逐文件许可证审查。
