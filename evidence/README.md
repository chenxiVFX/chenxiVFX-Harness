# 中文 TUI 实际运行证据

`tui-synthetic-session.cast` 是 Linux PTY 中启动真实 `TerminalUI` 后录下的 4 秒终端会话，使用 asciinema v2 格式。录制依次展示主界面、`/` 命令与简介候选、选中高亮、多行输入、带合成 diff 的默认“否”审批以及按 Enter 拒绝。

回放：

```sh
asciinema play evidence/tui-synthetic-session.cast
```

会话只用合成文本，没有调用模型 API、读取凭据、创建或修改工作区文件。录屏证明 Linux PTY 下的实际界面渲染和键盘操作；不代表 Windows Terminal/ConPTY 或中文输入法的人工验收。Windows runner 的打包 EXE 合成键盘自检见 [Actions 运行记录](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/runs/39240989690)。
