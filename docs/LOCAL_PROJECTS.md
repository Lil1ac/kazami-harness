# 打开本机项目

首页以“打开文件夹、输入任务、发送”为主流程。当前面向与 Python 服务同机的 Windows 单用户；不是上传本机文件到远程服务。首次使用不需要编辑 TOML 登记项目。

## 1. 使用流程

1. 按 README 构建前端并启动 `harness serve --config harness.example.toml`。
2. 点击“选择项目文件夹”，在系统窗口选择真实目录。取消保留已输入文字，不登记目录、不创建运行、不调用模型。
3. 输入框附近显示目录名、实际路径和执行方式；Git 分支和提交收在高级设置。侧栏点击项目进入会话列表，再新建或继续已有对话；读取列表不改变打开时间。
4. 输入任务后发送，直接修改所选目录。高级设置可选择技能、预期产物或项目测试；Git 项目可明确选择“创建独立 Git 工作树（更换目录）”，此时才显示提交基线。执行位置不从文字草稿恢复。
5. 在会话中查看工具轨迹、产物、测试与代码差异，并继续发送要求。原目录修改已经生效；隔离 Git 会话共用一个工作树，仅最新轮提供保留/丢弃，旧轮证据只读。

新任务文字、基线和预期产物保存在当前浏览器标签页的 sessionStorage；调整配置或刷新后可以继续编辑，后端明确接纳后清除。最近选中项目只在浏览器保存编号。密钥继续来自服务进程环境，不通过项目设置保存。

## 2. 目录与执行方式

| 来源 | 处理方式 |
| --- | --- |
| 已有提交的 Git 根目录 | 默认直接使用所选目录的当前文件，包含未提交改动；高级设置可明确选择隔离 worktree |
| 尚无提交的 Git 根目录 | 可以登记并展示不可用原因；不能发送代码任务，不降级成普通目录 |
| 普通目录 | 始终在所选目录执行，不提供副本模式；文件、命令和测试使用同一目录 |
| Git 子目录、裸仓库或损坏的 Git 来源 | 明确拒绝，提示选择仓库根或检查 Git |
| 不存在、无权限或位于运行记录内的目录 | 明确失败，不改写项目代码 |

原目录差异采集排除本服务的运行记录和 Git 工作树整棵目录，避免把其他会话记录带入证据。若普通来源本身处于受保护目录内部，则拒绝执行。同一路径之后初始化或移除 Git 元数据，明确重新打开时会更新类型并保留编号。

任务仍是可信本机执行，没有容器或进程权限沙箱，不自动安装依赖、提交代码或合并原分支。Git 子模块及 LFS 等边界沿用 [代码任务与审阅](PROJECT_TASKS.md)。

### 原目录执行与逐轮证据

新会话固定来源路径、执行方式和目录对象身份。工具读写、命令的 cwd、项目测试 cwd 都使用该目录。续轮不复制文件、不创建 worktree、不应用旧补丁；允许用户在两轮之间手动改文件，Agent 使用当前文件。目录被移动、删除、链接替换或同名重建时拒绝继续，避免把旧会话写到另一目录。

会话页始终显示“所选目录／独立 Git 工作树”和实际执行路径。会话执行方式固定，记录按当前格式核验。

每轮原目录执行前后分别采集有界快照（各最多1000项、16MiB），排除 Git 控制目录、常见依赖缓存及服务记录/工作树目录。报告相对该轮开始状态，而非 HEAD；二进制保留原始快照并列出变化，文本差异最多32MiB。它是审阅记录，不提供自动回滚或二进制补丁应用。采集超限、特殊文件或读取期间变化时明确报告不完整；实际目录仍可执行和继续，不因为差异限制强制改到副本。

原目录运行的历史工作区产物从该轮结束快照取回，后续覆盖同名文件不会改变旧轮下载；快照缺失或损坏返回409，不冒充旧产物返回当前内容。隔离模式的既有产物访问规则保持。记录在 `.local-before/<run_id>`、`.local-after/<run_id>` 和 `.local-changes/<run_id>`，都位于受保护的 runs_root 中。

网页提交明确发送 execution_mode，省略时统一默认 local，不再根据登记方式或隐藏配置推断。isolated 仅接受 Git 项目；普通目录请求隔离执行返回 workspace_copy_not_supported。CLI 直接使用 --workspace 指定目录。

## 3. 项目测试与最近记录

所有真实项目的“高级设置”可保存 Python 解释器，空白按 PATH；Git 项目另外可保存测试命令和超时（秒，必须大于 0 且不超过 3600）。例如：

```text
D:\projects\AgentHarness\.venv\Scripts\python.exe -m pytest -q
```

请替换为该项目已安装依赖的解释器。含空格的程序或参数使用双引号。Windows 按系统命令行规则解析，其他平台按 POSIX 规则解析；保存后固定为参数数组，不经过 shell。Windows 的脚本应显式指定 `.exe` 解释器；程序入口不能使用 `{workspace}`，其他参数可引用该占位符。

保存只核对程序入口与设置，不执行命令。未配置或留空并保存时，任务依然可以执行，最终保持 `unverified`。已配置时，后续新运行在该轮实际执行目录中检查。原目录不自动暂存、提交、检出分支或应用补丁；隔离模式的测试通过与用户保留工作树仍是两个状态。旧运行的事件和验收结果不因更新设置而重写。默认 Python 导入目录是该轮实际执行目录的 `.` 和 `src`；可选 TOML 预设可以明确调整。

登记存放在 `runs_root/.local-projects/catalog.json`（默认 `.runs/.local-projects/catalog.json`），最多 100 项、256 KiB，以临时文件和原子替换保存。规范路径去重、编号稳定，重启后保留；损坏记录返回明确错误，不静默清空或覆盖。

TOML 的 projects/workspaces 仍可作为初始配置；明确打开的同路径记录更新类型与测试设置。如果宿主后来把同一编号分配给另一路径，旧本机记录不能覆盖新配置。默认不登记或创建演示目录；显式登记的普通目录必须已经存在。`worktrees_root` 仅配置 Git 隔离工作树的父目录，按任务需要创建。

## 4. HTTP 契约

所有修改接口沿用 localhost Host 和浏览器同源检查，错误结构为 `{error:{code,message}}`。

| 方法与路径 | 请求与响应 |
| --- | --- |
| `GET /api/options` | 返回 `recent_projects`、`folder_picker_available`，以及已有配置、projects/workspaces、验收器、技能和忙碌状态；列表属于同一次登记快照 |
| `POST /api/projects/pick` | 无需路径请求体，等待系统窗口；成功返回 `{cancelled:false,project:LocalProjectOption}`，取消返回 `{cancelled:true,project:null}` |
| `POST /api/projects/{id}/open` | 只接受已登记编号，重新检查目录并更新最近时间；返回 `LocalProjectOption` |
| `PUT /api/projects/{id}/settings` | JSON：`test_command`（最多 4096 字符，可空）与 `test_timeout_seconds`（默认 30）；只支持 Git 项目，返回更新后的 `LocalProjectOption` |
| `POST /api/runs` | `project_id` / `workspace_id` 二选一，`execution_mode=local/isolated`；原目录不接受其他 Git 基线，不接受任意宿主路径或验收命令 |

`LocalProjectOption` 包含 `id/label/path/kind/available/error/last_opened_at`。Git 项目增加分支、HEAD、未提交标记、测试参数、回显命令与超时。选目录和重新打开不会创建工作树、执行测试或调用模型。

选择器由独立标准库 GUI 子进程提供，同一服务最多一个窗口，等待上限 180 秒。Windows 直接启动基础 Python 解释器，超时或请求协程取消后终止并等待 GUI 进程；辅助控制台隐藏，系统选择框正常显示。专用环境仅补齐 Shell 路径变量，不继承模型密钥或 Python 注入变量；工作目录固定为系统临时目录，Agent 工具的受限进程环境保持原规则。

不可用环境返回 `folder_picker_unavailable/503`，已有窗口 `folder_picker_busy/409`，等待超时 `folder_picker_timeout/408`，其他窗口故障 `folder_picker_failed/503`。目录原因包括 `project_root_required`、`project_bare_unsupported`、`project_records_forbidden` 等；记录损坏或写入失败是 503。不存在的最近项目、坏命令或程序不可用均明确反馈并保留输入。

## 5. 验收与参考

产品验收对应 PRD P01—P06。单元和接口回归覆盖目录识别、去重与重启、类型切换、原子保存、选择取消/超时/并发、跨源拒绝、命令引号往返、登记并发、记录目录排除、无测试运行及保存测试后续轮。真实页面与最终测试结果统一见 [控制台验证记录](WEB_CONSOLE.md#7-已保留的验证证据)。

交互参考 [Codex app](https://openai.com/index/introducing-the-codex-app/) 的项目与任务组织，以及 [WorkBuddy 任务管理](https://www.codebuddy.cn/docs/workbuddy/Task-Management) 的历史入口。仅采用与当前功能相符的层次，不代表具备其并行、云端或桌面集成能力。

项目设置 PUT /api/projects/{id}/settings 接受 python_executable（空白或 null 为 PATH）、test_command 与 test_timeout_seconds；普通目录可保存解释器，项目测试仍由 Git 项目配置。保存不启动模型或命令，旧轮快照不随设置变化。
