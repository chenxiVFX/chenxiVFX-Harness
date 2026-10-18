# 功能矩阵与可恢复任务清单

状态定义：**已实现**表示本地实现存在；**离线可测试**表示有模拟/本地验证；**占位**表示只记录语义或 UI 状态；**待实现**表示尚未交付。

| 能力 | 状态 | 当前说明 |
| --- | --- | --- |
| Windows 中文命令行交互 | 已实现 / 离线可测试 | 简体中文命令、UTF-8 控制台、Ctrl+C 取消交互 |
| 离线模拟模型 | 已实现 / 离线可测试 | 默认模式，不访问网络、不读取密钥 |
| OpenAI 兼容提供商 | 已实现 / 离线可测试 | stdlib HTTP Chat Completions；未用真实 DeepSeek API 验证 |
| Anthropic 兼容提供商 | 待实现 | DeepSeek `/anthropic` 端点已确认，后续增加独立适配和模拟响应测试 |
| 工作区文件读取与 `@` 引用 | 已实现 / 离线可测试 | 文本 UTF-8，路径需在当前工作区内 |
| 文件编辑 | 已实现 / 离线可测试 | 写入须逐次人工确认；计划模式拦截写入 |
| PowerShell/cmd 命令执行 | 待实现 | 需要真实 Windows 进程隔离/沙箱后再开放 |
| 计划模式 | 已实现 / 离线可测试 | 基础开关；读取可用，写入拦截 |
| 目标模式 | 占位 | 目标作为会话提示上下文，不会自动长时间执行 |
| 撤销、恢复、会话持久化 | 待实现 | 需设计备份、历史与恢复边界 |
| 上下文压缩、缓存输入优化 | 待实现 | 无自动摘要或缓存策略 |
| 子代理 | 待实现 | 没有并发任务或隔离 worktree 调度 |
| PTC 模式 | 占位 / 语义待确认 | 暂按 Programmatic Tool Calling 理解；未实现专门运行时或脚本工具 |
| 多提供商和可配置模型 | 部分实现 | OpenAI 兼容基础适配器；更多协议与配置校验待做 |
| GUI/TUI、快捷键 | 待实现 | 当前为标准终端输入循环，无全屏 TUI |
| 持续集成与 Windows 版本矩阵 | 待实现 | 当前仅执行本机 Python 3.13 Windows 测试 |

## 语义假设

- **目标模式**：本版本中只表示一个会话目标字符串，并将它作为上下文；不暗示模型拥有更高权限或可无人值守持续运行。
- **PTC**：先按 Programmatic Tool Calling 记录为待实现；需要明确其具体含义（例如允许模型写脚本编排工具，还是更广泛的程序化执行）后再实现。无论定义如何，仍要经过本地权限校验。

## 实施顺序

1. **当前轮**：离线 CLI、模拟模型、OpenAI 兼容适配器、路径约束、逐次写入审批、中文 README、Windows 单元测试。
2. 建立用户确认的安全凭据录入和一次性、小额 API 测试流程；在未明确费用上限与用户完成录入前不调用真实 API。
3. 实现编辑差异预览、备份/恢复、会话存档和更细的审批规则。
4. 设计 Windows 命令沙箱；先用隔离 fixtures 测越权，再开放 PowerShell/cmd。
5. 添加 Anthropic 协议适配，覆盖 DeepSeek `/anthropic` 与独立 Anthropic provider 配置。
6. 迭代上下文压缩、缓存优化、目标执行、子代理及 PTC 语义。
7. 建立真实 Windows CI 与多 Python 版本回归；完成后再扩展支持声明。

## 参考项目与复用边界

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：官方 GitHub 页面显示 MIT 许可证，README 描述其插件式架构和 developer preview。本项目仅参考其“组件边界清晰”的架构方向，不复制实现代码。
- [ZCode](https://github.com/zai-org/ZCode)：官方 GitHub 页面显示 Apache-2.0 许可证，仓库说明包含桌面、服务端、Agent CLI 与运行时。本项目只参考其公开能力分层与 CLI 使用方式，不复制代码或资源。
- [OpenCode](https://github.com/anomalyco/opencode)：官方 GitHub 页面显示 MIT 许可证，README 记录 plan/build 权限分层及 general subagent。本项目只参考只读计划模式和逐步加入子代理的路线，不复制代码。
- 本项目在初始授权 GitHub 页面上为空仓库，无现存许可证/规则/实现可继承。新项目许可证暂不擅自决定，待仓库所有者指定。
