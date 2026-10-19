# 中文 TUI 实际运行证据

`tui-synthetic-session.cast` 是在 Linux PTY 中启动真实 `TerminalUI` 录制的 asciinema v2 会话。操作过程展示主界面、命令候选与高亮、多行草稿、写入审批预览和默认拒绝结果。录制只使用合成文字和 Mock；没有调用模型服务、读取凭据或修改测试工作区文件。

关键帧从这份 PTY 录屏回放导出，使用官方 asciinema `agg` v1.9.0 终端渲染器和 `ffmpeg` 转成 PNG。它们保留 PTY 的 ANSI、中文字体与布局输出；不是重画的终端模拟图，也不是 Windows 截图。

- [主界面](tui-frame-main.png)
- [命令候选高亮](tui-frame-candidates.png)
- [多行草稿](tui-frame-multiline.png)
- [写入审批预览](tui-frame-approval.png)
- [拒绝结果](tui-frame-rejected.png)
- [关键帧联系表](tui-keyframes.png)

回放原始终端录屏：

```sh
asciinema play evidence/tui-synthetic-session.cast
```

录屏、POSIX PTY 测试验证 Linux PTY 下的实际 TUI 渲染与键盘流程；不代表 Windows Terminal、PowerShell/ConPTY 或中文输入法的人工兼容验收。Windows runner 的独立包合成键盘自检见 [Actions 运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39240989690)。
