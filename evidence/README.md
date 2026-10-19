# 中文 TUI 走查证据

## 本轮覆盖的交互

这份录屏展示当前实现中的配置编辑预览与取消、长中文对话、工作区搜索确认、精确补丁的拒绝与批准、用户测试配置和运行审批、会话保存与恢复、计划候选拒绝、压缩预览与默认拒绝，以及 46×14 终端尺寸下的输入状态。压缩画面展示估算前后值、将概括/保留的轮次、目标和完整约束、摘要摘录与不可信上下文说明；输入框保留在底部，用户可先滚动阅读再明确确认。

## 离线验证

`tui-synthetic-session.cast` 是 Linux POSIX PTY 中运行真实 `TerminalUI` 的 asciinema v2 录屏。可用 `python scripts/record_tui_qa.py` 重现。脚本创建临时合成工作区，并替换为确定性的本地 provider fixture；子进程环境只保留运行所需变量。它没有调用模型 API、读取凭据或访问用户项目。补丁、配置、会话、测试 profile 和唯一 unittest 都只存在于临时目录。

自动化测试另外覆盖 Tab/方向键候选填入、SGR 鼠标报告点击候选并只填入不提交、鼠标审批选择只切换状态不执行、快速重复 Enter、窄终端状态栏及审批默认拒绝。合成凭据输入会被隐藏并拒绝；输入 history 与 clipboard 使用不存内容的实现，合成控制序列只显示为可见文本。TUI 不设置操作系统终端标题，也不接入系统剪贴板。这些是 prompt_toolkit 合成输入事件，不是物理鼠标或 Windows 终端验收。

关键帧由官方 asciinema `agg` v1.9.0 回放录屏并用 `ffmpeg` 导出；窄终端帧裁切到 46×14 PTY 视口。

- [主界面](tui-frame-main.png)
- [补丁审阅与拒绝/批准](tui-frame-patch.png)
- [压缩预览与默认拒绝](tui-frame-compression.png)
- [窄终端布局](tui-frame-narrow.png)

回放录屏：

```sh
asciinema play evidence/tui-synthetic-session.cast
```

## 验证边界

这些证据说明当前 TUI 在合成离线流程中的行为。它们不验证真实 provider/API、网络 usage 或缓存计数，也不是 Windows Terminal、PowerShell/ConPTY、IME、剪贴板或屏幕阅读器的人工验收。CI 的 Windows/Linux 矩阵与 Windows standalone 检查是独立证据，不等于 Windows 交互终端实测。
