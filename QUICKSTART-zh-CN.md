# Windows 单文件包：首次试用

本指南适用于从 GitHub Actions 下载的 `windows-x64-cvfx-harness-py312`。它是 Windows x64 的试验构建，不是正式 Release。

## 下载并核对

从[Windows standalone workflow](https://github.com/chenxiVFX/chenxiVFX-Harness/actions/workflows/windows-standalone.yml)打开一个成功的 `main` 运行，核对该次运行的提交，再下载上述 artifact 并解压。Actions artifact 会在 14 天后过期。

在解压目录打开 PowerShell，核对包内清单与可执行文件：

```powershell
$expected = ((Get-Content .\SHA256SUMS.txt -Raw).Trim() -split '\s+')[0]
$actual = (Get-FileHash .\cvfx-harness.exe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'cvfx-harness.exe 的 SHA-256 不匹配' }
```

这会检测下载或解压损坏；核对来源时还应确认你下载的是成功的 workflow 运行及其 artifact。程序未签名；不要为了运行它关闭 SmartScreen、Defender 或组织安全策略。

## 先用离线模式打开工作区

把变量改为一个**已存在**的项目目录。路径可含空格：

```powershell
$workspace = 'D:\Projects\My Project'
if (-not (Test-Path -LiteralPath $workspace -PathType Container)) { throw '工作区目录不存在' }
.\cvfx-harness.exe --workspace "$workspace" --provider mock
```

`mock` 默认完全离线，不读取密钥、不连接模型。先输入 `你好` 看中文响应，再用 `/状态` 核对显示的工作区路径，最后输入 `/退出`。Harness 本身不需要安装系统 Python。

## 配置模型并审阅一次修改

在 Mock 会话中运行 `/配置 编辑`，选择 `openai`、模型 `deepseek-flash`，Base URL 输入 `默认`，预算与输出上限可按需保留默认值；快捷键字段可留空，或设置如 `F2=帮助,F4=状态`，随后审阅并确认保存。设置保存在项目工作区 `.harness/config/settings.json`，其中不含密钥；Harness 不会替你的项目修改 `.gitignore`，请在提交前确认 `.harness/` 已被项目忽略。`/快捷键` 可查看映射；自定义 F2–F8 只在下次 TUI 启动时填入本地只读命令草稿，仍需按 Enter。纯文本 CLI 直接输入命令，不捕获 function keys。

提供商、模型和端点的改动在下次启动生效。若要发起真实 DeepSeek 请求，启动前必须已通过你所在环境认可的安全方式向进程提供 `DEEPSEEK_API_KEY`；`/配置 编辑` 不接受或保存密钥。本地 Harness 没有 Windows Credential Manager 集成，普通环境变量会进入进程并对该进程可见。不要把密钥输入聊天、命令行或 `.harness/config`；如果当前环境没有可用的安全密钥注入方式，就继续使用 Mock。启动真实模式时不要带 `--provider mock`，否则命令行覆盖会让本次仍使用 Mock：

```powershell
.\cvfx-harness.exe --workspace "$workspace"
```

若已保存真实 provider，但当前没有安全密钥注入方式，可仍以离线模式启动：

```powershell
.\cvfx-harness.exe --workspace "$workspace" --provider mock
```

如果 `.harness/config/settings.json` 缺失，`/配置 编辑` 会从内置默认值开始；如果配置损坏或字段无效，请用 `--ignore-config --provider mock` 离线启动，再运行 `/配置 编辑`。该选项不会读取损坏文件；只有审阅并确认保存后才会替换它。取消编辑会保留原文件。

输入一个范围清楚的小修改请求，并使用工作区相对路径说明目标。如果模型需要先读取文件，会单独显示读取审批；写入前会显示目标与完整差异。仔细核对后再批准：TUI 默认拒绝，选择“是”并按 Enter；纯文本模式输入 `y`、`yes` 或 `是`。每次写入都单独审批，没有永久授权。

## 为 Python 项目配置并运行测试

项目测试仅支持 `unittest` 和 `pytest`。运行 Harness 本身不需要系统 Python；运行项目测试时才需要用户单独选择一个现存的外部 `python.exe`，不能选 Harness 自己的 `cvfx-harness.exe`。例如：

```text
/测试 添加 smoke
/测试 运行 smoke
```

按提示选择框架、外部 Python、工作目录和测试目标。用 `pytest` 时，必须已在所选解释器中安装 pytest；Harness 不会安装 Python、pytest 或项目依赖。每次运行前先核对显示的解释器、参数和工作目录再批准。测试代码以当前用户权限运行，**不是 OS 沙箱**，可能访问该用户可访问的文件与网络；只运行你信任的项目测试。若需要把测试输出发给模型，还会另行询问。

## 保存并退出

如需保存可安全存档的普通对话，运行 `/保存 首次试用`，再运行 `/退出`。会话文件保存在工作区 `.harness/sessions/`，是未加密的本地 JSON；文件内容和工具结果不会进入普通对话存档，不要把存档当作加密备份。若项目由 Git 管理，提交前再次检查 `.harness/` 的忽略状态。

若计划步骤在请求中断或程序重启后变为“受阻”，先检查当前工作区，再运行 `/继续 <编号>`。冷启动时旧目标、聊天、问卷和工具结果不会恢复；可输入一条仅本次发送且不存档的补充目标/约束，或留空按已保存步骤的有限上下文继续。两种情况都要审阅最终说明并明确确认；工具调用和审批不会重放。

Windows Terminal、PowerShell/ConPTY 与中文输入法组合尚未完成人工兼容验收。若全屏界面在你的终端无法正常输入，可用 `--plain` 启动纯文本界面；这不改变单次审批要求。
