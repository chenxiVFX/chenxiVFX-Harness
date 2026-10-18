# chenxiVFX Harness（最小原型）

Windows 优先、中文交互的 AI 编程命令行工具。当前版本提供交互式会话、离线模拟提供商、OpenAI 兼容 Chat Completions 适配、工作区内文本读取、逐次确认的差异预览和文件写入、备份撤销、文件引用、计划模式和会话目标。

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
| `/模式 计划` | 每次读取仍需确认；在工具层拦截模型写入和撤销 |
| `/模式 执行` | 恢复写入能力；每次写入仍需人工确认 |
| `/目标 <文字>` | 设置本会话目标，作为上下文提示 |
| `/目标 清空` | 清除目标 |
| `/撤销` | 显示差异并确认后，恢复最近一次 Harness 写入前的内容 |
| `/退出` | 结束会话 |

提问时用 `@相对路径` 引用工作区文件；带空格或中文的路径用 `@{相对路径}`，例如 `看看 @{资料/场景 文件.txt}`。每次引用或模型工具读取都要单独确认；确认后文件内容才会作为不可信上下文发送给所选模型。只允许读取工作区内、UTF-8 编码且不超过 64 KiB 的文本文件。

## 权限与边界

- 所有文件工具都将相对路径解析到规范化后的工作区目录；绝对路径、UNC、驱动器相对路径、备用数据流、目录穿越和解析到工作区外的链接会被拒绝。边界比较不使用字符串前缀。
- `.env*`、常见凭据目录、密钥/令牌命名文件、`.git` 和 Harness 内部备份默认不可通过工具读取或修改；含多个硬链接的文件也会被拒绝。敏感路径规则是保守启发式，不能替代操作系统沙箱。
- 模型不能选择任意 Python 函数；工具名、参数集合和类型由本地注册表校验。每次模型读取、用户 `@` 引用读取和写入都逐次人工确认。未知工具会被拒绝。
- 每次写入前会显示完整统一差异并等待明确确认；确认后先在工作区 `.harness/backups/` 留下写入前快照，再原子替换目标文件。该目录被 Git 忽略并且模型工具不能访问。拒绝时不写文件、不留备份。
- `/撤销` 会显示反向差异并再次要求确认；只撤销最近一次未撤销的 Harness 写入。如果文件在写入后又被改动，撤销会停止，避免覆盖新内容。撤销新建文件时会删除该文件。Harness 进程意外终止、磁盘损坏或其他程序并发改动时，不承诺事务级恢复。
- 计划模式在工具执行层拦截模型写入和 `/撤销`，不依赖提示词；当前没有 shell/命令工具，未知的命令动作也不会执行。
- 模型回复和差异预览中的控制/格式字符会转义后显示，避免 ANSI 控制序列伪造审批界面。
- 当前版本**不开放任意 shell 命令执行**。仅设置工作目录无法将 PowerShell/cmd 的任意命令限制在工作区内；在加入真正的 Windows 沙箱前，不把“逐条确认”伪装成隔离。
- 响应正文不会记录到日志。错误处理不会打印 API 请求头、请求体或服务端错误正文；密钥只作为 Authorization 请求头使用。

本机 Windows 回归尝试创建目录 junction、symlink 和硬链接来测试链接逃逸；操作系统拒绝了这些测试夹具（junction 创建返回拒绝访问，symlink 返回 WinError 1314，硬链接返回 WinError 5），因此三项链接集成测试在本机标记为跳过。测试需在当前权限本来就允许创建测试链接的 Windows 环境中显式设置 `HARNESS_TEST_CREATE_LINKS=1` 才会运行；不需要、也不应为此改系统安全设置。普通 UNC、绝对/驱动器路径、路径穿越、大小写处理和前缀相似目录仍通过实际文件系统测试；见[功能矩阵与任务清单](ROADMAP.md)。

## 本地模拟测试

```powershell
python -m unittest discover -s tests -v
```

测试只使用临时目录和伪造 HTTP 响应，不访问外网、不读取真实 API 密钥，也不会产生 API 费用。
