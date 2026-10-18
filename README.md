# chenxiVFX Harness（最小原型）

Windows 优先、中文交互的 AI 编程命令行工具。当前版本包含离线模拟模型、OpenAI 兼容 Chat Completions 适配、工作区内文件读取与写入、逐次权限确认、差异预览和撤销、文件引用、计划模式、目标上下文、本地会话存档、可审核的上下文压缩、Token 估算和受限委派计划。

这不是 Claude Code 或 Codex CLI 的完整复制品。功能状态和后续清单见[功能矩阵与可恢复任务清单](ROADMAP.md)，架构边界见[架构说明](ARCHITECTURE.md)。

## 环境与安装

需要 Python 3.11 或更新版本。项目没有第三方运行时依赖。创建虚拟环境并安装：

```powershell
cd C:\path\to\chenxiVFX-Harness
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

也可以直接用当前 Python 启动仓库中的脚本：

```powershell
.\run.ps1 --help
.\run.ps1 --workspace . --provider mock
```

安装后可运行 `cvfx-harness --workspace . --provider mock` 或 `python -m cvfx_harness`。默认提供商为 `mock`，不会读取密钥或发起网络请求。终端使用 UTF-8；Ctrl+C 会取消当前交互或请求。

## 可用命令

在交互提示符输入 `/帮助` 查看完整说明。

| 命令 | 用途 |
| --- | --- |
| `/状态`、`/配置` | 查看模型、工作区、端点、预算提示与安全设置；密钥值不显示 |
| `/模式 计划`、`/模式 执行` | 计划模式会阻止工具写入；执行模式仍逐次询问 |
| `/目标 <内容>`、`/目标 清空` | 设置或清除仅作为上下文的会话目标 |
| `/保存 [名称]`、`/恢复 <名称>`、`/会话` | 保存安全对话、确认后恢复、列出工作区会话 |
| `/压缩 [保留轮数]` | 先预览本地摘要和将丢弃的旧轮次，确认后再应用 |
| `/统计`、`/预算 [tokens]` | 查看当前估算上下文、累计用量和提供商报告；设置预算提示 |
| `/子代理 任务甲 \| 任务乙` | 生成最多 3 项的委派计划，不启动子代理 |
| `/撤销` | 预览并确认恢复最近一次 Harness 文件写入 |
| `/退出` | 结束会话 |

在提问里用 `@相对路径` 引用文件；路径含空格时写 `@{资料/需求 文档.txt}`。每次引用读取都要确认；内容随后会发送给所选提供商。敏感路径、工作区外路径、链接逃逸和非 UTF-8 文本默认拒绝。当前没有 shell 执行工具，PowerShell/cmd 命令不会执行。

## 会话、压缩和 Token 统计

存档放在工作区 `.harness/sessions/`，为 UTF-8 JSON。只保存完整且未触发工具的普通用户/助手对话、受标记的本地历史摘要、非敏感目标文本和计数。工具调用及结果、文件引用轮次、不完整轮次、疑似凭据文本不会存档。恢复前会先完整校验；损坏存档不会替换当前上下文。存档是本地明文文件，未加密，请勿共享含敏感内容的工作区。

`/压缩` 会展示旧对话摘要和估算变化，用户确认后才修改上下文。最近轮次作为完整消息组保留，工具调用和对应结果不会被拆开；较旧工具轮次可以在预览确认后丢弃，但不会被概括。摘要始终作为带不可信标记的 `user` 消息，不能提升为系统指令。系统提示和工具 schema 的顺序保持固定，便于缓存友好的稳定前缀；程序不声称或保证任何缓存命中率。

Token 数由本地字符启发式估算，不是提供商 tokenizer 的精确数值。`--token-budget`（默认 16384）和 `/预算` 只是提示，不限制提供商侧输出。提供商若返回 usage，统计会同时显示这些报告值。启动参数示例：`.\run.ps1 --token-budget 32000`。

委派计划最多列 3 项，标明每项最多 4 轮、子代理深度 0、Shell 关闭；当前只展示计划，不执行并行子代理。目标模式目前也只是把用户目标作为上下文，不授予模型额外权限。

## DeepSeek OpenAI 兼容模式

适配器默认端点为 `https://api.deepseek.com/chat/completions`，模型默认为 `deepseek-flash`。真实请求需要用户自行安全设置 `DEEPSEEK_API_KEY`；可用 `HARNESS_BASE_URL` 和 `HARNESS_MODEL` 覆盖端点与模型。没有密钥时程序会报错退出，不会尝试真实请求。

在当前 PowerShell 进程里录入密钥的示例（密钥值不会写入命令文本或项目文件）：

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

本轮开发和测试不执行 API 请求，也不要求密钥。请求头和响应正文不会写入会话或 Harness 日志；端点配置可在 `/配置` 查看。不要把密钥写入仓库、`.env`、命令行参数或测试输出。

## 权限与边界

- 文件工具只接受工作区内相对路径；拒绝绝对路径、UNC、ADS、遍历、敏感路径、reparse point 和可识别的硬链接逃逸。
- 文件读取、引用、写入和撤销均由本地执行代码逐次检查；每次读写都要人工确认。写入前显示差异，确认后写入 Harness 撤销备份。
- 模型输出、文件内容、工具结果、目标文本和历史摘要都是不可信数据，不能修改权限规则。未知工具一律拒绝。
- 当前没有 shell、通用命令执行、隔离 worktree、PTC 执行、实际子代理执行或 Anthropic provider。
- Windows 路径/中文文件的普通读写测试在本机通过。受当前系统策略限制，junction、symlink、hard link 逃逸集成测试未实际创建链接验证；不因测试而更改系统安全设置。

## 离线回归

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -v
.\run.ps1 --help
```

测试只使用本地临时工作区、模拟提供商和 stub HTTP 响应，不读取真实密钥或访问 API。链接集成用例默认跳过。
