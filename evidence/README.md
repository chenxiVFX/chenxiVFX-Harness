# 中文 TUI 走查证据

## 本轮覆盖的交互

这份录屏展示配置编辑预览与取消、`/状态`、长中文对话、工作区搜索确认、精确补丁的拒绝与批准、用户测试配置和运行审批、会话保存与恢复、结构化问卷取消/修改/显式提交与默认隐私、计划步骤审阅、工具审批、工具结果已返回后模型请求中断及 `/继续` 的工作区复核与显式确认、两文件变更集整批预览/审批、失败后的补偿及外部冲突留存、压缩预览和默认拒绝，以及 46×14 终端下的输入状态。录屏还在合成配置审阅后退出并重新启动 TUI，验证 F2 映射只填入 `/帮助` 草稿、必须再按 Enter 才显示帮助。问卷画面展示已修改答案和默认仅本次；计划画面展示完成条件、中断阶段、仅保存固定结果状态（原文未存档）和显式继续；变更集画面展示逐文件恢复状态。压缩画面展示估算前后值、概括/保留的轮次、目标和完整约束、摘要摘录与不可信上下文说明；输入框保留在底部，用户可先滚动阅读再明确确认。

补丁审批把目标、工具从当前工作区内容取得的 SHA-256 前 16 位指纹和差异分层显示；短指纹在 UI 结构中独立传递，不从模型/文件文本解析。差异文本会先脱敏，新增/删除行在深色主题使用红/绿高对比颜色并保留 `+`/`-` 标记；测试检查 `NO_COLOR` 样式无配色，并校验常见浅色 `COLORFGBG` 模式下的 diff 对比度。顶栏按宽度截断，`/状态` 可查看完整信息。

## 离线验证

`tui-synthetic-session.cast` 是 Linux POSIX PTY 中运行真实 `TerminalUI` 的 asciinema v2 录屏。可用 `python scripts/record_tui_qa.py` 重现。脚本创建临时合成工作区，并替换为确定性的本地 provider fixture；子进程环境只保留运行所需变量。它没有调用模型 API、读取凭据或访问用户项目。补丁、配置、会话、测试 profile 和唯一 unittest 都只存在于临时目录。

自动化测试另外覆盖 Tab/方向键候选填入、问卷切题后的答案框焦点、SGR 鼠标报告点击候选并只填入不提交、鼠标审批选择只切换状态不执行、快速重复 Enter、F2 自定义映射保留草稿并等待显式提交、窄终端状态栏、`NO_COLOR` 无配色及审批默认拒绝。另有一遍完整 PTY 流程设置 `NO_COLOR=1`；录制脚本检查输出没有 ANSI 颜色属性。合成凭据输入会被隐藏并拒绝；输入 history 与 clipboard 使用不存内容的实现，合成控制序列只显示为可见文本。TUI 不设置操作系统终端标题，也不接入系统剪贴板。这些是 prompt_toolkit 合成输入事件，不是物理鼠标或 Windows 终端验收。

关键帧由官方 asciinema `agg` v1.9.0 回放录屏并用 `ffmpeg` 导出；窄终端帧裁切到 46×14 PTY 视口。

本轮录屏另走查 `/项目说明`：默认拒绝后再次预览并明确确认；`/状态` 显示已确认和最近一次 provider 调用请求上下文中的文件路径、字节数、SHA-256 与失效条件。合成 `AGENTS.md` 含有要求绕过审批并运行 PowerShell 的恶意句子；在计划模式里 fixture 故意返回一次 `write_file` 请求，工具层拒绝且没有创建目标文件。随后明确批准一个子代理任务，并检查其 provider 消息不含项目说明标记或正文。录屏中的文件和所有 provider 都是一次性合成数据；没有真实 API、凭据或用户项目访问。

- [主界面](tui-frame-main.png)
- [补丁审阅与拒绝/批准](tui-frame-patch.png)
- [压缩预览与默认拒绝](tui-frame-compression.png)
- [已修改答案的问卷审阅](tui-frame-question.png)
- [计划步骤的显式确认](tui-frame-plan.png)
- [多文件补偿与冲突状态](tui-frame-change-set.png)
- [窄终端布局](tui-frame-narrow.png)

回放录屏：

```sh
asciinema play evidence/tui-synthetic-session.cast
```

完整离线流程也可在无配色模式下重放；这条命令将临时录屏写入 `/tmp`，不覆盖仓库证据：

```sh
CVFX_TUI_QA_NO_COLOR=1 python scripts/record_tui_qa.py
```

## 验证边界

这些证据说明当前 TUI 在合成离线流程中的行为。它们不验证真实 provider/API、网络 usage 或缓存计数，也不是 Windows Terminal、PowerShell/ConPTY、IME、剪贴板或屏幕阅读器的人工验收。CI 的 Windows/Linux 矩阵与 Windows standalone 检查是独立证据，不等于 Windows 交互终端实测。
