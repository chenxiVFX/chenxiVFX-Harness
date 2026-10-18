# 架构选择（v0.1）

## 选择

采用 Python 3.11+、标准库 HTTP 与 `urllib`，以可安装的纯 Python CLI 为首个交付形态。仓库起始为空，因此这是一项新项目架构选择，不是对既有实现的替换。

选择理由：

- 在 Windows 上可直接使用 Python、PowerShell 和 Windows 路径，便于先验证中文终端与文件名。
- 标准库足以实现会话、JSON/HTTP、路径限制与模拟提供商；运行时无需下载依赖，适合先离线开发。
- Provider 与工具层分开，后续可逐一加入 Anthropic 协议和其他模型，而不让供应商协议控制文件权限。
- 当前以标准输入/输出作为兼容性基线；只有在快捷键、中断恢复等行为稳定后才考虑 TUI。

## 数据流

```text
终端命令与文本
       │
       ▼
CLI（中文命令、目标/计划状态、显式写入审批）
       │
       ▼
Agent（会话消息、限制工具轮数、校验工具名称）
       ├── Provider：Mock 或 OpenAI 兼容 Chat Completions
       └── Workspace 工具：UTF-8 读取 / 经确认后写入
```

模型只能请求两个已注册工具：`read_file` 与 `write_file`。本地工具层独立解析规范路径并检查工作区根目录；模型返回的工具名称不能映射到任意本地函数。每次写入都经过 CLI 的人工确认回调，计划模式直接拒绝写入。

## 安全边界与限制

- 路径边界依赖 Python 的路径解析与写入前检查，可阻止相对路径穿越和解析后指向工作区外的链接。此 v0.1 尚未处理恶意本地进程并发替换链接造成的 TOCTOU 竞争；不应将其描述为操作系统级沙箱。
- 本版本没有 shell 工具。任意 PowerShell/cmd 命令即使从工作区启动也能访问工作区外，因此在真正的 Windows 进程隔离到位之前不开放。
- 网络 Provider 只在 `--provider openai` 时启用；默认 mock 不发网络请求。密钥只从进程环境读取并放在 Authorization header，不保存在仓库或会话日志中。
- 模型回答、文件内容与工具结果都是不可信输入。权限由本地代码执行，system prompt 只作为额外提示，不能代替本地校验。

## 参考与复用

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的官方仓库展示了插件化模块和工具/Provider 的边界，标注 MIT 许可证。本项目参考模块化方向，不复制源码。
- [ZCode](https://github.com/zai-org/ZCode) 的官方仓库标注 Apache-2.0，覆盖桌面端、服务端和 Agent CLI/运行时。本项目只参考能力分层与中文 CLI 文档做法，不复制源码或资源。
- [OpenCode](https://github.com/anomalyco/opencode) 的官方仓库标注 MIT，README 展示 plan/build agent 的权限差异与 subagent 能力。本项目参考“计划只读、执行审批”的分级思路，不复制源码。
- 本项目 GitHub 仓库当前为空，暂时没有许可证或项目规则；新项目许可证尚未由仓库所有者指定，因此本轮没有擅自加 LICENSE。
