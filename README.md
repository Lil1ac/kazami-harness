# Agent Harness

Agent Harness 是一个在本机执行代码任务的 Agent 工作台。打开一个项目，描述要改的行为，Agent 就可以搜索代码、编辑文件、运行经批准的命令。你可以在同一段会话里查看补丁、测试输出和运行轨迹，再决定是否采纳改动。Windows 桌面端、Web 控制台和 CLI 共用同一个 Python Runner。

## 快速开始

源码开发使用 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node.js 22.12 或更新的兼容版本。以下 PowerShell 命令从仓库根目录执行。

### Windows 桌面端

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-desktop.ps1
```

启动脚本会安装依赖、构建前端并打开 Electron 工作台。后续可用 `npm.cmd --prefix desktop run dev` 启动，修改前端后需先重新构建。

1. 在「运行配置 → 模型服务」添加服务商，填写兼容 Chat Completions 的接口地址、模型 ID 和所需凭据。桌面端通过操作系统加密保存密钥。
2. 在首页新建项目并绑定本机文件夹。在「项目设置」可填写该项目已有的测试命令和 Python 解释器。
3. 输入任务，例如“找到项目里处理空输入的函数，补一个回归测试并运行它”。命令或验收需要批准时，页面会显示实际参数和目标。结束后打开本轮详情，查看文件差异与测试结果。

默认直接修改所选目录，包括其中已有的未提交改动。Git 项目也可以在新建会话时选择独立工作树，随后预览、采纳和复验改动。未配置验收器时，任务结果显示为“未验收”。[项目与工作树](docs/LOCAL_PROJECTS.md) · [成果采纳](docs/INTEGRATIONS.md)

### Web 控制台与 CLI

```powershell
uv --cache-dir .uv-cache sync --frozen
Copy-Item harness.example.toml harness.local.toml
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
uv --cache-dir .uv-cache run harness serve --config harness.local.toml
```

首次运行时，在 `harness.local.toml` 中登记服务地址和模型 ID。有认证要求的服务只在配置里写 `api_key_env`，将密钥放入启动终端对应的环境变量。本地配置已加入 `.gitignore`。启动后打开 [本地工作台](http://127.0.0.1:8765/runs)。[模型配置示例](docs/MODELS.md)

只用 CLI 时，完成 Python 依赖和模型配置即可。下面的示例读取仓库自带的虚构日志。将 `RUN_ID` 替换为第一次运行输出的编号即可查看记录。

```powershell
uv --cache-dir .uv-cache run harness run --config harness.local.toml --workspace examples/logs --task-file examples/logs/task.md
uv --cache-dir .uv-cache run harness show RUN_ID --json
```

CLI 退出码 `0` 表示执行完成且独立验收通过，`3` 表示执行完成但未验收，`4` 表示正在等待人工输入或审批。完整交互式审批可在桌面或 Web 中完成。[任务与验收](docs/PROJECT_TASKS.md) · [退出码与运行记录](docs/TECH_DESIGN_V0.1.md)

## 工作台能力

| 能力 | 使用方式 |
| --- | --- |
| 多轮任务与验收 | 在同一会话追问或追加要求，回看每轮工具回执、文件差异和测试。配置公开项目测试后，可开启最多两次的失败自动修复。[多轮任务](docs/MULTITURN_TASKS.md) |
| 权限与扩展 | 为会话选择仅研究、工作区编辑或严格审批。宿主策略限制目录和动作，项目可启用已审核的 Hooks、技能及 MCP 工具。[权限](docs/PERMISSIONS.md) · [Hooks](docs/HOOKS.md) · [Skills](docs/SKILLS.md) · [MCP](docs/MCP.md) |
| 上下文管理 | 固定本轮的 `AGENTS.md` 和技能版本，每次模型请求前核算输入预算。混合压缩将大段工具输出归档，必要时遮蔽旧结果、生成摘要，Agent 可按引用回读证据。[上下文管理](docs/CONTEXT_MANAGEMENT.md) |
| 项目长期记忆 | 跨会话检索项目历史及已确认条目。选取历史运行生成整理草案，逐条核对来源并发布，后续会话可查看实际采用的修订。[项目记忆](docs/MEMORY.md) |
| 队列与中断接续 | 不同项目可并发，资源重叠的任务排队。长命令支持查看输出、停止和安全暂停。后台异常退出后，可在队列核对状态并手动接续符合条件的运行。[调度与恢复](docs/SCHEDULING.md) · [长任务](docs/LONG_RUNNING.md) |
| 委派与自动化 | 规划任务可拆成带依赖的工作项，按角色委派探索、审阅或隔离开发。时间或本机事件规则可预览、试跑和启用。[子 Agent](docs/SUBAGENTS.md) · [团队](docs/TEAMS.md) · [自动化](docs/AUTOMATIONS.md) |

普通项目命令在本机执行。会话审批约束可执行的动作，操作系统级的进程与网络隔离需由运行环境提供。真实模型请求会将任务及选入上下文的内容发送到配置的服务商。

## 可复现评测

`harness eval` 固定题集、模型配置、预算和评分条件。每个试次使用独立工作副本，宿主对候选补丁评分。Web 的「评测」页可按批次回看环境预检、逐题结果和原始运行。先用仓库自带题集做零模型预检，验证起点触发预期失败、参考解通过评分。

```powershell
uv --cache-dir .uv-cache run harness eval --suite evals/context-v1/suite.json --config evals/context-v1/deepseek.toml --split dev --runs-dir .runtime/eval/runs --work-dir .runtime/eval/work --preflight
uv --cache-dir .uv-cache run harness serve --config harness.local.toml --runs-dir .runtime/eval/runs
```

2026-11-18 的 SWE-bench Verified 对照固定了 20 道题，每组每题运行一次。两题的基线适配器发生超时异常，形成 18 个有效配对。以下分母为各组全部 20 次正式试次。

| 指标 | Agent Harness | mini-SWE-agent 2.4.6 |
| --- | ---: | ---: |
| 补丁通过官方评分 | 15/20 | 16/20 |
| 正常结束且通过官方评分 | 12/20 | 15/20 |

Agent Harness 还检查最终补丁能否通过同一入口的复验，有 **11/20** 次满足这项完整交付条件。以上结果只描述该次题单。真实仓库评分需要另行准备固定版本的 SWE-bench 环境和 Docker 镜像。[冻结题单](evals/repository-comparison/protocol.json) · [评测方法与各批次记录](docs/EVALUATION.md)

## 开发与检查

完整后端回归包含外部基线和三组对照的调度测试。首次运行时，安装锁定的评测依赖，并用仓库脚本从固定快照生成上游题目记录。

```powershell
uv --cache-dir .uv-cache sync --frozen --group evaluation
uv --cache-dir .uv-cache run --no-sync --with pyarrow python evals/swe-three-arm/build_suite.py
```

```powershell
uv --cache-dir .uv-cache run --group evaluation pytest -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

后端位于 `src/agent_harness/`，React 控制台位于 `frontend/`，Electron 宿主位于 `desktop/`。运行轨迹默认写入 `.runs/`，本地缓存和评测产物保存在 `.runtime/`、`.tmp/` 或 `output/`。这些目录可能包含任务内容、代码副本及原始日志，仅保存在本机。

构建 Windows 安装程序和便携 ZIP 可运行 `scripts/build-desktop.ps1`，产物位于 `dist/desktop/`。[桌面构建](docs/DESKTOP.md) · [前端开发](frontend/README.md) · [项目结构与文档导航](PROJECT.md)
