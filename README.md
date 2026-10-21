# Agent Harness

面向后端开发者的本地代码任务工作台，支持独立工作树、项目测试和代码审阅。CLI 与网页共用一个 Runner，支持模型多轮调用、读取/搜索/写入文件、执行命令、保存轨迹和产物，以及可选的宿主独立验收。

当前提供本地单用户、单任务控制台。任务产物可以是代码、文本、CSV 等格式；没有验收器时保持“未验收”。当前阶段、后续路线、目录职责和完整文档导航见 [PROJECT.md](PROJECT.md)。

## 安装与密钥

需要 Python 3.12、uv；使用网页还需要 Node.js 22.12 或更新兼容版本完成前端构建。所有命令在项目根目录执行。

```powershell
uv --cache-dir .uv-cache sync --frozen
uv --cache-dir .uv-cache run harness --help
```

程序从启动终端的 `DEEPSEEK_API_KEY` 环境变量读取密钥，不读取 `.env`。如果已经设置，可直接启动；也可以在 PowerShell 的密码框输入，避免把明文写入命令历史：

```powershell
$credential = Get-Credential -UserName 'deepseek' -Message '在密码框输入 DeepSeek API Key'
$env:DEEPSEEK_API_KEY = $credential.GetNetworkCredential().Password
```

密钥不进入网页或运行记录。真实模型运行会发送任务和读取的内容并消耗服务额度；仓库样例使用虚构数据。

## 启动网页控制台

Windows 下用 `npm.cmd` 避免 PowerShell 脚本执行策略影响。首次安装使用临时 npm 11，可避开本机旧版 npm 的依赖解析问题，不改变全局 npm：

```powershell
npx.cmd --yes npm@11 --prefix frontend ci
npm.cmd --prefix frontend run build
uv --cache-dir .uv-cache run harness serve --web-config web.example.toml --config harness.example.toml
```

打开 [本地任务工作台](http://127.0.0.1:8765/runs)。构建后的页面由同一个 Python 服务提供；后续不修改前端源码时，只需运行最后一条命令。

网页支持提交任务、实时轨迹、取消、历史、产物预览/下载和后续任务配置。“填充日志样例”会选择每次独立复制的输入及专用验收器。刷新或关闭网页不会取消后台任务。工作区登记、开发模式和 HTTP/SSE 契约见 [控制台说明](docs/WEB_CONSOLE.md)。

## 使用自己的 Git 项目

在宿主配置中登记项目和已有测试命令，网页选择代码项目后提交小修改任务。系统在独立 worktree 中执行，展示 diff 与测试证据，支持保留、下载补丁和确认丢弃。基线不包含源目录未提交改动，保留不自动合并。登记示例、依赖环境及完整操作见 [代码任务与审阅](docs/PROJECT_TASKS.md)。

## CLI 常用命令

只创建准备记录，不需要密钥，也不调用模型、工具或验收程序：

```powershell
uv --cache-dir .uv-cache run harness run --prepare-only --task "统计日志中的错误类型" --workspace .
```

读取仓库日志并返回文字统计：

```powershell
uv --cache-dir .uv-cache run harness run --task-file examples/logs/task.md --workspace examples/logs --config harness.example.toml
```

要生成脚本和报告并进行独立验收，先创建新的样例工作区：

```powershell
$demoWorkspace = Join-Path '.runtime' ('logs-' + [Guid]::NewGuid().ToString('N').Substring(0, 10))
New-Item -ItemType Directory -Path $demoWorkspace | Out-Null
Copy-Item -LiteralPath examples/logs/app.log -Destination (Join-Path $demoWorkspace 'app.log')

uv --cache-dir .uv-cache run harness run --task-file examples/logs/task_artifacts.md --workspace $demoWorkspace --config harness.example.toml --expected-output analyze_logs.py --expected-output report.json --verifier examples/verifiers/logs.toml
```

`--expected-output` 可以重复传入，用来登记命令生成的文件；登记存在性不等于内容验收。固定日志验收器只适用于仓库输入，其他任务需配置相应的宿主验收程序。

也可指定自己的工作区：

```powershell
uv --cache-dir .uv-cache run harness run --task "读取 app.log，统计 ERROR 类型和次数，分页读完再回答" --workspace 'D:\data\logs'
```

查看记录时，将 `RUN_ID` 换成运行输出的 UUID：

```powershell
uv --cache-dir .uv-cache run harness show RUN_ID
uv --cache-dir .uv-cache run harness show RUN_ID --json
```

记录默认写入 `.runs/<run_id>/`，查看历史不会重放工具或继续任务。`--runs-dir` 可更换记录目录，读取时使用同一路径。

## 配置与运行结果

根目录的 `harness.example.toml` 配置模型、工具和预算；`web.example.toml` 登记网页可选择的工作区与验收器。内置默认只启用 `read_file`，显式传入示例运行配置才启用全部四工具。CLI 可用 `--model` 和 `--max-model-turns` 覆盖配置。

执行、验收和清理分别记录；模型回答结束不代表产物正确。退出码如下，PowerShell 用 `$LASTEXITCODE` 查看：

| 退出码 | 含义 |
| --- | --- |
| 0 | 准备操作成功；或真实执行完成、独立验收通过且清理可确认 |
| 1 | 执行失败、达到上限、验收未通过/异常或清理未知/不完整 |
| 2 | 启动参数、配置或密钥错误 |
| 3 | 执行完成但未验收 |
| 124 | 运行或单模型轮次超时 |
| 130 | 用户/上游取消或服务关闭 |

`show` 返回 0 仅表示读取成功。当前执行可信本地命令，路径检查不等于容器隔离；命令边界、超时、取消、验收协议与记录结构见 [技术设计](docs/TECH_DESIGN_V0.1.md)。

## 检查

```powershell
uv --cache-dir .uv-cache run pytest -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

普通测试使用脚本化模型、MockTransport 和本地子进程，不依赖真实密钥或联网。平台不支持的符号链接/FIFO 用例会跳过。前端开发与源码阅读见 [前端工程说明](frontend/README.md)，已有验证记录集中在 [控制台验证证据](docs/WEB_CONSOLE.md#7-已保留的验证证据)。
