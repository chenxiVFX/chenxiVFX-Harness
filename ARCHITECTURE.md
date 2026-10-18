# 架构说明（v0.3）

## 运行边界

项目使用 Python 3.11+ 标准库，提供 Windows PowerShell 启动脚本。默认 Mock provider；真实 HTTP 只会在用户显式选择 OpenAI 兼容 provider 并配置密钥时调用。没有 shell 执行工具、自动文件写权限或常驻后台服务。

```text
终端 UTF-8 输入
      │
      ▼
CLI：中文命令、文件引用、权限确认、压缩预览和会话切换
      │
      ├── Agent：不可信的消息历史、完整轮次、使用统计
      │      ├── Provider：Mock / OpenAI 兼容 Chat Completions
      │      └── Workspace：路径校验、读写审批、差异、撤销备份
      ├── SessionStore：.harness/sessions 下的受限 JSON 存档
      ├── CompressionPreview：本地确定性摘录、摘要预览和确认后替换
      └── DelegationPlan：最多 3 项的展示计划，不启动子代理
```

## 对话与稳定前缀

Agent 的 system message 在每个请求、恢复和压缩后保持固定文本；工具 schema 来自固定列表。对话以完整轮次分组，助手发起的工具调用和所有工具结果留在同一组。预算估算统计完整 message/schema payload，但使用本地字符启发式，不能替代 provider tokenizer。

系统提示、工具输出、文件内容、历史摘要和模型回复之间的权限信任边界由本地执行代码维护。摘要只能是标注为 `untrusted-history-summary` 的 `user` 消息，不能合并进 system prompt。缓存友好前缀是结构设计目标，不承诺缓存命中率。

## 会话格式

- 存档位置：工作区 `.harness/sessions/<name>.json`；该目录对模型工具不可读写，并由 `.gitignore` 排除。
- 格式版本 1，限制为 4 MiB；名称受长度和字符白名单限制；写入采用工作区的原子文件写入辅助方法。
- 可存储完整普通 user/assistant 对话轮次、已标记的本地摘要、非敏感目标和非敏感 token 计数。
- 不存 system prompt、密钥/端点、工具调用参数、工具结果、文件引用内容或中断的轮次。用户普通对话内容仍应自行检查。
- 加载先解析、限制大小、校验字段/角色/敏感内容，再由 CLI 请求替换确认；验证失败或用户取消都不更改活跃上下文。

## 压缩规则

`/压缩` 使用确定性本地截取旧的普通、安全完整轮次，并显示摘要全文、保留轮数、丢弃类别和估算 tokens 变化。没有可能降低估算上下文时不提供无效预览。用户同意后，整个旧工具轮次可被丢弃；工具调用/返回不会被单独搬动或写入摘要。最近轮次原样保留。摘要仍是显式不可信用户上下文，并在保存时保留标记。

## 权限边界

- 路径工具先做白名单参数检查、规范路径边界检查和敏感区域过滤，再请求用户确认；模型参数中的 `approval` 字段不具授权效力。
- 读操作每次单独批准；写操作先预览差异，再单独批准并备份；撤销也预览并再次确认。
- `.harness/backups` 与 `.harness/sessions` 是 Harness 私有状态，不能通过模型工具访问。
- 目录 reparse point、硬链接身份等检查依赖 Windows 文件系统信息。本机集成测试无法创建链接，因此链接逃逸防护尚未在 junction/symlink/hard link 上端到端实测。
- 普通 Python 进程不能提供 OS 级沙箱。若未来加入 shell，须先设计并实测工作区隔离，不能把路径检查误称为进程沙箱。

## 语义假设

- “目标模式”暂指把用户目标作为上下文展示，不自动拆计划，不扩大模型权限。
- “PTC”暂指 Programmatic Tool Calling。当前没有 PTC 运行时；脚本、批量工具调用或命令执行需要用户澄清并另行设计权限模型。
- “子代理”目前是有界任务列表预案：最多 3 项、最大子代理深度 0、每项最多 4 轮，执行关闭且 Shell 关闭。

详细实现矩阵与续作步骤见 [ROADMAP.md](ROADMAP.md)。
