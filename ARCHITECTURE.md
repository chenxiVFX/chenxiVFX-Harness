# 架构说明（v0.5）

## 运行边界

项目使用 Python 3.11+ 标准库，提供 Windows PowerShell 启动脚本。默认 Mock provider；Mock 不查询 DeepSeek 密钥变量。工作区非敏感设置位于 `.harness/config/settings.json`，按默认值、工作区配置、环境变量、CLI 参数的顺序覆盖。远端 HTTP 只会在用户显式选择 DeepSeek OpenAI 兼容 provider 并提供凭据时调用；本机 loopback HTTP 可用无凭据 fixture 验证协议。没有 shell 执行工具、自动文件写权限或常驻后台服务。

```text
终端 UTF-8 输入
      │
      ▼
CLI：中文命令、文件引用、权限确认、压缩预览和会话切换
      │
      ├── Agent：不可信的消息历史、完整轮次、使用统计
      │      ├── Provider：Mock / OpenAI 兼容 Chat Completions
      │      └── Workspace：路径校验、读写审批、差异、撤销备份
      ├── SessionStore：.harness/sessions 下的安全对话与手写检查点 JSON
      ├── CompressionPreview：本地确定性摘录、摘要预览和确认后替换
      └── SubagentManager：逐项授权、最多 2 并发的 Mock / OpenAI 只读子代理
```

## 对话与稳定前缀

Agent 的 system message 在每个请求、恢复和压缩后保持固定文本；工具 schema 来自固定列表。每次提问最多进行 4 轮模型回复，每轮最多执行 4 项调用，单个响应最多接受 16 项调用。目录列表、文件读取和文件写入都要逐项审批；目录列表只返回有限数量的名称/类型，跳过敏感条目并拒绝链接目录。文件写入继续经过差异预览和备份。

每次提问的输出预算默认为 2048 tokens，跨模型请求累计；默认最多 4 次请求。每次请求的 `max_tokens` 按剩余预算和剩余请求数分配，并从总预算中预留完整额度，使发送的单次上限之和不超过总预算。Provider usage 用于统计和检查单次上限；缺少 usage 时，字符估算只用于发现明显超限响应，不承担精确计量。超预算、取消、provider 错误或格式错误会清除当前未完成对话轮次，避免把断开的工具交换带到下一次请求。已经由用户批准并完成的文件操作不会因后续请求取消或失败自动回滚；CLI 会提示用户用 `/撤销` 检查写入。该估算不是 tokenizer，因此不能保证与远端计量完全一致。

系统提示、工具输出、文件内容、历史摘要和模型回复之间的权限信任边界由本地执行代码维护。摘要只能是标注为 `untrusted-history-summary` 的 `user` 消息，不能合并进 system prompt。缓存友好前缀是结构设计目标，不承诺缓存命中率。

## SSE 流式传输

OpenAI 兼容 CLI 通过 `complete_streaming` 使用 `text/event-stream`。SSE 解析器增量解码严格 UTF-8，并识别 LF、CRLF、多行 `data` 和注释行；每条事件经完成空行分发，必须收到合法 finish reason 与 `[DONE]`。响应最多 4 MiB、助手文本/推理合计 2 MiB、单行或事件 512 KiB、16,384 个事件、最长 120 秒。响应体读取期间以不超过 250 ms 的 socket read timeout 检查取消；建连/响应头使用 provider timeout。

普通内容片段可立即交给 CLI；reasoning 内容不显示。流式函数工具调用按索引累积，结束后复用已有完整消息/参数校验，之后 Agent 才执行原有逐次人工审批。未收到完整终止标记、格式错误或超限时，Provider 抛出安全的流错误，Agent 清除未完成轮次且不返回工具调用。流式文字可能已显示一部分，错误后不会撤回；终端输出先脱敏疑似凭据并转义控制字符。当前拒绝多候选项、多模态、logprobs、未知 delta 字段和不支持流式的 provider。

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
- 模型循环上限为 4 轮、每轮执行上限为 4 项；超出的工具调用仍回填“未执行”结果，末轮工具调用全部不执行。每次提问共享 2048 默认输出预算，CLI 可用 `--max-output-tokens` 调整（1–65536）。
- 读操作每次单独批准；模型写入先区分新建/覆盖、预览完整差异，再单独批准，重新核对原始字节并创建可恢复备份后以同目录原子替换。输入拒绝、EOF、取消或无效内容均不写入；已有文件不能被空内容清空。
- 写入只支持工作区内≤64 KiB 的 UTF-8 文本，拒绝 NUL/控制字符、二进制、链接路径和敏感路径；没有模型删除工具。多个模型写调用各自询问，不批量批准。撤销仍预览并再次确认，计划模式的工具层继续拒绝写入。
- `.harness/backups` 与 `.harness/sessions` 是 Harness 私有状态，不能通过模型工具访问。
- `.harness/config/settings.json` 只存 provider/model/Base URL/预算等非敏感设置，拒绝重复键、未识别字段和疑似凭据，不包含 API Key；仅支持 Mock 与 DeepSeek OpenAI 兼容模式。`--ignore-config` 只跳过此文件以便修复损坏配置。
- 目录 symlink 与硬链接逃逸通过实际链接 fixture 集成测试覆盖；junction 仅适用于 Windows。当前 Linux/Windows 与 Python 3.11/3.12 跨平台 CI 矩阵全部通过；若 Windows 系统拒绝创建测试 fixture，对应 Windows 用例会明确跳过。
- 普通 Python 进程不能提供 OS 级沙箱。若未来加入 shell，须先设计并实测工作区隔离，不能把路径检查误称为进程沙箱。
- 子代理通过 CLI 明确创建；每项任务启动前都展示任务文本和只读路径并单独审批。Mock 与 OpenAI provider 都使用同一 `complete_subagent` 受限入口；OpenAI 子代理只发送独立的子任务消息和该任务授权范围内返回的读取结果，不复制父上下文。Provider 认证仍只走既有 HTTPS Authorization header。
- 默认不提供文件工具。提交时可预授权最多 4 个现存工作区相对路径：文件对应 `read_file`，目录对应 `list_directory`；调用前再次通过 `Workspace` 校验，路径必须与授权路径完全匹配。工具 schema 过滤掉 `write_file`，首轮最多 2 个只读调用，第二轮只用于最终答复；没有 shell、递归委派或任意代码入口。子代理只执行最多 2 轮模型请求，深度为 0。
- 每会话最多 3 项，最多 2 项并发；单项任务最多 240 字符，单请求估算输入最多 512 tokens，全会话最多 3072 估算输入 tokens，每项输出最多 256 tokens，全会话保留输出上限最多 768 tokens。子代理总运行时限默认 60 秒，可设为 5–120 秒，从该任务首个 provider 请求开始；逐项审批和执行前排队不计入。OpenAI 每次 SSE 请求默认受 45 秒总时限限制，可配置为 5–120 秒；时限从建连开始，响应读取期间每 250 ms 检查取消。任务总体 deadline 跨模型/工具轮次共享，每任务最多 2 轮。超时、取消、失败、拒绝和预算耗尽为不同终态。
- 结果先做疑似凭据脱敏，再 JSON 编码并转义可能伪造的封装标记。用户用 `收取` 后，结果以 `kind=subagent`、`persistable=False` 的 user 消息附加到父 Agent；不自动解析结果内的 `@` 文件引用。父会话的普通引用流程仍会对每个读取请求独立审批，子代理审批不等同父代理文件审批。
- 非流式 OpenAI 请求在请求前和响应后检查取消；CLI 的 SSE 响应体采用短 socket 读取超时并轮询取消，建连/响应头等待仍由 provider timeout 约束。429 不自动重试；记录 provider 返回的 Token 用量，不估算货币成本，也不把价格写入代码。
- OpenAI 兼容 CLI 只在该 provider 启动时读取 `DEEPSEEK_API_KEY`；环境密钥限定 HTTPS `api.deepseek.com:443`，不能通过 `HARNESS_BASE_URL` 转发到其他主机。`/配置` 不显示值，密钥只作为 Bearer 认证头，不加入对话、存档或 Harness 日志。Mock 不查询密钥变量。
- Codex Cloud Network secret 可把 `DEEPSEEK_API_KEY` placeholder 交给程序，再由 HTTPS 代理向允许的 DeepSeek 目标替换真实值；官方文档没有确认本适配器 Bearer 头的端到端兼容性，本项目尚未使用真实密钥验证。普通环境变量会直接对 task 代码可见，云端应由用户自行选择 Network secret 并限定 Allowed domains。
- 本地回环 HTTP fixture 验证请求/响应、密钥只在认证头、错误不披露请求认证值；这些结果不代表真实 DeepSeek API 或云端代理已验收。真实 API smoke 仍需要用户完成环境配置并单独确认测试范围。

## 语义假设

- “目标模式”暂指把用户目标作为上下文展示，不自动拆计划，不扩大模型权限。
- “PTC”暂指 Programmatic Tool Calling。当前没有 PTC 运行时；脚本、批量工具调用或命令执行需要用户澄清并另行设计权限模型。
- “子代理”限制为会话最多 3 项、深度 0、最多 2 轮，只能使用 Mock 或 OpenAI 兼容 provider 和预授权精确路径中的只读文件工具；无 shell、无写入、无递归。token 估算不等同 provider tokenizer；本机 fixture 覆盖接口路径，不代表线上模型行为或完整产品验收。

详细实现矩阵与续作步骤见 [ROADMAP.md](ROADMAP.md)。
