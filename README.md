# Agent Harness

项目可通过 `.agents/hooks.toml` 配置工具前检查、写入后格式化、后台命令结束及完成检查等生命周期动作。先在运行配置或项目设置审核定义，执行仍遵守原命令权限；CLI 提供 `harness hooks list/trust/enable/disable`。配置、审批接续和事件协议见 [Hooks](docs/HOOKS.md)，示例见 [examples/hooks](examples/hooks/hooks.toml)。

面向后端开发者的本地代码任务工作台，以 Windows 桌面端为主要入口，支持直接修改所选目录、可选独立工作树、项目测试和代码差异。Electron 桌面端、CLI 与网页共用一个 Python Runner，支持模型多轮调用、读取/搜索/写入文件、执行命令、保存轨迹和产物，以及可选的宿主独立验收。

当前提供本地单用户、受控跨项目并发控制台。任务产物可以是代码、文本、CSV 等格式；没有验收器时保持“未验收”。任务结束后可在详情页追加要求，继承会话历史和已有代码，在独立新一轮中执行、测试和审阅；使用与边界见 [多轮任务交互](docs/MULTITURN_TASKS.md)。当前阶段、后续路线、目录职责和完整文档导航见 [PROJECT.md](PROJECT.md)。

## 安装与密钥

Windows 发布版运行安装程序，或解压发布包后双击 `Agent Harness.exe`。Python 后台与界面随应用分发；在“运行配置”输入 DeepSeek API Key，应用使用操作系统加密后保存。桌面启动与构建详见 [桌面端说明](docs/DESKTOP.md)。

源码运行需要 Python 3.12、uv；源码桌面端和网页还需要 Node.js 22.12 或更新兼容版本完成构建。所有命令在项目根目录执行。

```powershell
uv --cache-dir .uv-cache sync --frozen
uv --cache-dir .uv-cache run harness --help
```

CLI 和独立网页服务从启动终端读取 `DEEPSEEK_API_KEY` 或 `DASHSCOPE_API_KEY`。桌面端使用设置页按供应商保存的加密凭据。Qwen 首版使用百炼北京地域，可通过 `harness run --provider qwen --model qwen-plus` 选择。完整参数、角色配置与凭据规则见 [模型供应商与角色配置](docs/MODELS.md)。

PowerShell 可以用密码框输入 CLI 凭据，避免把明文写入命令历史。

```powershell
$credential = Get-Credential -UserName 'deepseek' -Message '在密码框输入 DeepSeek API Key'
$env:DEEPSEEK_API_KEY = $credential.GetNetworkCredential().Password
```

密钥不进入网页或运行记录。真实模型运行会发送任务和读取的内容并消耗服务额度；仓库样例使用虚构数据。

## 启动桌面端

源码首次启动会自动准备依赖并构建界面：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-desktop.ps1
```

后续直接运行 `npm.cmd --prefix desktop run dev`。桌面程序自动启动本机后台，不需要另开服务或浏览器；前端改动后先重新构建。源码默认复用仓库 `.runs`，桌面公开设置、窗口状态及缓存放在 `.runtime/desktop`。

构建 Windows 安装包及便携 ZIP：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
```

产物位于 `dist/desktop`。下载默认保存到 `D:\Codex\Downloads`；发布版应用数据优先保存在 `D:\Codex\AgentHarness`，可通过 `--data-dir` 指定。任务运行中关闭窗口会先确认取消并等待清理；后台不会在无窗口状态下继续执行。

## 启动网页控制台

Windows 下用 `npm.cmd` 避免 PowerShell 脚本执行策略影响。首次安装使用临时 npm 11，可避开本机旧版 npm 的依赖解析问题，不改变全局 npm：

```powershell
npx.cmd --yes npm@11 --prefix frontend ci
npm.cmd --prefix frontend run build
uv --cache-dir .uv-cache run harness serve --config harness.example.toml
```

打开 [本地任务工作台](http://127.0.0.1:8765/runs)。构建后的页面由同一个 Python 服务提供；后续不修改前端源码时，只需运行最后一条命令。

首页可搜索选择已有项目，或点击“新建项目”，填写名称、选择本机文件夹并确认绑定，再输入任务并发送。侧栏按项目组织已有对话，首页提供最近会话；每段多轮会话只显示一条，项目页可明确新建对话。高级设置提供技能、执行方式与自动修复；项目测试和解释器在项目页“项目设置”保存，全局模型与预算在“运行配置”调整，任务结束后自动收集本轮新增、修改的文件。会话详情默认收起，点击“查看详情”或各轮“本轮详情”查看测试、轨迹和产物；刷新或关闭网页不会取消后台任务。首次使用不必编辑项目目录配置，具体操作与限制见 [打开本机项目](docs/LOCAL_PROJECTS.md)。

## 使用自己的项目

在首页打开 Git 仓库根目录，页面显示真实路径、分支、提交和未提交状态。默认直接在所选目录中读写，包含当前未提交改动；会话顶部显示实际执行路径，多轮继续使用同一目录。高级设置可明确选择隔离执行，再从指定基线建立 worktree；只有隔离工作树提供保留/丢弃，保留不自动合并。没有配置测试也能执行，结果明确保持“未验收”；可在项目设置保存已有测试命令、超时和解释器，保存本身不运行命令。依赖环境及审阅流程见 [代码任务与审阅](docs/PROJECT_TASKS.md)。

保存公开项目测试后，可开启“测试失败自动修复（最多2次）”。测试断言失败时，Agent沿用同一任务和剩余预算修改、重测；结果页保留每次检查、原始输出和最终结果。默认关闭，测试异常和取消不会触发修复。后续消息可在“下一轮设置”调整开关。

普通文件夹始终在所选目录执行；Git 项目可明确选择独立工作树。执行位置不从文字草稿恢复。原目录差异采集超限会明确提示，但不会把任务改到另一目录或阻止继续；旧轮产物保存在固定快照中。最近项目登记保存在当前运行记录根目录的 .local-projects/catalog.json，重启服务后保留。系统选择框当前支持与服务同机运行的 Windows 桌面，不能选择另一台电脑的目录；详见 [打开本机项目](docs/LOCAL_PROJECTS.md)。

## 权限与动作审批

工作树运行完成后，可在“文件 → 成果采纳与历史”选择同仓库已登记目标，独立预览合并、解决冲突并检查；确认后写回，原暂存区保留，目标复验另行批准。支持多轮增量、逐文件中断核对和受保护撤销；保留工作树仍只保留独立目录。使用规则见[成果采纳](docs/INTEGRATIONS.md)。本轮沿用本机执行，不需要Docker，也不提供操作系统命令沙箱。

新建与下一轮设置提供“仅研究 / 工作区编辑 / 严格审批”。默认允许已启用文件工具修改工作区，命令和宿主验收需逐项批准；先规划的只读限制独立生效，批准计划不会自动批准命令。页面展示原参数与实际目标，批准后宿主执行保存的动作；拒绝和取消不执行。等待释放执行槽，纯审批接续沿用剩余预算与修复次数，刷新和重启不自动重放。

宿主可在 TOML 的 `[permission_policy]` 收窄工具与目录范围，页面不能覆盖。当前仍是本机执行，审批不提供进程、网络或容器隔离。CLI遇到待审批动作返回4，暂不提供CLI审批恢复命令；完整人工交互请在桌面或Web创建会话。配置、故障边界与验收见[权限协议](docs/PERMISSIONS.md)。

## 项目约定与上下文预算

任务会自动加载实际工作区根目录的 `AGENTS.md`，固定为本次运行快照；每轮请求前检查消息和工具定义的整体输入大小。加载开关与内部上限由宿主 TOML 配置，详情“上下文”展示来源、快照和最新检查。默认整体输入256 KiB、指令文件32 KiB。示例配置启用分层压缩：成功大输出外置、旧结果遮蔽、必要时生成结构化摘要；Agent可通过只读引用回读。摘要占模型轮次与用量，仍超限时停止并保留修改。字节计量与token用量分开，可选声明窗口用于本地估算限制；未显式配置时默认关闭压缩。配置、事件和边界见 [上下文管理](docs/CONTEXT_MANAGEMENT.md)。

## 任务技能

“运行配置 → 外部工具连接”支持审核并连接 stdio/Streamable HTTP MCP 服务；新任务与下一轮可选择固定版本工具，具体调用默认逐次审批，详情保留来源和结果。桌面凭据按连接加密存储，独立 Web/CLI 使用环境变量引用。配置、权限、故障边界与验收见 [MCP 工具接入](docs/MCP.md)。

新任务可手动选择宿主登记的 SKILL.md，后续消息默认沿用，也可更换或清空。详情“上下文”保存本轮完整技能正文和指纹，正文计入输入预算并受压缩保护。示例配置已登记 examples/skills，默认不选择；CLI 可在配置中填写 skills = ["code-delivery"]。登记方法及当前边界见 [任务技能](docs/SKILLS.md)。

## CLI 常用命令

读取仓库日志并返回文字统计：

```powershell
uv --cache-dir .uv-cache run harness run --task-file examples/logs/task.md --workspace examples/logs --config harness.example.toml
```

要生成脚本和报告并进行独立验收，先创建新的样例工作区：

```powershell
$demoWorkspace = Join-Path '.runtime' ('logs-' + [Guid]::NewGuid().ToString('N').Substring(0, 10))
New-Item -ItemType Directory -Path $demoWorkspace | Out-Null
Copy-Item -LiteralPath examples/logs/app.log -Destination (Join-Path $demoWorkspace 'app.log')

uv --cache-dir .uv-cache run harness run --task-file examples/logs/task_artifacts.md --workspace $demoWorkspace --config harness.example.toml --verifier examples/verifiers/logs.toml
```

CLI 与网页均自动登记本轮新增、修改的文件，无需声明输出路径；删除文件保留在差异中，未修改的已有文件和依赖缓存不列为本轮产物。文件收集不等于内容验收。固定日志验收器只适用于仓库输入，其他任务需配置相应的宿主验收程序。

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

根目录的 `harness.example.toml` 配置模型、工具、上下文和内部限制；普通设置页只开放模型、工具、模型轮次、工具次数和总时限。项目目录可直接从页面打开。`web.example.toml` 可选登记初始项目、工作区和验收器，启动时用 `--web-config web.example.toml` 加载。内置默认只启用 `read_file`，显式传入示例运行配置才启用读、搜、写、局部编辑和命令五类工具。修改已有文件可使用带版本核验的 edit_file，统一 LF/CRLF 文件支持等价匹配并保持原换行与 BOM；具体协议见 [文件工具](docs/TECH_DESIGN_V0.1.md#6-文件与命令工具及本地执行边界)。CLI 可用 `--model` 和 `--max-model-turns` 覆盖配置。

执行、验收、文件收集和清理分别记录；模型回答结束不代表产物正确。退出码如下，PowerShell 用 `$LASTEXITCODE` 查看：

| 退出码 | 含义 |
| --- | --- |
| 0 | 真实执行完成、独立验收通过且清理可确认 |
| 1 | 执行失败、达到上限、验收未通过/异常或清理未知/不完整 |
| 2 | 启动参数、配置或密钥错误 |
| 3 | 执行完成但未验收 |
| 4 | 等待用户回答、计划批准或动作审批，尚未完成整条交付 |
| 124 | 运行或单模型轮次超时 |
| 130 | 用户/上游取消或服务关闭 |

`show` 返回 0 仅表示读取成功。当前执行可信本地命令，路径检查不等于容器隔离；命令边界、超时、取消、验收协议与记录结构见 [技术设计](docs/TECH_DESIGN_V0.1.md)。

CLI使用`--auto-repair`明确开启修复，`--verifier`清单必须声明`kind = "project_tests"`，并配置自己的公开测试argv/cwd，例如`argv = ["python", "-m", "pytest", "-q"]`。清单未声明kind时默认独立验收，不允许模型获得结果反馈；整体轮次、调用和时限不会因修复重置。

## 检查

```powershell
uv --cache-dir .uv-cache run pytest -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

普通测试使用脚本化模型、MockTransport 和本地子进程，不依赖真实密钥或联网。平台不支持的符号链接/FIFO 用例会跳过。前端开发与源码阅读见 [前端工程说明](frontend/README.md)，已有验证记录集中在 [控制台验证证据](docs/WEB_CONSOLE.md#7-已保留的验证证据)。

### 中断后继续

服务重启后打开原会话，在输入框发送“继续”即可。系统核对已有记录和当前代码，开始新一轮并重新测试；结果不明的旧工具先核对现场，不自动重放。记录损坏或来源无法核对时显示原因。详细边界见[多轮任务交互](docs/MULTITURN_TASKS.md#10-中断后直接发送继续2026-10-23)。

### 项目执行目录与 Python 环境

默认直接修改所选目录；明确选择 Git 隔离时，一个会话复用一个工作树。各轮证据固定保存，只有最新轮可保留/丢弃，历史产物由快照读取。

项目页“项目设置”可保存 Python 解释器；空白按 PATH。CLI 可使用 --python-executable 指定入口。命令与裸 Python 验收统一解析，依赖环境需提前准备。详细规则见 [Python 执行配置](docs/PYTHON_EXECUTION.md)。

项目入口支持在首页新建命名项目并绑定本机文件夹，或按名称/路径选择已有项目；同一目录复用已有登记。项目设置可独立修改名称，刷新或重启后保留绑定，详见[项目与目录绑定](docs/LOCAL_PROJECTS.md)。

## 任务清单与后台工作

侧栏“自动化”可保存时间或本机普通运行事件规则，先预览、试跑，再明确启用。每次执行使用独立会话，Git默认工作树；遇到未授权动作仍等待人工处理。宿主可明确开启`permission_policy.automation_grants`，再从页面审核精确命令、固定验收及已审核Hook的持续许可；默认关闭。漏跑、重叠、启停和结果通知见[本机自动化](docs/AUTOMATIONS.md)。电脑及宿主需保持运行。

批准计划后自动生成持久工作项，Agent 通过 task_create/get/list/update 管理依赖、进度和完成证据。会话内可查看、编辑待处理项、取消或重新打开；后台工作页查看排队、运行和待核对作业。默认两个独立项目并发，同仓库与重叠目录仍串行；退出保留未执行队列，重启按明确选择恢复。历史记录不自动重放工具；后台页面提供资源等待原因、单项停止和启动恢复设置，详见[调度与恢复](docs/SCHEDULING.md)及[任务协议](docs/TASKS.md)。

长命令可通过 `run_command(background=true)` 返回句柄，使用 `read_command`、`wait_command` 和 `stop_command` 管理；同一目录有活动命令时不继续写文件。页面执行中可发送本轮补充或排队下一轮，支持安全暂停和明确恢复；恢复沿用原预算，未知进程结果先核对。桌面/Web 可查看父子命令、增量输出和处理状态；详见[长任务协议](docs/LONG_RUNNING.md)。


## 项目长期记忆

项目页“项目记忆”可保存、检索、修改、删除及查看修订；会话消息可保存为记忆，Agent 提出的候选经用户保存后供新会话使用。新任务及下一轮设置独立控制使用与候选，运行上下文展示当时真正使用的固定版本。删除影响后续运行，历史快照保留。type 现区分行为约定、决策、事实、入口和经验：动态字段、动作触发、事实证据复核与冲突审阅统一生效；形成方式单独记录。协议及验收见[项目记忆](docs/MEMORY.md)。

全部会话及项目页支持命名、移动、置顶、归档、回收站恢复、单条彻底删除和当前页批量整理。移动只改变列表归属，继续执行仍使用原目录；彻底删除清理会话记录，保留项目代码和另存记忆。详见[会话管理](docs/CONVERSATION_MANAGEMENT.md)。
