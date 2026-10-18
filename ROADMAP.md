# 功能矩阵与可恢复任务清单

状态定义：**已实现**表示本地实现存在；**离线可测试**表示有模拟/本地验证；**占位**表示只记录语义或 UI 状态；**待实现**表示尚未交付。

| 能力 | 状态 | 当前说明 |
| --- | --- | --- |
| Windows 中文命令行交互 | 已实现 / 离线可测试 | 简体中文命令、UTF-8 控制台、Ctrl+C 取消交互 |
| 离线模拟模型 | 已实现 / 离线可测试 | 默认模式，不访问网络、不读取密钥 |
| OpenAI 兼容提供商 | 已实现 / 离线可测试 | stdlib HTTP Chat Completions；未用真实 DeepSeek API 验证 |
| Anthropic 兼容提供商 | 待实现 | DeepSeek `/anthropic` 端点已确认，后续增加独立适配和模拟响应测试 |
| 工作区文件读取与 `@` 引用 | 已实现 / 离线可测试 | 每次读取须逐次人工确认；敏感路径默认阻止；UTF-8 文本、路径需在当前工作区内 |
| 文件编辑与差异预览 | 已实现 / 离线可测试 | 写入前显示完整统一差异并逐次人工确认；严格校验工具参数 |
| 写前备份与撤销 | 已实现 / 离线可测试 | 工作区 `.harness/backups/` 留快照；`/撤销` 反向预览、再次确认并检测后续外部改动 |
| PowerShell/cmd 命令执行 | 待实现 | 需要真实 Windows 进程隔离/沙箱后再开放 |
| 计划模式 | 已实现 / 离线可测试 | 工具层拦截模型写入和撤销；没有 shell 工具，未知命令动作不会执行 |
| 终端输出安全 | 已实现 / 离线可测试 | 模型回复与差异预览里的控制/格式字符转义显示，避免终端序列伪造审批提示 |
| 目标模式 | 占位 | 目标作为会话提示上下文，不会自动长时间执行 |
| 会话持久化 | 待实现 | 尚未保存或恢复对话历史 |
| 上下文压缩、缓存输入优化 | 待实现 | 无自动摘要或缓存策略 |
| 子代理 | 待实现 | 没有并发任务或隔离 worktree 调度 |
| PTC 模式 | 占位 / 语义待确认 | 暂按 Programmatic Tool Calling 理解；未实现专门运行时或脚本工具 |
| 多提供商和可配置模型 | 部分实现 | OpenAI 兼容基础适配器；更多协议与配置校验待做 |
| GUI/TUI、快捷键 | 待实现 | 当前为标准终端输入循环，无全屏 TUI |
| 持续集成与 Windows 版本矩阵 | 待实现 | 当前仅执行本机 Python 3.13 Windows 测试 |

## 本轮验证记录（2026-10-18）

- 本机 Windows / Python 3.13.15：`python -m unittest discover -s tests -v` 共 25 项，22 项通过、3 项因 Windows 拒绝创建 link 夹具而跳过；这不是完整验收。
- `python -m compileall -q src tests`、`.\run.ps1 --help` 和输入中文帮助/状态/计划模式/撤销/退出的离线交互烟测均通过。
- 真正 `pip install -e .` 未在此环境验证：本机缺少 `setuptools>=68`，且普通 DNS/HTTPS 不可用；没有真实 API 请求。

## Windows 路径与权限回归

当前已通过的回归覆盖 UNC、驱动器绝对/相对路径、根相对路径、备用数据流、`..` 穿越、大小写变体、与工作区名称仅有前缀相似的外部目录、中文路径、凭据路径拦截、读写审批、写入差异预览、重复写入、撤销顺序、拒绝和文件冲突。硬链接计数阻断已有实现，但夹具创建被拒绝，因此尚未实测。所有解析都通过路径解析和工作区相对关系检查，不使用字符串前缀判断。

本机尝试创建外部目录 junction、目录 symlink 和硬链接夹具均被 Windows 拒绝，因此三项集成测试被标为跳过：PowerShell `New-Item -ItemType Junction` 返回 `NewItemCreateIOError` / 拒绝访问；`os.symlink` 返回 WinError 1314；`os.link` 返回 WinError 5。第一次确认 junction 被拒绝后，为验证新的测试跳过分支又重跑了同一 fixture 一次；这不符合应立即停止的操作要求。已将所有链接创建测试改为显式 opt-in，之后未再尝试；不要在本环境重试或修改系统安全设置。因而本轮尚不能声称实际 junction/symlink 逃逸测试通过；路径解析代码含外部解析目标阻断，但需在本来就获准创建链接的 Windows 环境补测。

## GitHub 同步状态（2026-10-18）

通过当前已授权 GitHub 连接器只读核实：`chenxiVFX/chenxiVFX-Harness` 为 public，默认分支名为 `main`，元数据 `size=0`，仓库对象权限显示 `push=true`；`/branches` 返回空列表，`/git/refs` 返回 `409: Git Repository is empty.`，读取 `/contents` 返回“repository is empty”的 404。这是空仓库响应，不是授权拒绝；本机 Git push 权限尚未实测。当前本机 DNS 诊断：`Resolve-DnsName github.com` 返回拒绝访问；`curl.exe -I https://api.github.com/...` 返回 curl 6 `Could not resolve host`；Python `socket.getaddrinfo` 返回 `gaierror(11001, 'getaddrinfo failed')`；`git ls-remote` 返回 `getaddrinfo() thread failed to start`（exit 128）。普通 HTTPS/DNS 与 Python 调用同样失败，因此问题不是 Git 子进程独有，也没有证据表明 GitHub 拒绝了账号授权。连接器只读调用成功。

本地提交尚未上传。待本机 DNS 恢复后先运行 `git ls-remote origin refs/heads/main`；若仍无任何 refs，再用 `git push -u origin main` 创建首次远端分支，不使用强推。若此间远端出现提交，先 fetch 并审阅共同历史，再决定合并或重放；不覆盖远端内容。远端目前没有可 pull 的提交，后续连接器只读复核以当时状态为准。

## 语义假设

- **目标模式**：本版本中只表示一个会话目标字符串，并将它作为上下文；不暗示模型拥有更高权限或可无人值守持续运行。
- **PTC**：先按 Programmatic Tool Calling 记录为待实现；需要明确其具体含义（例如允许模型写脚本编排工具，还是更广泛的程序化执行）后再实现。无论定义如何，仍要经过本地权限校验。

## 实施顺序

1. **已完成**：离线 CLI、模拟模型、OpenAI 兼容适配器、路径约束、敏感文件过滤、逐次读取/写入审批、差异预览、写前备份/撤销、中文 README、Windows 单元测试。
2. 建立用户确认的安全凭据录入和一次性、小额 API 测试流程；在未明确费用上限与用户完成录入前不调用真实 API。
3. 实际运行入口安装验证；在有授权的 Windows 环境补测 junction/symlink 路径逃逸，然后继续细化备份/恢复和文件审阅。
4. 设计 Windows 命令沙箱；先用隔离 fixtures 测越权，再开放 PowerShell/cmd。
5. 添加 Anthropic 协议适配，覆盖 DeepSeek `/anthropic` 与独立 Anthropic provider 配置。
6. 迭代上下文压缩、缓存优化、目标执行、子代理及 PTC 语义。
7. 建立真实 Windows CI 与多 Python 版本回归；完成后再扩展支持声明。

## 参考项目与复用边界

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：官方 GitHub 页面显示 MIT 许可证，README 描述其插件式架构和 developer preview。本项目仅参考其“组件边界清晰”的架构方向，不复制实现代码。
- [ZCode](https://github.com/zai-org/ZCode)：官方 GitHub 页面显示 Apache-2.0 许可证，仓库说明包含桌面、服务端、Agent CLI 与运行时。本项目只参考其公开能力分层与 CLI 使用方式，不复制代码或资源。
- [OpenCode](https://github.com/anomalyco/opencode)：官方 GitHub 页面显示 MIT 许可证，README 记录 plan/build 权限分层及 general subagent。本项目只参考只读计划模式和逐步加入子代理的路线，不复制代码。
- 本项目在初始授权 GitHub 页面上为空仓库，无现存许可证/规则/实现可继承。新项目许可证暂不擅自决定，待仓库所有者指定。
