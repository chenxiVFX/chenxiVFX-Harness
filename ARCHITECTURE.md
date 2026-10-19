# 架构说明（v0.5）

## 运行边界

项目使用 Python 3.11+ 标准库，提供 Windows PowerShell 启动脚本。默认 Mock provider；Mock 不查询 DeepSeek 密钥变量。工作区非敏感设置位于 `.harness/config/settings.json`，按默认值、工作区配置、环境变量、CLI 参数的顺序覆盖。远端 HTTP 只会在用户显式选择 DeepSeek OpenAI 兼容 provider 并提供凭据时调用；本机 loopback HTTP 可用无凭据 fixture 验证协议。没有 shell 工具、任意模型代码执行、自动文件写权限或常驻后台服务。唯一的代码运行入口是用户明确配置并逐次批准的固定 Python 测试 profile；它以当前用户权限运行，不是沙箱。

```text
终端 UTF-8 输入
      │
      ▼
CLI：中文命令、文件引用、权限确认、压缩预览和会话切换
      │
      ├── Agent：不可信的消息历史、完整轮次、使用统计
      │      ├── propose_plan：无工具 JSON 候选、预算与协议校验
      │      ├── Provider：Mock / OpenAI 兼容 Chat Completions
      │      └── Workspace：路径校验、读写审批、差异、撤销备份
      ├── SessionStore：.harness/sessions 下的安全对话与手写检查点 JSON
      ├── CompressionPreview：本地确定性摘录、摘要预览和确认后替换
      ├── PlanStore：有界结构化步骤；接受后保存，恢复不自动执行
      ├── SubagentManager：逐项授权、最多 2 并发的只读子代理
      └── TestProfileStore / ProcessRunner：用户维护的 ID-only Python profile；运行前审批与独立输出分享审批
```

## 对话与稳定前缀

Agent 的 system message 在每个请求、恢复和压缩后保持固定文本；文件工具 schema 顺序固定，有已保存测试 profile 时才在尾部增加 ID 枚举 runner，profile 改动会改变该尾部 schema。每次提问最多进行 4 轮模型回复，每轮最多执行 4 项调用，单个响应最多接受 16 项调用。目录列表、文件读取和文件写入都要逐项审批；目录列表只返回有限数量的名称/类型，跳过敏感条目并拒绝链接目录。文件写入继续经过差异预览和备份。

计划生成使用独立 `Agent.propose_plan` 请求，不携带对话历史、引用文件或工具 schema；本地输出预算限制为一个模型请求。模型必须返回严格 JSON，解析器校验唯一键、字段、大小、步骤数、字符、疑似凭据和工具枚举。候选保留在进程内，只有用户接受才由 `PlanStore` 原子保存；拒绝不写存档，修订仅修改内存候选。存档 v2 保留步骤说明、完成条件、建议工具和状态，v1 用户手写存档按默认完成条件兼容读取。步骤执行时只将当前步骤交给新 Agent，最多 2 次请求/512 输出 tokens；建议工具不限制或预授权运行能力，实际工具仍走原有逐次审批，且没有 shell、任意代码或删除工具。

每次提问的输出预算默认为 2048 tokens，跨模型请求累计；默认最多 4 次请求。每次请求的 `max_tokens` 按剩余预算和剩余请求数分配，并从总预算中预留完整额度，使发送的单次上限之和不超过总预算。Provider usage 用于统计和检查单次上限；缺少 usage 时，字符估算只用于发现明显超限响应，不承担精确计量。超预算、取消、provider 错误或格式错误会清除当前未完成对话轮次，避免把断开的工具交换带到下一次请求。已经由用户批准并完成的文件操作不会因后续请求取消或失败自动回滚；CLI 会提示用户用 `/撤销` 检查写入。该估算不是 tokenizer，因此不能保证与远端计量完全一致。

系统提示、工具输出、文件内容、历史摘要、测试输出和模型回复之间的权限信任边界由本地执行代码维护。测试工具只接受用户配置的一个 profile ID；普通对话与已确认计划步骤在本地逐次批准后才启动，一次提问最多执行一次。交互式计划模式仍不提供该工具。摘要只能是标注为 `untrusted-history-summary` 的 `user` 消息，不能合并进 system prompt。缓存友好前缀是结构设计目标，不承诺缓存命中率。

## SSE 流式传输

OpenAI 兼容 CLI 通过 `complete_streaming` 使用 `text/event-stream`。SSE 解析器增量解码严格 UTF-8，并识别 LF、CRLF、多行 `data` 和注释行；每条事件经完成空行分发，必须收到合法 finish reason 与 `[DONE]`。响应最多 4 MiB、助手文本/推理合计 2 MiB、单行或事件 512 KiB、16,384 个事件，截止时间配置最多 120 秒。响应体由调用线程使用 urllib 的 HTTP 解码器读取；每个响应另有一个 watchdog，以最多 250 ms 的间隔检查取消，并在取消或截止时间到达时 shutdown 该响应 socket。读取、解析和文本回调不切换线程；所有退出路径停止并 join watchdog，不使用无界队列或后台请求重试。

socket 的读取超时设为剩余截止时间，不以短超时轮询后恢复读取。[Python 文档](https://docs.python.org/3.12/library/socket.html#socket.socket.makefile)明确说明 socket 文件缓冲在超时后可能失效；真正的读取超时必须终止该响应。建连、代理 CONNECT、TLS 和响应头阶段仍由 urllib 的网络超时约束，取消需等这些调用返回才能检查；DNS 或多阶段、慢滴响应头不能承诺与响应体相同的主动硬截止边界。

普通内容片段可立即交给 CLI；reasoning 内容不显示。流式函数工具调用按索引累积，结束后复用已有完整消息/参数校验，之后 Agent 才执行原有逐次人工审批。用户测试 profile 工具沿用相同流程，且再执行独立的测试与输出分享审批。未收到完整终止标记、格式错误或超限时，Provider 抛出安全的流错误，Agent 清除未完成轮次且不返回工具调用。流式文字可能已显示一部分，错误后不会撤回；终端输出先脱敏疑似凭据并转义控制字符。当前拒绝多候选项、多模态、logprobs、未知 delta 字段和不支持流式的 provider。

## 会话格式

- 存档位置：工作区 `.harness/sessions/<name>.json`；该目录对模型工具不可读写，并由 `.gitignore` 排除。
- 格式版本 1，限制为 4 MiB；名称受长度和字符白名单限制；写入采用工作区的原子文件写入辅助方法。
- 可存储完整普通 user/assistant 对话轮次、已标记的本地摘要、非敏感目标和非敏感 token 计数。
- 不存 system prompt、密钥/端点、工具调用参数、工具结果、文件引用内容或中断的轮次。用户普通对话内容仍应自行检查。
- 加载先解析、限制大小、校验字段/角色/敏感内容，再由 CLI 请求替换确认；验证失败或用户取消都不更改活跃上下文。
- 引用或工具调用后，默认保存会排除后续回复，避免助手复述敏感结果。用户可用 `/检查点` 手写简短进度摘要；程序只对这段输入做凭据形态脱敏、展示预览并再次请求确认，不读取历史或工具结果，也不保存目标或其他消息。

## 压缩规则

`/压缩` 使用确定性本地截取旧的普通、安全完整轮次，并显示摘要全文、保留轮数、丢弃类别和估算 tokens 变化。没有可能降低估算上下文时不提供无效预览。用户同意后，整个旧工具轮次可被丢弃；工具调用/返回不会被单独搬动或写入摘要。最近轮次原样保留。摘要仍是显式不可信用户上下文，并在保存时保留标记。

## 权限边界

- 路径工具先做白名单参数检查、规范路径边界检查和敏感区域过滤，再请求用户确认；模型参数中的 `approval` 字段不具授权效力。
- `list_directory` 仅在批准后扫描一个工作区目录；不跟随 symlink/junction，只返回普通文件和目录，最多检查 1000 项并返回 100 项，排除敏感路径与 Harness 内部状态。扫描过程会逐项响应取消信号。操作不读取文件正文。
- 每个工具请求由本地 Agent 再验证调用编号、参数对象、调用数量和大小；重复编号或无效结构会停止整次未完成轮次，不执行该批次工具。
- `read_file` 返回原始字节 SHA-256；`patch_file` 必须提供该版本哈希和精确 old/new 文本，旧文本只允许唯一匹配。空旧文本、大小超限、控制/格式字符、过大的 diff、敏感/内部路径和版本不符均在审批前拒绝。审批显示目标、预期哈希和实际 diff，审批后再次按原始字节校验；提交沿用同一备份、原子写入与撤销路径。
- 模型循环上限为 4 轮、每轮执行上限为 4 项；超出的工具调用仍回填“未执行”结果，末轮工具调用全部不执行。每次提问共享 2048 默认输出预算，CLI 可用 `--max-output-tokens` 调整（1–65536）。
- 读操作每次单独批准；模型整文件写入先区分新建/覆盖，局部补丁先校验精确文本和源文件 SHA-256；两种写入都预览差异、逐次审批，核对原始字节与文件状态并创建可恢复备份后以同目录替换。普通 POSIX 权限位保留，特殊位在审批中显示后清除；可访问的 POSIX ACL xattr 和 Windows DACL 会复制，Windows 同时保留受保护/允许继承状态，复制失败时取消。撤销也使用目标版本检查和权限复制路径。输入拒绝、EOF、取消或无效内容均不写入；已有文件不能被空内容清空。
- 提交和撤销在临时文件就绪后执行最后一次目标字节、文件状态与 Windows DACL/继承状态核对；检测到并发冲突时停止并将冲突快照留在私有备份中。撤销快照保存原始字节，包括非 UTF-8 外部修改；审批前发现外部修改时先保存快照再拒绝撤销。最后核对到 rename/replace 之间仍有通用文件系统竞态；没有可移植的 CAS，advisory lock 也不能约束忽略它的编辑器，因此不承诺绝对防覆盖。Windows 额外受路径式检查窗口限制。
- Windows DACL 复制校验比较 DACL 存在/空状态、保护位与按原顺序保留的全部 ACE 字节（包含各 ACE 的继承标志）。允许 Windows 在复制普通文件时转换有效 ACL 格式、重算 AUTO_INHERIT/DEFAULTED 标记和缓冲区容量；不因此忽略任何权限条目或继承保护变化。审批和替换前的并发检查仍比较原始 DACL 快照，既有备份的 DACL 摘要算法保持兼容。Windows 临时夹具覆盖未经预处理的默认 DACL、受保护/允许继承 DACL、仅 DACL 变化的审批失效与最后写入检查，以及撤销前后仅 DACL 变化的拒绝与快照。
- 新备份额外保存权限语义摘要，用于在 Harness 自身写入/撤销导致 Windows 重算 ACL 元数据后核对同一权限版本，避免连续撤销被误判为外部权限更改。原始 DACL 摘要仍保留，单次审批与最终替换仍逐次检查原始快照。旧备份缺少语义摘要时继续保守使用原始摘要；无法核验的新 Windows 备份在撤销时拒绝执行。
- 写入只支持工作区内≤64 KiB 的 UTF-8 文本，拒绝 NUL/控制字符、二进制、链接路径和敏感路径；没有模型删除工具。多个模型写调用各自询问，不批量批准。撤销仍预览并再次确认，计划模式的工具层继续拒绝写入。
- `.harness/backups` 与 `.harness/sessions` 是 Harness 私有状态，不能通过模型工具访问。
- 用户测试 profile 放在 `.harness/config/test_profiles.json`，模型文件工具不能读取或修改。profile 只保存用户独立选择并校验的外部 Python 绝对路径、ID、`unittest`/`pytest` 框架和工作区相对 cwd/target；命令固定为 `<python> -m unittest discover -s <target>` 或 `<python> -m pytest -q <target>`，`shell=False`，不接受模型 argv、脚本、解释器路径、环境变量或工作目录。冻结 EXE 的 `sys.executable` 是 Harness 本身，因此测试执行明确使用外部解释器；没有配置解释器的旧 profile 保持禁用。
- 每次测试都显示确切解释器、argv、cwd、120 秒总限时和 32 KiB 合并 stdout/stderr 上限，并要求用户逐次批准。输出在本地脱敏、转义控制字符后展示；分享最多 16 KiB 输出另需用户批准，拒绝时只回传状态和退出码。一次用户提问最多启动一次测试，没有持久授权或自动重试。
- 测试代码以当前用户 OS 权限运行，不是沙箱；网络访问没有被屏蔽，测试代码可访问用户权限范围内的其他文件和资源。子进程不会继承凭据或其他运行时环境变量；Windows 仅传递系统路径和临时目录。Linux 用 `start_new_session=True` 和进程组终止普通子进程，恶意/自行 `setsid` 脱离的后代不保证受控；Windows 使用 `CREATE_SUSPENDED`，先将进程放入不允许 breakaway 的 kill-on-close Job Object，再恢复主线程，启动失败时在用户代码运行前终止。Windows 合成测试会从被运行的 fixture 中验证 Job Object 成员关系。
- `.harness/config/settings.json` 只存 provider/model/Base URL/预算等非敏感设置，拒绝重复键、未识别字段和疑似凭据，不包含 API Key；仅支持 Mock 与 DeepSeek OpenAI 兼容模式。`--ignore-config` 只跳过此文件以便修复损坏配置。
- 目录 symlink 与硬链接逃逸通过实际链接 fixture 集成测试覆盖；junction 仅适用于 Windows。当前 Linux/Windows 与 Python 3.11/3.12 跨平台 CI 矩阵全部通过；若 Windows 系统拒绝创建测试 fixture，对应 Windows 用例会明确跳过。
- 普通 Python 进程不能提供 OS 级沙箱。若未来加入 shell，须先设计并实测工作区隔离，不能把路径检查误称为进程沙箱。
- 子代理通过 CLI 明确创建；每项任务启动前都展示任务文本和只读路径并单独审批。Mock 与 OpenAI provider 都使用同一 `complete_subagent` 受限入口；OpenAI 子代理只发送独立的子任务消息和该任务授权范围内返回的读取结果，不复制父上下文。Provider 认证仍只走既有 HTTPS Authorization header。
- 默认不提供文件工具。提交时可预授权最多 4 个现存工作区相对路径：文件对应 `read_file`，目录对应 `list_directory`；调用前再次通过 `Workspace` 校验，路径必须与授权路径完全匹配。工具 schema 过滤掉 `write_file` 和 `patch_file`，首轮最多 2 个只读调用，第二轮只用于最终答复；没有 shell、递归委派或任意代码入口。子代理只执行最多 2 轮模型请求，深度为 0。
- 每会话最多 3 项，最多 2 项并发；单项任务最多 240 字符，单请求估算输入最多 512 tokens，全会话最多 3072 估算输入 tokens，每项输出最多 256 tokens，全会话保留输出上限最多 768 tokens。子代理总运行时限默认 60 秒，可设为 5–120 秒，从该任务首个 provider 请求开始；逐项审批和执行前排队不计入。OpenAI 每次 SSE 请求默认受 45 秒总时限限制，可配置为 5–120 秒；时限从建连开始，响应读取期间每 250 ms 检查取消。任务总体 deadline 跨模型/工具轮次共享，每任务最多 2 轮。超时、取消、失败、拒绝和预算耗尽为不同终态。
- 结果先做疑似凭据脱敏，再 JSON 编码并转义可能伪造的封装标记。用户用 `收取` 后，结果以 `kind=subagent`、`persistable=False` 的 user 消息附加到父 Agent；不自动解析结果内的 `@` 文件引用。父会话的普通引用流程仍会对每个读取请求独立审批，子代理审批不等同父代理文件审批。
- 非流式 OpenAI 请求在请求前和响应后检查取消；CLI 的两种 SSE provider 共用响应体 watchdog，建连/响应头等待仍由 provider timeout 约束。429 和读取失败都不自动重试；记录 provider 返回的 Token 用量，不估算货币成本，也不把价格写入代码。
- OpenAI 兼容 CLI 只在该 provider 启动时读取 `DEEPSEEK_API_KEY`；环境密钥限定 HTTPS `api.deepseek.com:443`，不能通过 `HARNESS_BASE_URL` 转发到其他主机。`/配置` 不显示值，密钥只作为 Bearer 认证头，不加入对话、存档或 Harness 日志。Mock 不查询密钥变量。
- Codex Cloud Network secret 可把 `DEEPSEEK_API_KEY` placeholder 交给程序，再由 HTTPS 代理向允许的 DeepSeek 目标替换真实值；官方文档没有确认本适配器 Bearer 头的端到端兼容性，本项目尚未使用真实密钥验证。普通环境变量会直接对 task 代码可见，云端应由用户自行选择 Network secret 并限定 Allowed domains。
- 本地回环 HTTP fixture 验证请求/响应、密钥只在认证头、错误不披露请求认证值；这些结果不代表真实 DeepSeek API 或云端代理已验收。真实 API smoke 仍需要用户完成环境配置并单独确认测试范围。

## 语义假设

- 用户目标仍只作为上下文；只有用户显式要求时，计划生成才产生待审阅候选。候选或已保存计划不授予工具权限，计划执行仍需针对每项工具单独审批。
- “PTC”暂指 Programmatic Tool Calling。当前没有 PTC 运行时；脚本、批量工具调用或命令执行需要用户澄清并另行设计权限模型。
- “子代理”限制为会话最多 3 项、深度 0、最多 2 轮，只能使用 Mock 或 OpenAI 兼容 provider 和预授权精确路径中的只读文件工具；无 shell、无写入、无递归。token 估算不等同 provider tokenizer；本机 fixture 覆盖接口路径，不代表线上模型行为或完整产品验收。

详细实现矩阵与续作步骤见 [ROADMAP.md](ROADMAP.md)。
