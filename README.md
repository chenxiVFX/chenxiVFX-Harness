# chenxiVFX Harness（最小原型）

Windows 优先、中文交互的 AI 编程命令行工具。当前版本只交付一个可安装运行的最小闭环：交互式会话、离线模拟提供商、OpenAI 兼容 Chat Completions 适配、工作区内文本读取、逐次确认的文件写入、文件引用、计划模式和会话目标。

这个版本不是 Claude Code 或 Codex CLI 的完整复制品。后续能力按[功能矩阵与任务清单](ROADMAP.md)逐步实现。

## 环境与安装

需要 Python 3.11 或更新版本。项目不依赖第三方运行时库。

```powershell
cd C:\path\to\chenxiVFX-Harness
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

在不能执行 PowerShell 激活脚本的终端里，可以直接使用虚拟环境解释器：

```powershell
.\.venv\Scripts\python.exe -m cvfx_harness --help
```

## 离线运行

默认使用内置模拟提供商，不读取密钥，也不会发起网络请求：

```powershell
cvfx-harness --workspace . --provider mock
```

不安装包时可直接运行仓库中的启动脚本：

```powershell
.\run.ps1 --workspace . --provider mock
```

安装后也可用 `python -m cvfx_harness` 启动。用 `/帮助` 查看命令；按 Ctrl+C 会中断当前交互。

## DeepSeek OpenAI 兼容模式

适配器默认使用 `https://api.deepseek.com` 和 `deepseek-flash`，请求发往 `/chat/completions`。本地默认工作区和模拟模式不需要密钥。未由用户配置 `DEEPSEEK_API_KEY` 时，真实提供商会直接报错，不会尝试网络请求。

安全录入示例（PowerShell）：

```powershell
$secret = Read-Host "输入 DeepSeek API Key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
cvfx-harness --workspace . --provider openai --model deepseek-flash
Remove-Item Env:\DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
$secret.Dispose()
```

密钥不会写入配置或仓库，不要使用 `setx`、`.env`、命令行参数或日志传递密钥。此示例仅设置当前 PowerShell 进程环境，之后立即清除。连接真实端点、录入密钥及产生 API 费用均由用户控制；当前轮次没有执行真实 API 调用。端点可用 `HARNESS_BASE_URL` 覆盖，模型可用 `HARNESS_MODEL` 覆盖。不要把 API Key 放进 Base URL。

## 交互命令

| 命令 | 用途 |
| --- | --- |
| `/帮助` | 显示中文帮助 |
| `/状态` | 显示当前工作区、提供商、模型、模式和目标 |
| `/模式 计划` | 允许读取，但拦截所有模型提出的文件写入 |
| `/模式 执行` | 恢复写入能力；每次写入仍需人工确认 |
| `/目标 <文字>` | 设置本会话目标，作为上下文提示 |
| `/目标 清空` | 清除目标 |
| `/退出` | 结束会话 |

提问时用 `@相对路径` 引用工作区文件；带空格或中文的路径用 `@{相对路径}`，例如 `看看 @{资料/场景 文件.txt}`。引用文件会作为不可信上下文发送给所选模型。只允许读取工作区内、UTF-8 编码且不超过 64 KiB 的文本文件。

## 权限与边界

- 所有文件工具都将相对路径解析到规范化后的工作区目录；绝对路径、目录穿越和解析到工作区外的符号链接会被拒绝。
- 模型不能选择任意 Python 函数；工具名和参数由本地注册表校验。未知工具会被拒绝。
- 每次文件写入前都会显示路径并要求用户明确确认；计划模式会阻止写入。拒绝时文件保持原样。
- 当前版本**不开放任意 shell 命令执行**。仅设置工作目录无法将 PowerShell/cmd 的任意命令限制在工作区内；在加入真正的 Windows 沙箱前，不把“逐条确认”伪装成隔离。
- 目前没有撤销/恢复机制；写入前请确认目标路径。后续会先增加可验证的备份/恢复流程，再考虑更多修改操作。
- 响应正文不会记录到日志。错误处理不会打印 API 请求头、请求体或服务端错误正文；密钥只作为 Authorization 请求头使用。

## 本地模拟测试

```powershell
python -m unittest discover -s tests -v
```

测试只使用临时目录和伪造 HTTP 响应，不访问外网、不读取真实 API 密钥，也不会产生 API 费用。
