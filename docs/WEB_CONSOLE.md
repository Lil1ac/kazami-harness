# 本地任务控制台

本地网页复用现有 Runner：任务 API 负责提交/查询/取消，SSE 传输已落盘事件，React 负责展示和交互。任务执行、工具和独立验收使用同一后端核心。

本文维护 Web 操作、宿主配置、HTTP/SSE 契约与集中验证证据；整体交付阶段和完整文档导航见 [项目概览](../PROJECT.md)。

## 2026-11-03：工作树成果采纳与复验

“文件 → 成果采纳与历史”贯通目标选择、独立冲突解决、候选检查、确认清单、保护暂存区写回及目标复验；后台中心能定位实际采纳记录。三类测试与代码版本分别绑定，下载保留本次原始输出，桌面复用原生受认证下载。真实页面完成冲突到写回、复验和刷新；390像素抽屉与采纳面板无横向裁切，最终浏览器零错误、零警告。

后端全量858项通过、11项平台跳过，前端全量181项通过，最终桌面6项通过；最后状态收尾、中断恢复与布局修复的后端关联18项、前端关联18项通过，与全量重叠。完整协议、限制、失败复跑说明、审计编号和截图见[成果采纳验收](INTEGRATIONS.md#8-验收记录)。本轮仍为可信本机执行，没有Docker或操作系统命令沙箱。

## 2026-11-02：权限与动作审批验收

任务入口和持续会话提供仅研究、工作区编辑、严格审批三种模式；实际权限由后端返回。审批卡片展示固定文件内容或完整命令、实际目标与到期时间，支持批准本次、拒绝、取消；检查器保留当轮快照和只读历史，后台中心区分等待、已批准排队和已处理。

本轮页面使用真实HTTP、队列、文件和进程，核对拒绝覆盖保留原文、取消创建无文件、命令只执行一次、验收批准后零模型直接通过。390像素手机页面无横向溢出，控制台零错误、零警告；刷新不增加运行或重复副作用。前端177项、桌面集成5项、后端全量842项通过（另11项平台跳过）；最后清理修正的关联35项通过、1项跳过，与全量重叠。配置、接口、审计编号及截图集中在[权限控制与验收](PERMISSIONS.md#7-验收记录)。

## 2026-11-01：会话管理验收

按获批Plan交付整段会话命名、移动、置顶、归档、回收站恢复、单条彻底删除及当前页批量整理。统一服务端标题和整理归属，执行来源、项目记忆和已有批准范围不随移动改变；任务、作业、计划决定、运行正文和桌面产物入口执行生命周期限制。清理先持久冻结UUID清单，失败/重启可重试，项目代码保留。接口、存储和删除边界见[会话管理](CONVERSATION_MANAGEMENT.md)。

本轮检查结果：

- 后端全量799项通过、10项平台跳过（546.97秒）；补齐损坏状态、链接和桌面下载边界后，会话管理11项通过、1项Windows链接权限跳过。最后HTTP缓存修复后的全部Web与桌面接口192项通过、2项平台跳过（316.04秒）。统一未人工整理的续轮作业标题后，管理完整文件11项通过、1项平台跳过（12.94秒），队列完整文件另行复验通过。
- 前端最终整套34文件162项通过（126.04秒）；之后新增多窗口实时更新基线冻结，管理组件9项补验通过（12.90秒）。类型检查、生产构建通过，Electron5项通过。保留Vite已有大包提示及jsdom样式/伪元素模拟提示；初次整套一项既有详情超时，原断言/时限独立9项及后续全套复跑通过，失败日志保留。
- 真实本地页面完成命名、置顶、跨项目移动、标题/首需求搜索、归档及取消归档、原目录续轮、三项批量删除、旧轮链接恢复、其余两项批量恢复和一项明确确认后彻底清理。移动会话整理归属为文档项目，实际两轮仍在原日志项目目录执行；后台作业显示统一会话标题与归属。
- 发现并修复浏览器缓存410导致恢复后仍停留回收站的缺陷：生命周期错误和API读取禁止缓存，恢复显式重读原轮和会话。两个真实窗口验证状态同步、过期编辑409、草稿保留及明确刷新后的保存；弹窗冻结打开时版本，不能被SSE实时更新静默放宽。
- 390×844布局目检通过，文档宽和滚动宽均390。永久清理前后两个项目源码哈希相同；服务重启后管理视图、删除墓碑及三份保留事件哈希一致，运行请求仍为4条，没有整理导致的额外执行。控制台保留预期410/409和服务重启断连记录，无脚本异常。

本轮使用离线模型响应，API、队列、Runner、Git、文件工具和持久化真实执行，未调用真实供应商或重新打包桌面安装程序。专属夹具数据位于`D:/Codex/Temp/harness-management-browser-20261101`，可复用服务为`tests/helpers/serve_conversation_management_fixture.py`。

证据：[后端全量](../.runtime/conversation-management-20261101/backend-full.txt)、[最终Web及桌面](../.runtime/conversation-management-20261101/backend-integrated-final.txt)、[标题联动补验](../.runtime/conversation-management-20261101/backend-title-final.txt)、[队列补验](../.runtime/conversation-management-20261101/backend-queue-final.txt)、[前端整套](../.runtime/conversation-management-20261101/frontend-accepted.txt)、[多窗口补验](../.runtime/conversation-management-20261101/frontend-conflict-final.txt)、[页面与重启事实](../output/playwright/conversation-management-20261101/proof.json)、[桌面列表](../output/playwright/conversation-management-20261101/08-final-desktop.png)、[手机列表](../output/playwright/conversation-management-20261101/09-final-mobile.png)、[永久删除确认](../output/playwright/conversation-management-20261101/06-purge-confirm-mobile.png)、[冲突草稿](../output/playwright/conversation-management-20261101/07-multiwindow-conflict.png)。

## 1. 启动与开发

安装依赖、设置密钥与首次构建按 [README](../README.md#安装与密钥) 执行。构建完成后，在项目根目录启动：

```powershell
uv --cache-dir .uv-cache run harness serve --config harness.example.toml
```

打开 [本地任务工作台](http://127.0.0.1:8765/runs)。在宿主 TOML 登记服务地址和模型 ID，使用 API Key 时在启动终端设置相应环境变量。独立网页只报告凭据状态。首次配置前可以浏览历史，提交任务需要选择已登记的模型。见 [模型服务](MODELS.md)。

前端依赖由 package-lock.json 锁定，后端依赖由 uv.lock 锁定。前端构建后由 Python 服务统一提供，不需要常驻第二个 Node 服务；修改前端源文件后重新 build 并刷新。--port 支持 1 至 65535，默认 8765，固定绑定 127.0.0.1，单 worker 运行。
开发时分别启动Python服务与：

```powershell
npm.cmd --prefix frontend run dev
```

Vite默认在127.0.0.1:5173提供页面，将/api代理到127.0.0.1:8765。前端只需相对API路径，生产构建沿用同源访问。使用其它开发端口时需要同步调整代理与宿主Origin规则。

## 2. 操作流程

- **选择或新建项目**：首页项目选择器可按名称或路径搜索已有项目；“新建项目”填写名称、通过Windows系统窗口选择Git根目录或普通文件夹，确认后绑定。同目录复用已有项目；项目设置可独立改名，侧栏按项目组织会话。直接修改所选目录；只有明确创建 Git 工作树时才更换执行位置；取消保留输入且不执行。详细协议见 [打开本机项目](LOCAL_PROJECTS.md)。
- **普通任务**：打开输入所在目录，填写任务并开始执行，结束后自动收集本轮新增、修改文件。任务产物可以是代码、文本、CSV等格式；不要求输出JSON。未选择验收器时最终明确显示“未验收”。
- **运行详情**：消息区实时展示模型文字与工具活动，结束后收拢过程并固定最终回答；工作栏按需打开任务、计划、Agent、结果、文件和诊断。右侧面板与窄屏抽屉独立于消息和输入，工具参数/输出按纯文本展开。产物提供64KiB以内UTF-8文本预览，二进制文件可下载。界面与增量协议见[工作台界面](UI_WORKSPACE.md)。
- **运行配置**：修改模型、五类工具和模型轮次、工具次数、总时限，仅影响本浏览器之后提交的任务；刷新页面恢复宿主默认。正在运行的配置不变，页面不修改服务端TOML。
- **取消**：明确点击并确认取消后，先显示正在取消，后端清理完成后才显示终态。刷新、页面切换、关闭网页或SSE断开只清理订阅，不取消Agent。
- **项目与会话**：侧栏进入项目会话列表，“新建对话”从当前项目开始，点击已有会话则进入完整消息区。首页最近会话与全部会话按稳定会话聚合，每段多轮工作只显示一条；手机使用项目导航抽屉。运行详情普通入口默认收起，按需展开，详情深链接保持可直接访问。
- **历史**：重新启动服务后可读取既有记录；读取不会重放工具动作。缺少可信结束事件的记录显示中断与警告，不推断其副作用已经完成。

执行、验收、清理状态分别展示。只有最终execution_status=completed、validation_status=passed且cleanup_status为not_needed/complete时展示成功。报告错误是验收failed；验收进程出错是error；两者都保留模型已完成的执行事实。

任务结束后可在会话底部继续发送。原目录模式复用所选目录，Git 隔离模式复用同一会话工作树；每轮保存轨迹、配置、累计差异与固定产物。旧轮只读，仅最新轮管理共享目录，运行中禁止删除。查看历史和服务重启不重放工具。协议见 [多轮任务交互](MULTITURN_TASKS.md)。

## 3. 宿主配置

首次使用可直接在页面打开目录；可选 --web-config web.example.toml 采用表数组登记初始工作区、项目和验收器。相对路径以该TOML所在目录解释：

~~~toml
worktrees_root = ".runtime/project-worktrees"
frontend_dist = "frontend/dist"
~~~

默认不登记项目、工作区或验收器，也不创建演示目录。可通过 `[[workspaces]]` 登记已经存在的普通目录，通过 `[[projects]]` 登记 Git 项目，通过 `[[verifiers]]` 登记通用独立验收器；对应示例与字段见 web.example.toml 和代码任务文档。所有目录默认原地执行，只有 Git 项目显式请求 isolated 才创建工作树。

预设编号必须唯一。任务接口只提交登记编号，不能传入任意宿主路径、验收 argv 或凭据；项目测试由独立的显式设置接口保存。普通目录差异采集排除运行记录与 Git 工作树目录。原目录和 Git 隔离的历史产物均读取对应轮的固定快照，缺失或损坏时返回409，不回退到当前同名文件。历史验收只代表当次运行。

`harness.example.toml` 管理模型与完整运行策略。普通设置页仅开放模型、五类工具和三项任务预算；上下文、重试、单次输出及清理限制由宿主 TOML 管理。API 的受限配置覆盖仍保留，页面不发送内部参数覆盖。记录只按当前格式读取，不迁移旧字段。容器隔离尚未实现。

## 4. 模块与生命周期

```text
React页面 → HTTP API → WebRunManager → Runner → 模型/工具/Verifier
                          ↓                ↓
                       活动句柄         RunStore记录
                          ↑                ↓
React时间线 ← SSE补发/订阅 ←───────────────┘
```

Web 接入集中在 `src/agent_harness/web/`：

| 模块 | 职责 |
| --- | --- |
| `config.py` | 宿主工作区、验收预设及构建目录 |
| `project_catalog.py` | 本机目录、最近项目与可选测试的持久登记 |
| `folder_picker.py` / `native_dialog.py` | 异步协调本机系统目录窗口、取消与超时 |
| `schemas.py` | 受限请求与运行视图 |
| `service.py` | 按 Run 的后台任务、进度、取消、历史与固定产物；资源调度见[协议](SCHEDULING.md) |
| `conversations.py` | 会话查询、追加条件、历史与工作区继承和幂等调度 |
| `conversation_index.py` | 从现有记录只读聚合项目会话、保守核验最新轮、筛选与分页 |
| `api.py` | HTTP/SSE、静态资源和受限产物访问 |

CLI 的 serve 只组装模块，不另建执行循环。核心目录和阅读路线见 [技术设计](TECH_DESIGN_V0.1.md#2-模块职责与调用方向)，前端阅读入口见 [前端工程说明](../frontend/README.md)。

提交预检和槽位占用在服务内串行处理。通过on_context等待Runner已创建记录后才返回202，避免“提交后立即取消”打断尚未开始的协程。任务由服务持有，生命周期独立于请求和浏览器连接。网页取消记录source=api_user，CLI信号仍记录user_sigint，停服记录service_shutdown。

停服先给HTTP/SSE请求2秒退出宽限，再由lifespan取消后台任务并等待已有进程清理。清理有单独预算，因此总退出时间不保证恰好2秒。开启多个worker、自动reload或同时从CLI操作相同工作区不属于当前调度保证。

## 5. HTTP契约

| 方法与路径 | 请求/返回含义 |
| --- | --- |
| GET /api/options | 默认配置、密钥存在、统一 recent_projects 项目列表、验收器、技能、选择器能力与忙碌任务ID；不返回重复的 projects/workspaces |
| POST /api/projects/pick | 系统选择目录，返回临时selection草稿（600秒），取消为selection:null；不登记、不启动任务 |
| POST /api/projects | selection_id与label确认项目绑定；同目录复用已有项目，返回201及项目选项 |
| PATCH /api/projects/{id} | 保存项目名称；不修改ID、绑定路径、执行设置和历史证据 |
| POST /api/projects/{id}/open | 重新核验已登记目录并更新最近时间，返回项目选项 |
| PUT /api/projects/{id}/settings | 从项目设置保存解释器；Git 项目还可保存测试命令与秒级超时；保存不执行 |
| POST /api/runs | 接收prompt、workspace_id/project_id二选一、项目base_ref或普通verifier_id、config；成功202，忙碌409 |
| GET /api/conversations | 先聚合会话再按project_id、最新轮status筛选及offset/limit分页；返回会话摘要items/total/active_run_ids |
| GET /api/runs/{id} | 返回统一RunView，不恢复执行 |
| GET /api/runs/{id}/conversation | 返回所属会话的逐轮要求/回答、最新轮与可继续条件；只读 |
| POST /api/runs/{id}/followups | request_id、prompt及可选config，成功202；同编号同载荷复用原运行，不重放 |
| POST /api/runs/{id}/cancel | 幂等请求取消，返回视图；任务可能仍在清理 |
| GET /api/runs/{id}/events | 返回seq>after的分页事件，limit为1至500；last_seq是当前磁盘有效最大序号 |
| GET /api/runs/{id}/stream | SSE补发与持续订阅，支持after和Last-Event-ID，取较大非负游标 |
| GET /api/runs/{id}/review | 读取终态项目审阅；中断遗留pending可补捕获一次，不恢复模型 |
| POST /api/runs/{id}/review | decision为keep/discard；独立保存审阅决定，不自动提交或合并 |
| GET /api/runs/{id}/diff | 固定隔离补丁或原目录逐轮文本差异，返回content/truncated/size_bytes |
| GET /api/runs/{id}/patch | 下载已核对完整性的完整补丁，丢弃后仍可访问 |
| GET /api/runs/{id}/artifacts | location+path必须匹配登记引用；preview=true返回文本预览，否则附件下载 |
| POST /api/file-references/list | 按实际执行来源列出受控项目文件，目录内分页 |
| POST /api/file-references/preview | 预览 UTF-8 文件或指定行段，返回与内容及来源绑定的版本 |
| GET /api/runs/{id}/file-references/{index} | 按从零开始的顺序读取该轮固定引用，沿用会话读取权限 |

RunView包含run_id、prompt、workspace、created_at、result、phase、event_count、last_seq、active、cancelling、interrupted、warnings、本次config、可空project审阅详情及可空context上下文详情；追加session_id、可空parent_run_id及从1开始的turn_index描述用户轮次。result继续使用RunResult及可空verification；毫秒、字节、字符等单位沿用后端定义。用户消息、工具输出、日志、指令和补丁按纯文本转义，Agent最终回答按受限Markdown渲染，不启用原始HTML。

config覆盖只接受requested_model、tools、skills、auto_repair、limits和context；limits/context按字段合并，分别复用RunLimits/ContextPolicy严格校验。提交配置不会改写宿主默认值。context保存本次指令快照与最新输入检查，尚未准备时为null；配置、事件和大小单位见 [上下文管理](CONTEXT_MANAGEMENT.md)。受控错误统一为{error:{code,message}}；校验错误不回显输入值、密钥或底层异常正文。

SSE逐条发送event=run_event、id=seq、data=完整RunEvent；每10秒注释心跳。终态或无活动任务的中断历史补齐已落盘事件后发送stream_end，其中包含last_seq与interrupted。前端按run_id+seq排序去重、遇缺口补历史；重新建立连接时显式携带游标，收到终止通知后重新查询持久化快照。

运行记录仍以events.jsonl为事实来源，单写入者不变；服务按文件签名缓存最近历史，减少多个订阅对不变日志的重复解析。前端轨迹有显示数量上限，原始记录继续保留在后端。

模型适配器使用真实 SSE，通过 `model_delta` 发布正文、供应商推理内容和工具参数增量。完整 `assistant_message` 覆盖同轮增量，`run_finished` 决定本轮结束。对话内容块独立于原始事件窗口累计；详细字段与断流恢复见[工作台界面](UI_WORKSPACE.md#流式数据链路)。

## 6. 验证与限制

```powershell
uv --cache-dir .uv-cache run pytest -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

后端 Web 测试集中在 `tests/web/`：`test_api.py` 覆盖后台跨请求执行、单槽并发、网页取消、停服、SSE 补发/断流/终态竞争、独立验收、历史恢复、工作区及产物边界；`test_cli_shutdown.py` 使用真实子进程、开放 SSE 和 SIGINT 验证停服。核心执行行为仍在 `tests/test_*.py` 中测试。

前端测试就近放在 `frontend/src/` 的对应模块旁，覆盖事件合并、缺口补齐、StrictMode 订阅清理、换任务竞态、状态展示及表单失败保留。可单独用 `pytest tests/web -q` 检查 Web 接入；完整命令如上。

需要复现离线浏览器验证时，可以显式启动测试辅助服务：

```powershell
uv --cache-dir .uv-cache run python tests/helpers/serve_web_fixture.py
```

该服务位于8766，仅使用脚本化模型，仍执行真实工具和验收，记录写入.runtime/browser-runs；它不证明真实模型能力。普通serve不启用这个替身。多轮页面的专用离线服务为 `uv --cache-dir .uv-cache run python tests/helpers/serve_conversation_fixture.py`，默认18771；预设files/code分别验证普通文件与Git项目，首轮输入 `first`、追加输入 `second`，记录保存在 `.runtime/conversation-browser/runs`。真实模型从页面运行会使用后端环境密钥并消耗额度，结果按实际记录核对。

当前为本地单用户、受控跨项目并发运行控制台，见[调度协议](SCHEDULING.md)。CSV/目录清单样例、demo 入口及后续恢复/容器/自优化的阶段安排统一见 [项目概览](../PROJECT.md)。缓存、依赖和本地输出保留原路径，只由 `.vscode/settings.json` 在编辑器文件树隐藏；`.runs/` 与 `.runtime/` 不因整理而删除或迁移。


文件收集在Runner唯一终态之前完成，使用artifact_collection_started、artifact_registered、artifacts_collected事件；收尾阶段展示“整理交付文件”并继续占用所属槽位及资源。RunResult.artifact_status为pending/complete/incomplete，artifact_warnings为采集受限的受控说明。命令和文件工具生成的本轮新增、修改文件自动进入产物列表，失败或取消后的实际成果仍保留；既有未修改文件、依赖缓存不列为新产物，删除仅在差异中查看。CLI产物也从该轮固定快照下载，读取不回退当前目录。

## 7. 已保留的验证证据

以下按日期保留历史验收事实。旧记录中的演示入口、细粒度网页配置及旧执行流程不代表当前功能；当前行为以本文前六节为准。

### 2026-10-19：基础控制台验收

以下为目录整理前已完成的验收记录。后端 **334 项通过、6 项平台跳过**；前端 **22 项通过**，类型检查与构建通过。跳过项涉及 Windows 符号链接权限及 POSIX FIFO。真实页面日志任务3634a2cf-247d-41e5-9fda-d2a55e255f03，6轮模型、8次HTTP尝试、7次工具调用、47条事件、8项产物，最终completed/passed。页面配置覆盖10轮已进入清单；浏览器展示47个唯一事件序号，与磁盘相同。

离线浏览器任务5e7b63ca-5e51-4620-9612-3ca766d635a6通过固定日志验收；f7d626bb-2871-4f53-8bb3-d712a89cff52生成并预览普通note.txt且保持unverified；7c4ae3ec-d357-47c6-a7da-e55d6988f599在刷新/断网后继续真实阻塞命令，由页面确认取消，记录api_user、cleanup=complete和唯一终态。离线记录位于.runtime/browser-runs，真实模型记录位于.runs。

桌面和390像素窄屏截图保存于output/playwright；该目录为本地验证输出，不加入版本控制。页面无效路径提交、表单保留和下载也已验证。对应 [前端验收要求](FRONTEND_REQUIREMENTS.md#5-首批验收标准) 的证据如下：

| 范围 | 2026-10-19 已保留证据 |
| --- | --- |
| F01 | 真实表单日志任务调用模型并验收通过；离线普通 txt 任务保持未验收 |
| F02 | 页面事件序号唯一且与持久化一致，模型/HTTP/工具计数与 API 一致 |
| F03 | 报告与 txt 预览、页面下载，执行/验收/清理分开展示 |
| F04 | 运行中刷新、断网重连保持任务；历史不重放工具；API 测试覆盖新服务实例读取历史 |
| F05 | 页面取消真实阻塞进程，api_user、cleanup=complete、唯一终态；CLI 测试覆盖开放 SSE 停服 |
| F06 | 越界路径提前拒绝、保留输入、不新增运行；API 并发测试覆盖 busy409 与单槽 |
| F07 | 无密钥输入；接口测试覆盖受限字段、主机/同源、登记产物与路径边界 |
| F08 | 页面、HTTP、事件、记录和文档联动核对；桌面与 390 像素布局实测，无浏览器运行错误 |

### 2026-10-20：目录整理回归

Web 模块迁至 `src/agent_harness/web/`，对应测试归入 `tests/web/`。整理后后端 **334 项通过、6 项平台跳过**，前端 **22 项通过**，TypeScript 类型检查与生产构建通过。

离线真实浏览器从表单填充并提交日志任务 `90d642b1-4ee6-405e-9e55-3ff51693b946`，实际执行工具与宿主验收，最终 completed/passed，32 条事件、6 项产物，只有一个 run_finished。磁盘报告为 auth=1、database=2、timeout=3、total_errors=6，终态与页面、API 一致。记录位于 `.runtime/browser-runs/`，截图为 `output/playwright/directory-migration.png`。

此次检查验证目录迁移后的离线联动；模型使用测试脚本响应，真实模型接入证据仍为 2026-10-19 的记录。缓存、依赖和生成输出只调整编辑器显示，未迁移运行记录或样例工作区。

后续迭代在实际完成检查后追加日期、范围与结果。


### 2026-10-20：真实项目任务与代码审阅

本轮后端 **389 项通过、6 项平台跳过**；前端 **39 项通过**，TypeScript 检查及生产构建通过。项目模块测试使用临时真实 Git 仓库，接口测试使用真实 Runner、文件工具和测试进程，覆盖原目录 dirty/index 保留、固定基线、工作树导入、完整新增/删除/重命名/二进制补丁、过期快照拒删、归属改变拒删、取消、准备期间断开/停服、服务重启和并发恢复。缺少原生符号链接权限及 POSIX FIFO 的跳过不视为已验证。

离线真实浏览器记录位于 `.runtime/project-browser/runs/`：

| 任务 | 页面与实际记录 |
| --- | --- |
| `4cada733-db37-44e6-b4ff-36d2ccebbba4` | 修改源码并新增CHANGELOG，项目测试通过；页面保留、刷新、确认丢弃成功，工作树确实移除，丢弃前后下载补丁完全相同 |
| `477fcf0d-460a-4822-9673-9cfa160c3e4d` | 不修改错误实现，项目测试如实失败，相对基线无代码改动 |
| `5356ccb7-2be5-49d5-ad75-5e066d99d3db` | 真实阻塞命令已开始后由页面取消，cleanup=complete；代码快照可审阅，服务重启后仍为待审阅，页面可保留工作树 |

真实模型由正常 CLI serve 在18768提供，未使用脚本化模型。页面任务 `51c55a91-8d5a-4707-805e-e751ed16d558` 在独立Git测试仓库中修复加法错误：4轮模型、4次HTTP尝试、4次工具、30条事件，只修改 `src/calc.py`，已有测试通过，完整补丁308字节，审阅保持pending供使用者检查。基线仓库中的README未提交改动、原错误源码和已有测试均保持原样；界面下载与API补丁字节一致。运行记录在 `.runtime/project-live/runs/`。

浏览器验证包含刷新、测试失败、取消后审阅、保留、确认丢弃、补丁下载、服务重启与390像素窄屏无横向页面溢出。截图保存在 `output/playwright/project-live-review.png`、`project-live-mobile.png`、`project-review-kept.png`、`project-review-discarded.png`、`project-test-failed.png` 和 `project-cancel-review.png`。测试仓库验证工程机制及模型接入；使用者自己的项目仍需登记并确认依赖、测试命令和真实任务范围，不能据此认定产品需求已被验证。

操作和登记示例见 [代码任务与审阅](PROJECT_TASKS.md)。


### 2026-10-21：上下文管理第一阶段

最终后端 **444 项通过、9 项平台跳过**，前端 **50 项通过**，TypeScript 与生产构建通过。跳过项为 Windows 原生符号链接权限和 POSIX FIFO，不能作为已验证路径。新增回归覆盖根指令一次快照、文件边界与编码、固定 JSON UTF-8 计量、工具定义与 reasoning 字段、恰好上限、首次及工具后超限、消息配对/产物保留、配置局部合并、Git 固定基线指令、跨应用读取和受损历史降级。Windows stat/fstat 的 ctime 语义差异已修复，另有明确回归。

离线真实页面记录位于 `.runtime/context-browser/runs/`，服务仅使用脚本化模型，Runner 与文件工具为真实实现：

| 任务 | 页面操作与记录 |
| --- | --- |
| `5b03d311-d1b7-483c-99cc-5b7e63cdc392` | 配置页将整体输入改为128KiB并实际提交131072字节；加载76字节项目指令，模型确实收到规则，首轮输入2799字节 |
| `72dbf40d-9820-4254-a257-fc2658a42c9c` | 配置1KiB，首轮超限，模型/HTTP/工具均为0；页面显示实际大小和原因，刷新保持 |
| `ef23cf33-904e-42f1-9a07-a6db3680a79a` | 配置8KiB，真实write_file生成growth.txt后第二轮超限；实际1轮模型/1次工具，产物和调用结果保留 |
| `11c0c14a-6158-4762-86f1-8b5e634c0e36` | 页面将指令上限设为32字节；明确显示指令过大，0模型请求，未截断加载且无输入检查 |
| `f235a304-7b93-4569-98be-ad65fd077c5f` | 页面关闭自动加载，跳过超过限制的文件；模型消息中没有项目规则，任务正常结束 |

修改离线工作区指令为另一内容并重启服务后，页面重新打开第一条历史，仍显示原规则和2799字节输入。原文件随后恢复；历史查询没有重新读取文件或发模型请求。

真实模型检查通过正常 CLI serve 在18770运行，任务 **`d295d8d4-24bc-4022-bc57-217d42e72de1`**。页面提交公开求和小任务，自动加载193字节AGENTS.md；DeepSeek调用真实文件工具生成 `result.txt`，内容为 `sum=31` 与项目约定标记 `CONTEXT_LIVE_RULE` 两行，宿主独立检查通过。任务 **4轮模型、4次HTTP、4次工具、35条事件**，每轮输入分别4615、5681、6256、6739字节，逐条按已保存消息和工具定义重算一致。原项目指令摘要与输入文件保持原样，运行记录在 `.runtime/context-live-20261021/runs/`。这证明本次接入与格式约束链路，不代表长任务能力或压缩收益。

浏览器检查覆盖配置提交、开关、错误/超限、刷新与服务重启、390像素窄屏无横向页面溢出；JavaScript控制台0错误0警告。截图保存在 `output/playwright/context-loaded.png`、`context-first-limit.png`、`context-later-limit.png`、`context-instruction-error.png`、`context-restart.png`、`context-live.png` 与 `context-live-mobile.png`。


### 2026-10-21：多轮任务交互

最终后端 **468项通过、9项平台跳过**，前端 **57项通过**，TypeScript检查与生产构建通过。跳过项仍为Windows原生符号链接权限和POSIX FIFO。新增后端回归覆盖历史重建与摘要、普通文件快照、真实Git累计补丁及autocrlf两种配置、独立预算和测试、最新轮限制、重复/冲突/未确认请求、单槽并发、请求断开和准备期间停服。前端覆盖固定请求载荷、重试/刷新、逐轮导航、忙碌、错误保留与审阅后的可继续条件刷新。

离线浏览器通过真实页面、Runner、文件工具、Git和宿主测试验证以下场景；模型为脚本化替身，不计入真实模型能力证据。记录位于 `.runtime/conversation-browser/runs/`：

| 运行或场景 | 实际观察 |
| --- | --- |
| `b068ff9f-8fd7-4322-bccf-dd6e8e191c3b` → `a90c50db-b723-4170-870b-7b11977e9095` | 普通文件两轮，第二轮在独立副本读取并保留上一轮修改，结果均为completed/unverified；刷新/旧轮导航没有新增执行 |
| `627cce89-9d54-4823-b8b1-3a78e86a20bf` → `1462cbd6-e005-44d1-be7d-7e578c942e1c` | 代码项目两轮均通过宿主测试；第一轮kept及17条事件保持，新轮重新pending；独立工作树与累计差异可查看 |
| `900098a8-eed5-4db2-8eba-184b083ae116` | 修复后重新实测：已有可继续会话缓存时，由页面确认丢弃工作树，立即出现不可继续原因并禁用按钮，已编辑草稿保留 |
| 外部文件变化 | 第二轮文件被验收脚本暂时修改，页面追加返回workspace_snapshot_stale/409，保留third输入；运行总数仍为5，未执行模型，随后原字节恢复 |
| 服务重启 → `7e6ff53b-f868-48c8-b74b-71168b5d5697` | 重启先读回原两轮，记录总数仍为5；用户明确提交后才产生第三轮，继续保留前两轮代码，历史用户消息没有重复 |
| `29bc747e-6b97-4116-b2f2-02b065da5f14` | 页面配置1KiB后追加第四轮，继承历史使输入达到6090字节；limit_exceeded，模型/HTTP/工具均为0，既有文件保持，页面显示1024字节上限及实际大小 |

真实模型通过正常CLI serve在18772运行，使用DeepSeek环境密钥；专用Git仓库包含已有加法测试及原目录未提交文件。第一轮由页面提交修复加法，第二轮从详情追加要求，保留修复后补充int参数/返回提示、中文说明及CHANGELOG：

| 运行 | 模型轮次 / HTTP尝试 / 工具调用 / 事件 | 结果 |
| --- | --- | --- |
| `f982aa0d-a130-4d50-a201-6a1a313ea583` | 4 / 4 / 6 / 39 | completed/passed，只修改app.py |
| `d26cc34c-524b-4e38-a26f-d0738400a100` | 3 / 4 / 5 / 34 | completed/passed，继承加法修复并完成类型说明和CHANGELOG，新轮pending |

真实两轮使用不同工作树。第二轮初始历史与第一轮完整会话消息逐项一致，来源摘要一致，只有一个history_loaded；各轮分别只有一个run_finished和宿主verification_result。第二轮函数AST核对为带int类型提示的加法，已有check.py和AGENTS.md字节保持。第一轮事件、清单、补丁、审阅与源码、原目录源码/规则/未提交文件共13个路径摘要未变，源仓库HEAD与未提交状态保持。记录及核验摘要分别位于 `.runtime/conversation-live-20261021/runs/` 与 `.runtime/conversation-live-20261021/verification.json`；它验证两轮工程闭环，不代表复杂项目或长任务能力已被证明。

对应PRD验收：M01/M02由普通文件和真实模型两轮交付核对；M03由服务重启后第三轮核对；M04由API断开、幂等/单槽回归及前端重试测试覆盖；M05由页面丢弃/外部修改和受损历史回归覆盖；M06由页面第四轮零请求超限覆盖。该轮验证时尚无历史压缩；2026-10-22的后续压缩交付见下文。检查点恢复、容器和并行分支仍未实现。

截图保存在 `output/playwright/conversation-files.png`、`conversation-project.png`、`conversation-discarded.png`、`conversation-stale.png`、`conversation-restart.png`、`conversation-budget.png`、`conversation-live.png`、`conversation-live-code.png` 和 `conversation-live-mobile.png`。真实模型页面JavaScript控制台0错误0警告，390像素页面与内容宽度均为390；离线错误场景有预期409网络日志，无额外JavaScript异常。前端离线测试仍有JSDOM CSS解析提示，生产构建仍有既有大包提示，两者均未阻止测试/构建通过。


### 2026-10-21：会话工作区界面重构

本轮复用已有会话、运行和事件API，未修改后端执行、持久化、预算或验收实现。前端 **69项通过（15个文件）**，类型检查与生产构建通过。新增回归覆盖三轮原地追加、输入节点不重建、旧轮检查与最新轮发送分离、输入法/快捷键、防重、草稿、未知响应确认、新轮快照未返回时保持编辑、Markdown/复制、未确认步骤不伪装运行中及手机旧详情深链接。

浏览器使用新构建从表单发起真实Git/工具/宿主测试链路，模型为离线脚本响应，记录在 `.runtime/conversation-browser/runs/`：

| 要求 | 运行编号 | 实际模型轮次 / 工具 / 事件 | 结果 |
| --- | --- | --- | --- |
| first | `2231d7d2-10d8-4990-8bbf-8d51478578d4` | 2 / 1 / 17 | completed/passed |
| second | `ee577399-6f6d-4229-9043-cfb820314f3f` | 3 / 2 / 24 | completed/passed |
| third | `0fe164dc-77ac-42e2-a159-a32c2548a90b` | 3 / 2 / 24 | completed/passed |
| retry-fourth | `afd2d1b1-2854-4b74-8bd3-7402d01afe9d` | 3 / 2 / 24 | completed/passed |

连续发送时实际比较输入框与消息滚动节点，均为同一DOM；第三轮回看第一轮详情、刷新后草稿恢复，运行总数仍为10。第四轮通过Playwright在服务端已接纳后丢失响应，再由页面“重试确认”；两次POST的父轮URL、请求编号和完整载荷一致，运行总数仅从10变为11，最终正好4轮用户消息，期间另写的下一条草稿保留。四个独立工作树均通过宿主测试，各轮只有一个run_finished；历史用户消息严格为first/second/third/retry-fourth，原仓库错误实现和未提交local.txt保持。

第四轮在新检查器中先保留、再确认丢弃，页面立即禁止发送并保留草稿，丢弃前后下载补丁字节相同。真实阻塞进程验证使用18773的显式项目辅助服务：`12923faf-d2de-4311-bca2-a7eb08f58769` 产生command.ready后由新页头确认停止，最终cancelled、cleanup=complete，cancellation_received来源为api_user，12条事件中只有一个run_finished；消息和下一条草稿保留，记录在 `.runtime/project-browser/runs/`。

既有真实DeepSeek两轮会话用于检查长回答、Markdown、代码复制、历史详情及手机展示，本轮没有新增真实模型调用。旧记录及原目录共13个路径摘要仍与既有证据一致。1440×960桌面页面与视口等高，输入栏底部为960；390×844手机页面宽度与滚动宽度均为390、高度与输入栏底部均为844。详情内容独立滚动，实测20px内边距、748px视口、1159px代码内容可继续下滚；手机抽屉宽390，关闭后原草稿保留。修复了Ant Design 6 body-holder内容区域的滚动适配。

真实会话浏览器控制台0错误0警告；故意丢失响应的离线场景有1条预期ERR_FAILED网络日志，无额外JavaScript异常。前端测试保留JSDOM CSS与伪元素计算提示，Vite仍有单包体积提示，检查与构建均通过。模型请求仍为已有非流式模式，界面只展示真实阶段和事件，不模拟逐字生成。

截图在 `output/playwright/chat-workspace-desktop.png`、`chat-workspace-mobile.png`、`chat-mobile-review.png`、`chat-retry-confirmed.png`、`chat-review-discarded.png` 和 `chat-cancelled.png`；本轮持久化核验摘要为 `.runtime/chat-refactor-verification.json`。交互及模块说明见 [多轮任务交互第8节](MULTITURN_TASKS.md#8-会话工作区实现)。


### 2026-10-22：分层历史压缩与证据回读

本轮接入成功大输出外置、旧结果规则遮蔽、结构化工作摘要、可信祖先回读和跨轮有效摘要复用；摘要请求纳入现有模型/HTTP/用量/超时限制。页面提供策略和预算设置、前后输入大小、来源范围、摘要内容及失败记录。协议与限制见[上下文管理](CONTEXT_MANAGEMENT.md)。

最终后端完整回归483 passed、9 skipped（Windows符号链接权限及POSIX FIFO）；前端16个文件共72项测试通过，类型检查和构建通过。保留已有Vite大块打包提示，前端JSDOM测试也有已有样式模拟提示。

离线页面验收使用真实Runner、文件工具、宿主验收与快照，脚本化模型不代表真实模型能力分数：

- 首轮5bd750a7-96fd-4942-a14c-f49a5d8aeea1：9次模型调用含2次摘要、6次真实工具，宿主测试通过。
- 后续6a8def20-c19f-472c-a0c8-77d8fac30363与e6cb97de-506a-4f27-9235-ad985aedb27c连续修改通过，前轮日志指纹未变。
- 57274691-fb98-4410-864a-df5ba29a195f：真实read_context取回预览之外的MIDDLE_EVIDENCE，修改并通过宿主测试，输入节点连续且刷新证据不变。
- 最终版本974b68cb-0591-492f-b5bd-c6e9b6df8446在15,000字节预算下明确超限停止；原记录和已有代码保留。随后从页面将后续预算调整为20KiB并提交99a5550e-f870-4d50-b7cb-bf76ea7682e7，通过宿主测试：复用4个有效摘要、新增1次摘要，最终原始输入104,593字节、发送输入9,685字节。此为该次请求的大小，不是泛化压缩率或成本收益。
- 07701df9-df12-454e-94e2-c0c9030631ad：无效摘要以context_summary_invalid停止，没有提交摘要替换。
- 08eb7408-cee8-4ced-909a-e63874ec405f：页面在摘要阶段确认停止，保存cancelled与not_needed清理状态，未发送草稿保留。
- 手机390×844视口，页面scrollWidth=390、scrollHeight=844；上下文在抽屉展示。截图位于output/playwright/context-compression-*.png。

证据与逐轮计数保存在.runtime/compression-browser/verification.json，首轮基线在ui-before-followup.json。

另做一次真实DeepSeek摘要协议检查：62ff9b4f-fbe8-4507-af78-36bd8d1a2d9c，使用先前离线模型配合真实工具产生的轨迹作为输入，仅请求真实deepseek-flash摘要，不执行新工具。1次模型/1次HTTP/0次工具，结构化摘要采用，prompt_tokens=4059、completion_tokens=246。记录在.runtime/compression-live-20261022/runs，verification.json保存元信息。此检查证明真实供应商协议接通，不是完整长任务能力评测。

检查点恢复、可进化过滤规则、技能和容器仍未交付。


### 2026-10-23：普通消息接续中断任务

正式页面沿用原会话输入框发送“继续”，默认不创建检查点，不展示独立恢复面板。新运行核对可信中断前缀及当前代码，复用原多轮派生流程；未知工具结果只补充待核对标记，重新使用独立预算、运行宿主测试并等待审阅。协议见[多轮任务交互第10节](MULTITURN_TASKS.md#10-中断后直接发送继续2026-10-23)。本节记录普通消息接续首次交付时的证据；独立检查点的后续清理见下文。

最终后端全量516 passed、9 skipped（Windows符号链接权限及POSIX FIFO），耗时339.40秒；前端18个文件84项通过，类型检查与生产构建通过。保留已有Vite包体积提示和JSDOM样式模拟提示。新增回归覆盖Git与普通文件工作区、中断尾部已开始但无结果/尚未开始调用、重复请求、坏记录拒绝、默认旧接口停用及Windows换行组合；此为清理前的历史测试结果；后续移除废弃实现后另行记录验收。

浏览器通过专属18777服务和真实Git项目验证，模型为离线脚本响应，工具、持久化、上下文回读、Git和pytest均实际执行：

| 证据 | 结果 |
| --- | --- |
| 中断源 f709b1f9-7f0b-451e-b776-37c2b386c987 | 真实写入加法修复、命令计数及大输出读取后停止专属服务；原轮保留中断状态 |
| 普通输入框发送“继续” → 9d5f0940-99f0-45a8-929e-15b3ed45b958 | 第2轮，3次模型/2次工具，completed/passed；宿主pytest核对修复、说明文件以及已完成命令计数仍为1 |
| 独立结果与代码 | 新工作树、新测试与待审阅补丁；原run.json、events.jsonl哈希及源仓库README、Git状态保持 |
| 输入和刷新 | 发送前后为同一输入DOM；刷新后仍为2轮，没有POST；正式页没有恢复按钮 |
| 默认存储 | 原轮和新轮均无checkpoints目录；当前代码只在明确接续时按既有流程捕获 |
| 响应式与控制台 | 390×844视口，页面宽与滚动宽均390；专属验收页面0错误0警告 |

记录与哈希在.runtime/continuation-browser/runs及verification.json，截图为output/playwright/ordinary-continue-desktop.png和ordinary-continue-mobile.png。该验证证明中断接续的工程链路，不代表真实模型长任务能力或任意外部副作用可精确恢复。


### 2026-10-23：彻底移除独立检查点实现

删除检查点核心、恢复服务、前端恢复面板与Hook、专属浏览器夹具/测试及原设计草案，共10个专属文件。Runner不再接受封存回调，Agent和压缩器仅按本轮额度运行；上下文回读只核验普通线性父链。会话、API与页面不再提供同轮尝试、恢复来源或跨尝试累计预算；旧recovery/resumes路径均为404。

普通历史读取只清理旧普通运行中的空元数据，并核对旧普通请求的冗余轮次与线性父关系后丢弃；不会改写原记录。非空旧恢复来源、非首次执行和不一致轮次继续拒绝，没有检查点加载或恢复执行分支。上一轮真实普通会话f709b1f9-7f0b-451e-b776-37c2b386c987的两轮记录可读，旧会话文件字节保持。

最终后端全量500 passed、9 skipped（Windows符号链接权限及POSIX FIFO），耗时181.56秒；前端17个文件77项通过，类型检查及生产构建通过。数量减少来自移除废弃功能专属测试；新增11项格式读取回归。JSDOM样式模拟提示和Vite包体积提示保留，均未阻止验证通过。

新建专属18778服务及真实Git项目验证：源运行380ebf67-de8f-454d-a097-56f2eb6f5d83先实际修复文件、执行计数命令并读取证据，然后中断服务。在原输入框发送“继续”生成1d947dfb-cefa-4874-9634-da3c92e73b9e，3次模型、2次工具（read_context/write_file），completed/passed并等待代码审阅。宿主pytest确认原命令计数仍为1、加法修复和说明文件存在；原run.json、JSONL及源仓库README/Git状态保持。模型为离线脚本响应，工具、Git、持久化和pytest均实际执行。

发送前后输入DOM保持，历史详情与刷新均保留草稿；刷新0个POST，消息仍为2轮。390×844手机宽度与滚动宽度均390，浏览器0错误0警告。新旧运行均没有checkpoints目录，新记录与API均不输出废弃字段。记录与核验摘要在.runtime/continuation-browser-clean，截图为output/playwright/checkpoint-removal-desktop.png及checkpoint-removal-mobile.png。本轮证明清理后的工程链路，不代表真实模型能力评测。


### 2026-10-23：手动技能加载与逐轮快照

宿主登记 SKILL.md 集合，新任务表单和会话输入区可手动选择；续轮默认沿用最新实际选择，支持更换及明确清空。运行开始固定完整正文、来源和字节指纹，详情“上下文”查看；正文参与输入预算并受压缩保护。技能不能更改宿主工具、预算或验收；本阶段不执行附属脚本，协议见 [任务技能](SKILLS.md)。

最终后端全量 550 passed、10 skipped，耗时198.43秒；跳过项来自 Windows 符号链接权限与 POSIX FIFO。前端19个文件87项通过，类型检查和生产构建通过。前端首次并发运行时原有设置页用例撞20秒超时，独立及最终单进程全量均通过，未放宽超时时间。已有 JSDOM 样式模拟提示及 Vite 单包体积提示保留。自动化额外覆盖损坏快照、失效技能后清空续聊、hybrid摘要期间保护全文，以及仅技能前缀增减时祖先摘要的正确偏移。

页面联动使用专属18779服务、真实Git项目、文件工具与宿主pytest；模型为确定性离线响应，没有新增真实供应商调用：

| 轮次与运行 | 页面操作和真实结果 |
| --- | --- |
| 1 / bfcffb33-8e55-40ff-9900-cd1a9aa37a08 | 勾选alpha提交；实际模型收到STYLE_ALPHA_V1，交付文件包含对应标记，pytest通过 |
| 2 / e025094d-33ad-4d69-89b6-aa6cfa02ea2f | 更新专属技能源文件后，页面默认沿用alpha；实际加载V2，pytest通过；第1轮快照仍为V1 |
| 3 / 144127ad-1667-4b4c-be9e-fe404658da9a | 页面取消全部选择，配置和快照为空；交付文件为NO_SKILL，pytest通过 |
| 4 / aa6d2f00-3427-405a-98e2-fa387eb9cb34 | 页面选择beta；新文件包含STYLE_BETA，pytest通过；重启服务后历史可读 |
| 5 / 749f315a-f0fe-41ba-bc57-95a5e1fb3d82 | 暂移专属Beta文件后发送；明确failed / skill_unreadable，模型与工具均0，详情展示原因且会话可继续 |
| 6 / bdd3d513-9b37-4870-9223-448abc883fcc | 页面清空失效技能后普通发送，真实修改与pytest再次通过；发送前后输入DOM保持 |

5个成功运行各3次离线模型、2次真实工具，共15次模型输入记录；失败轮没有调用。源仓库实现、README、未提交local.txt及Git状态保持，首轮run.json、events.jsonl、summary.json的SHA保持。刷新0个POST、草稿保留，不自动续跑。390×844手机宽度与滚动宽度均390、页面高844，输入栏保持底部；技能选择与旧快照抽屉可读，关闭详情保留草稿。浏览器控制台0错误0警告。

证据位于.runtime/skills-browser/verification.json、requests.jsonl及runs/，截图为output/playwright/skills-alpha-snapshot.png、skills-load-failed.png、skills-mobile-selector.png及skills-mobile-snapshot.png。该检查证明技能加载及交付链路，不是技能质量或模型能力提升评测。


### 2026-10-24：本机项目入口与首页重构

首页以打开本机文件夹、任务输入和发送为主操作；最近项目持久保存，完整历史单独查看，技能、测试、基线和产物收在高级设置，示例作为辅助入口。Windows 原生选择器独立运行；Git 根目录走工作树，普通目录走有界副本。测试可留空，保存测试设置不会执行，后续运行使用新设置，旧轮证据保持。使用和协议见 [打开本机项目](LOCAL_PROJECTS.md)。

后端完整回归 **576 passed、10 skipped**，耗时 248.93 秒；跳过来自 Windows 符号链接权限与 POSIX FIFO。前端完整回归 **20 个文件、87 项通过**，耗时 234.50 秒；最终补充状态标记后再验首页与草稿相关 11 项通过，类型检查和最终生产构建通过。保留已有 JSDOM 样式模拟提示及 Vite 单包体积提示。

修复并覆盖了 Windows 测试命令的嵌套引号往返、同路径 Git/普通类型变化的稳定编号、目录列表读取的并发一致性、普通副本排除运行记录及任务目录，以及后台项目刷新失败时表单不卸载。原生窗口仅额外继承系统桌面路径变量，工作目录固定为临时目录，工具进程的环境限制保持原规则；已清理早期验证窗口误生成的专属 Shell 缓存。

页面联动使用专属 18780 服务及“我的 后端项目”真实 Git 仓库，模型为确定性离线响应，文件工具、Git、持久化和 pytest 实际执行，没有新增真实供应商调用：

| 场景 | 实际结果 |
| --- | --- |
| 系统选择目录 | 页面打开真实原生窗口，选择中文和空格目录后显示真实路径、master、HEAD 短提交及未提交提示；草稿保持，运行总数仍为 0 |
| 取消选择 | 实际响应为 `{cancelled:true,project:null}`；项目登记文件哈希、输入及运行总数保持，没有模型或工具调用 |
| 第一轮 `088dc69c-f5b7-423d-abe9-4c387defbfb4` | 从新首页发送，在独立工作树真实修改 app.py；2 次离线模型、1 次文件工具，completed/unverified，等待审阅 |
| 首页保存项目测试 | 显式保存现有解释器的 `-m pytest -q`，60 秒超时；出现已保存，运行总数仍为 1 |
| 第二轮 `bf09eda5-a5c3-484f-82ef-ba8b70f518d6` | 从原会话追加要求，派生另一工作树并继承修改，2 次离线模型、1 次文件工具；真实 pytest 为 1 passed，completed/passed，重新等待审阅 |
| 源码及旧记录 | 源项目 app.py、test_app.py、未提交 local.txt 和 Git 状态保持；首轮 run.json、events.jsonl 的 SHA-256 保持，更新设置不改写旧验收结论 |
| 重启、刷新、最近项目 | 空闲后仅重启专属辅助服务，最近项目及测试命令仍可读；刷新 0 个 POST，未发送草稿保持，重新打开不创建运行，运行总数仍为 2 |
| 手机与控制台 | 390×844 视口；首页内容宽与滚动宽均 390，高级设置出现系统滚动条时可用宽与滚动宽均 375，无横向溢出；0 个页面错误、0 个警告 |

证据位于 `.runtime/open-project-browser/final-verification.json` 及该目录的 `runs/`。截图为 `output/playwright/open-project-final-home.png`、`open-project-final-settings.png`、`open-project-final-passed.png`、`open-project-final-diff.png`、`open-project-final-mobile.png` 和 `open-project-final-mobile-settings.png`。P01/P03/P04/P05/P06 由上述页面流程核对；P02 的普通目录、无提交、子目录及裸仓库边界由接口和真实文件/Git 回归补充。该验证说明工程链路可运行，不代表复杂真实模型任务能力的评测。


### 2026-10-24：项目会话导航与原目录默认执行

桌面侧栏按项目组织会话，项目页与全部会话先聚合再分页；同一会话仅一条入口，从旧轮链接进入仍展示完整消息，发送指向最新轮。项目直达路由支持刷新/冷启动，草稿按项目和会话分别保存，手机使用同一项目树抽屉。异常最新轮不借用旧轮成功，来源编号、类型与路径同时匹配才归入当前项目。

本机新会话默认直接在所选目录执行；执行方式、来源路径和目录对象身份随会话固定。文件工具、命令与项目测试使用实际目录，后续消息不复制代码或应用旧补丁，允许用户手动编辑后继续。隔离模式需明确选择，仍采用原逐轮工作树/副本流程。原目录差异相对本轮开始状态，产物从固定结束快照读取；快照超限不阻止当前目录继续。详细协议见 [本机项目](LOCAL_PROJECTS.md)，产品验收为 PRD N01—N04、D01—D05。

后端最终全量 **587 passed、10 skipped**，耗时288.14秒；跳过项来自Windows符号链接权限与POSIX FIFO。新增原目录接口回归覆盖普通/Git三轮、真实命令cwd、项目测试cwd、重启、两轮间手动修改、已有Git索引/提交保留、显式隔离、目录替换拒绝、采集超限可继续、原产物不可用时受控拒绝，以及差异损坏拒绝读取。

前端最终全量 **21个文件、95项通过**，耗时214.83秒；TypeScript检查与最终生产构建通过。保留全部请求与状态断言、原用例超时上限：设置页采用一次选中粘贴与每控件一次可点击性核对，提交页按保存设置和发送基线两个独立行为拆分。另覆盖差异元数据已经固化、后台尚在收尾时等待终态再读取，避免临时409留在查询缓存中。已有JSDOM布局/样式提示及Vite包体积提示保留，最终真实浏览器控制台0错误、0警告。

真实页面联动使用专属18781/18782服务。模型为确定性离线响应；文件、Git、持久化与pytest均实际执行，没有新增真实供应商请求：

| 页面场景 | 实际结果 |
| --- | --- |
| 隔离Alpha连续会话 | 前三轮只有一个项目入口；重启后从旧首轮继续得到第4轮 `9fe84456-f002-4b37-b861-9f06ae74b615`，父轮为原第3轮，pytest通过；Beta项目独立一条 |
| 原目录Git第1轮 `a3bbaeb9-11d8-4395-a34c-8e291afd7db0` | 首页明确原目录发送，真实修改所选app.py，3次离线模型、2次文件工具，completed/passed；无工作树审阅记录 |
| 原目录Git第2轮 `9345dca8-ae02-4856-b84c-f26be7dd7704` | 沿用相同目录和已有修复，pytest通过；项目页仍只显示1条、2轮 |
| 普通目录 `33ddcdf7-b1f6-4422-9847-8dd012dabd60` | 从该目录新建入口发送，实际目录等于来源，真实app.py修改和差异可查看；未配置验收，保持unverified |
| 原目录Git重启后第3轮 `d59748a1-eb3d-4689-a6cf-7202b431fc5d` | 重启前在专用源文件手动追加说明；旧首轮入口继续指向最新第2轮，同目录执行并保留手动内容，pytest通过；差异只增加本轮说明 |
| 旧证据 | 隔离24个、原目录32个文件SHA-256保持，包括原轮事件、清单、结果、固定产物和差异；隔离源仓库状态保持，原目录索引及用户local.txt保持 |
| 草稿与浏览 | 重启、刷新、项目列表及旧轮导航保留草稿；浏览0个POST，两次重启后的明确发送各只有1个followup请求 |
| 手机/平板 | 390×844宽度/滚动宽度均390，输入底部780.33；820×1000均820，输入底部929.33；导航抽屉为预期深色，进入同一会话后草稿保留 |

浏览器普通目录使用宿主明确配置的专用工作区；本轮原生系统选择器沿用前轮页面验收，明确登记普通目录后的默认原目录行为另由真实ASGI回归覆盖。该验证说明工程链路，不作为复杂任务的模型能力评测。

证据在 `.runtime/conversation-navigation-browser/final-verification.json` 和 `.runtime/local-execution-browser/final-verification.json`。截图为 `output/playwright/conversation-nav-project.png`、`conversation-nav-restarted.png`、`local-execution-first.png`、`local-execution-restarted.png`、`local-execution-project.png`、`local-execution-files.png`、`local-execution-mobile.png` 和 `local-execution-mobile-menu.png`。

升级后需重启当前Python服务并刷新页面。不兼容旧版普通目录副本会话；新任务在所选目录执行，执行位置不从浏览器文字草稿恢复。项目Git未自动暂存、提交或推送。

### 2026-10-25：精简布局与按需详情

首页保留输入与最近会话，项目入口统一到侧栏；移除重复顶栏、英文次要标题、装饰头像和引导描述。所选路径紧邻输入，执行方式与必要风险继续展示，Git 分支和提交收进高级设置。会话普通入口默认收起详情，显式 tab/inspect 深链接直接展开；跨断点收起详情时保持消息与输入节点。设置页仅保留一次配置生效范围说明。

前端完整回归 **21 个文件、95 项通过**；最后一轮文案与消息排版调整后，受影响组件回归 **6 个文件、39 项通过**。最终类型检查、生产构建和 diff 空白检查通过。保留已有 JSDOM 样式模拟提示与 Vite 单包体积提示。本轮仅调整前端布局和展示，后端接口、数据结构与执行语义保持原契约，因此未修改后端源码；前后端联动由真实离线服务补充验收。

专属 18783 服务复用项目导航夹具，模型为确定性离线响应，文件工具、项目 pytest 和持久化实际执行。首页从 Alpha 项目新建运行 `9faf9acf-9ad5-4056-b2b0-588051010445`，再从同一输入追加 `353384b8-74c5-41a2-bc6e-7b779deba798`，两轮均 completed/passed，实际原目录保持一致。桌面详情、手机抽屉、显式上下文深链接和默认收起均通过；详情与断点切换保留同一输入节点与草稿。

会话在 320、390、768、1024、1440 像素视口下内容宽等于可用宽，底部输入均在视口内；设置页相同五档宽度无横向溢出。手机项目导航可进入同一项目；首页、高级设置、项目列表、长标题与空列表均无横向溢出。长标题、空页和选项读取503仅通过浏览器只读响应替身模拟，未改写服务端历史；刷新失败保留原输入节点与草稿，恢复后错误消失。正常链路控制台0错误、0警告；异常模拟额外产生一条预期503资源错误。

汇总证据在 `.runtime/ui-layout-verification.json`，原始日志为 `.runtime/ui-layout-frontend-tests.log`、`.runtime/ui-layout-final-tests.log`、`.runtime/ui-layout-browser-verification.log` 和 `.runtime/ui-layout-pages-verification.log`。截图位于 `output/playwright/ui-layout-*.png`，包括桌面与手机首页、会话、设置、项目、导航及详情抽屉，以及长标题、空列表和刷新失败状态。浏览器辅助服务使用离线模型，本轮未调用真实供应商。


### 2026-10-26：版本核验的局部编辑

新增可配置 edit_file：read_file 对预算内文件返回完整 SHA-256，模型提交原片段、新片段和预期版本；只替换唯一匹配，支持删除片段，保留其他原字节、BOM、CRLF 和权限位。读取后变化、写临时文件期间变化、匹配歧义、二进制、编码、大小和路径错误均明确拒绝；提交失败保留原文件。上限与 max_file_write_bytes 共用，超过上限的文件仍可分页但不签发编辑指纹。协议见技术设计第6节，PRD新增E01—E05。

后端完整回归615项通过、10项跳过，日志为 .runtime/edit-file-20261026/backend-accepted.log；跳过与Windows符号链接权限和POSIX FIFO有关。首次把临时目录放在本仓库内部使普通目录被Git识别为子目录，已改为有归属标记的仓库外目录 D:/Codex/Temp/AgentHarness/edit-file-20261026，完整重跑通过。前端类型检查与生产构建通过，生产包为 index-C7LT72NS.js。JSDOM仍有样式/模拟布局提示，真实浏览器控制台0错误、0警告。

真实页面验收使用独立离线服务18786，未调用真实供应商。配置页明确勾选新工具后，从项目新建会话提交首轮：0b858832-dbcf-4fda-9455-1a658a53f416。实际修改calculator.py、创建测试、执行pytest，并通过宿主项目测试。两轮之间手工追加用户注释，再从页面继续发送：ed6f7e69-8cd6-4c27-a0fd-57572698c63e；使用edit_file分别修改函数和已有测试，再次通过真实命令和宿主测试。两轮使用同一原目录，用户手工补充保留；旧轮run.json/events.jsonl/summary.json字节、固定差异和产物均保持，Git index和用户未提交的user-note.txt不变。API回归另覆盖服务重启后接续和开始事件无法持久化时不执行编辑。

桌面执行过程可展开实际old_text/new_text/expected_sha256及replacements/新sha256。旧首轮深链接显示完整两轮会话，读取不创建运行；390×844下文档宽度=滚动宽度=390，输入栏底部832，无横向溢出。截图保存在 output/playwright/edit-file-second.png 与 edit-file-mobile.png，固定事实保存在 .runtime/edit-file-20261026/browser-proof.json。专用浏览器会话与18786服务已关闭，用户其他服务保持。


前端验收覆盖21个测试文件共98项：串行全量中其余20个文件91项通过；配置页将非法目标比例与有效预算提交拆分为独立场景后，7项全部独立复验通过（67.90秒），保留原有断言和超时上限；最终TypeScript检查通过。日志分别为 .runtime/edit-file-20261026/frontend-accepted.log、frontend-settings-final.log 和 frontend-final-typecheck.log。早期默认多worker并行出现超时，已有失败记录保留，不计作通过；本次最终统计采用每个文件最近一次有效验收。没有为通过测试放宽工具行为、断言或时限。

### 2026-10-26：指定目录即执行目录

个人普通目录始终原地执行；Git预设与页面打开目录统一默认local，不再由登记方式或隐藏配置决定位置。删除copy_on_run、普通目录副本入口、续聊从文件快照派生执行副本及从审阅记录推断旧来源的兼容逻辑。文字草稿不恢复执行方式或提交基线；只有Git项目本次明确选择“创建独立 Git 工作树（更换目录）”才改变位置。示例仍由明确入口创建首轮副本，同一会话后续复用该目录；逐轮日志、差异和固定产物独立保存。

最终后端全量617项通过、10项因Windows链接权限或POSIX能力跳过（319.30秒）；前端21个测试文件99项全部通过（130.46秒，两个工作进程），TypeScript检查及生产构建通过。日志保存在.runtime/directory-fix-tests/backend-full.txt、frontend-final.txt。配置页联动夹具明确选择个人目录，不再把固定示例伪装为个人项目；既有测试断言和时限未放宽。

真实浏览器使用18787离线夹具，文件工具、命令和项目测试实际执行。普通目录首轮6af0b960-0ff0-4e9b-a118-fb8eab5bed5a、续轮d5fae26f-9625-428c-88d2-71dfbacec5ed始终使用同一所选目录，两轮pytest命令退出0；Git项目a983a62e-705d-4515-bf38-74a604a93ba5同样原地执行，宿主项目测试通过。未创建副本根目录，首轮固定产物不随续轮变化，Git索引保留原始版本。浏览器控制台0错误、0警告。截图为output/playwright/directory-fix-ordinary.png与directory-fix-git.png，固定证据为.runtime/directory-fix-tests/browser-proof.json。未调用真实模型供应商。

### 2026-10-26：真实源码修复、小功能与重启接续

通过正常 CLI serve 与真实 DeepSeek 模型，从页面在明确选择的本仓库源码副本完成递归 glob 修复、ignore_case 小功能和重启后的 Unicode 回归。最终宿主检查分别为124/134/142项通过，每次3项平台跳过；最后142项包含17项事先固定的独立检查。模型自测与宿主检查分别记录。

一个会话保留七次运行：三次completed/passed、两次达到轮次上限、两次model_transport；四条人工接续均保留。服务重启后原六个运行编号、代码与项目测试设置保持，刷新不新建任务，第七轮随后通过。旧结果、补丁及32个历史产物下载与原字节一致；原产品源码在报告同步前178个记录文件的指纹全部未变。取消、原目录执行及中断接续的现有离线回归18项通过，前端类型检查与构建通过。

真实任务、完整运行ID、用量不完整情况、环境准备失误、人工干预、截图和副本补丁见 [真实代码任务验收](REAL_TASK_ACCEPTANCE.md)。这是一份自项目的指导完成记录，尚未采纳副本源码修改或验证无人干预成功率，后续按具体问题提交改进Plan。

### 2026-10-27：模型命令与宿主验收共用当轮源码

新增当轮 Python 执行快照，按实际工作区固定解释器及源码目录；普通命令、子目录命令和宿主项目测试共享源码范围，隔离续轮根据新目录重新解析。无验收项目同样适用，验收私有依赖不提供给模型。页面概况显示该轮固定值，历史读取与服务重启不重算。

最终后端完整回归 638 项通过、10 项平台跳过（319.95 秒）。前端 21 个文件共 99 项按每个文件最近一次有效验收统计通过：首次并行全量出现 6 项超时，涉及的 2 个文件随后独立复验 19 项通过；没有调整断言或时限。类型检查及生产构建通过。

真实页面在 18792 专属服务提交 f8a64b52-f087-4515-bab6-5443dc8c10a7，调用 DeepSeek 与真实命令，最终 completed/passed。模型输出的包路径位于所选源码副本，普通 pytest 与宿主检查各 125 项通过、3 项平台跳过；没有手工导入覆盖参数，没有修改副本源码。2 次模型轮次、3 次 HTTP 尝试、2 次工具调用。重启后保存的运行和快照保持；浏览器控制台 0 错误、0 警告。

配置含义、原始日志、事实与截图见 [Python 源码导入及本轮验收](PYTHON_EXECUTION.md#5-本轮验收2026-10-27)。这项修复已进入原产品源码；前次真实任务产生的搜索补丁仍保留在验收副本，前次失败及人工干预记录保持。

### 2026-10-28：递归搜索、项目 Python 配置与会话工作树（10月27日起实施）

原产品修复 `**/` 零级目录匹配，搜索可覆盖根目录和嵌套文件；仅采纳对应修复与回归，副本中的其他候选不自动加入。项目高级设置提供 Python 解释器，空白按 PATH 查找；模型命令和裸 Python 验收共用选择，CLI 支持 --python-executable。普通目录也可保存，并显示确认；保存不执行程序。

Git 隔离改为一个会话一个工作树，prepare_turn 只登记新轮证据，不创建目录或应用旧补丁。ProjectWorkspace 管理当前状态，ProjectReview 与历史文件按轮固定；仅最新轮可以保留/丢弃。新轮未启动即失败时回退管理状态，保留共享代码。已知会话丢弃不冒充目录异常丢失，意外丢失和归属变化仍核验。

最终后端完整回归 **652 passed、10 skipped**，340.22秒；跳过来自 Windows 符号链接权限及 POSIX FIFO。前端最终 **21个文件、101项通过**，228.71秒；最终 TypeScript 检查、生产构建通过，保留既有 JSDOM 样式模拟和 Vite 大包提示。历史误报保留先失败再修复的回归记录，没有放宽旧轮删除和目录归属检查。

真实页面使用专属18794离线模型服务，模型无供应商调用，文件、Git、命令和 unittest 实际执行：

| 轮次 | 运行ID | 实际结果 |
| --- | --- | --- |
| 1 | 9f6f9c95-f9c9-483f-b2c2-36c9da8c4118 | 页面保存独立解释器和测试后仍0运行；明确选择Git隔离提交。搜索包含main.py，命令使用所选虚拟环境，自测与宿主1项unittest均通过；随后页面保留 |
| 2 | 69340417-c3af-47c9-9273-7771b8c83b3b | 原会话追加，目录身份保持、无需新工作树；重新测试和待审阅，首轮三个核心记录、补丁、产物SHA保持 |
| 3 | 9edde332-3cf1-4084-9ea4-75e013d55313 | 空闲后重启服务，原2轮和设置保持；工作树手工新增文件随后保留，第三轮再次通过，3轮始终同目录 |

合计15次离线模型轮次、12次真实工具调用。来源验收仓库的文件、未提交README与Git索引保持。旧轮代码页隐藏管理按钮，补丁仍可下载；最新轮明确丢弃后，目录及Git注册移除，三轮固定补丁和源码产物均保持原SHA，会话停止接续。使用最终后端重新打开首轮预览，内容仍为 ROUND=1，未混入第三轮文件，旧轮没有误报警。

390×844手机视口文档宽390，无横向溢出，旧轮产物可读。主动停服期间有1次预期连接拒绝；最终新浏览器0错误0警告。验证脚本首次全页匹配误命中隐藏工具记录，后限定实际预览抽屉；断点切换收起详情后按移动端入口重新打开核对，没有修改产品行为或验收标准来通过检查。

证据：[最终后端](../.runtime/execution-workspace-20261027/backend-final.txt)、[最终前端](../.runtime/execution-workspace-20261027/frontend-final.txt)、[类型检查](../.runtime/execution-workspace-20261027/typecheck-final.txt)、[构建](../.runtime/execution-workspace-20261027/build-final.txt)、[页面固定事实](../.runtime/execution-workspace-20261027/browser/proof.json)、[误报警复现](../.runtime/execution-workspace-20261027/history-warning-red.txt)。截图为 [设置](../output/playwright/execution-workspace-settings.png)、[历史只读](../output/playwright/execution-workspace-history.png)、[第三轮](../output/playwright/execution-workspace-third.png)、[丢弃后预览](../output/playwright/execution-workspace-discarded-preview.png) 和 [手机预览](../output/playwright/execution-workspace-mobile.png)。本轮证明功能与执行证据一致性，不代表真实模型无人干预成功率或效率收益。

验收完成后专属浏览器及18794辅助服务已关闭，来源目录、运行记录和截图保留；用户原有服务未调整。

### 2026-10-28：面向个人与小团队的首批功能精简

删除日志演示产品入口、默认演示目录和验收器登记、示例副本创建与续聊、CLI 准备模式，以及磁盘旧字段清洗、缺失历史前缀推断和浏览器旧草稿迁移。CLI 日志教材、测试素材与通用独立验收继续使用。Web 配置统一使用 `worktrees_root`，普通目录始终原地执行，隔离模式只接受 Git 项目。

普通设置页仅开放模型、五类工具、模型轮次、工具次数和总时限；上下文、重试、输出和清理参数由宿主 TOML 管理。修改三项预算时，实际请求不携带内部参数覆盖，后端仍保存完整有效配置。本轮沿用现有压缩、逐轮快照和工作树审阅机制。

验收结果：

- 后端全套 **633 passed、10 skipped**，351.77秒；跳过来自 Windows 符号链接权限及 POSIX 能力。全套收集后新增的默认配置无目录副作用回归单独运行，**1 passed**。
- 前端最终 **21个文件、99项通过**，110.51秒，使用两个工作进程；TypeScript 检查、生产构建和差异空白检查通过。首次全并发有1项现有页面测试超过5秒，保持断言及时限不变，单独复查和双工作进程全套均通过。既有 JSDOM 样式模拟和 Vite 大包提示保留。
- 本次页面验收使用独立18806离线服务，模型不访问供应商；文件、Git、搜索、Python 命令和 unittest 真实执行。页面保存解释器及测试设置，应用8轮/8次/120秒预算后提交，核对内部限制与上下文配置保持宿主值。

| 用户轮次 | 运行ID | 结果 |
| --- | --- | --- |
| 1 | 5017b395-76e2-4801-ae10-a3f877dd810a | 原目录修复加法，5次模型轮次、4次工具调用，项目测试通过；页面显示真实测试与代码差异 |
| 2 | ff216279-74a9-4758-baa5-23e4af308781 | 同一会话和目录继续修改，独立测试通过，首轮记录保持 |
| 3 | 0ce9c6d7-946d-4131-b0f5-ec2c20d480af | 重启服务、刷新原会话后继续，测试通过；3轮固定产物分别保存 ROUND=1/2/3 |

共15次离线模型轮次、12次工具调用；刷新及重启读取没有额外模型调用，未创建工作树或文件执行副本。第三轮完成后首轮 run.json、events.jsonl、summary.json 的 SHA-256 全部保持，实际页面首轮文件预览仍为 ROUND=1。专属项目与记录位于 `D:/Codex/Temp/harness-pruning-browser-20261028`。

证据：[页面事实核对](../output/playwright/pruning-20261028/checks.json)、[设置页](../output/playwright/pruning-20261028/settings.png)、[首轮代码](../output/playwright/pruning-20261028/first-code.png)、[重启后第三轮](../output/playwright/pruning-20261028/restarted-third.png)、[第三轮后首轮产物](../output/playwright/pruning-20261028/first-artifact-after-third.png)。本轮不用于评估真实模型成功率或效率提升。

### 2026-10-28：局部编辑的换行可靠性与失败原因展示

统一 LF/CRLF 文件中的 old_text、新增多行 new_text 均按原文件格式处理，其他字符仍精确匹配；混合换行、仅 CR 和无换行文件维持原片段语义。独立 CR、歧义、无变化和指纹冲突在提交前拒绝。原BOM、未匹配字节、权限及末尾无换行保持；完整替换省略BOM也保留原编码标记。read_file 在原有有界指纹扫描中同时报告完整 newline，超过编辑预算时与 sha256 一起为 null。没有新增用户配置项或编辑旁路。

展开会话执行过程即可看到局部编辑的真实失败说明，完整轨迹摘要同步展示；原始参数及结果按需展开，均纯文本转义。成功与失败来自工具事件，项目测试与单项工具结果分开展示。

验证：

- 后端完整回归 **657 passed、10 skipped**，336.39秒；跳过为 Windows 符号链接权限与 POSIX FIFO。
- 前端 **22个测试文件、102项通过**，106.96秒；TypeScript、生产构建、Python语法与差异空白检查通过。保留既有JSDOM样式模拟和Vite大包提示。
- 修改前用相同输入复现了5项换行失败：跨LF/CRLF的多行匹配、带BOM匹配以及单行命中后插入LF制造混合换行。新增64KiB块边界样例首次因默认用例名过长触发Windows临时路径限制，改用短编号后保持原输入和断言验收。
- 回归覆盖完整文件换行分类、CRLF跨64KiB块、未知类型预算边界、BOM、无末尾换行、单行插入、混合换行精确编辑、大小写/空白不容错、重复/重叠片段、等价后无变化，以及原有外部改动和提交前二次指纹核验。

页面使用独立18807离线服务。模型故意用LF片段操作CRLF文件，文件、Git、pytest、命令和原目录快照均实际执行：

| 用户轮次 | 运行ID | 结果 |
| --- | --- | --- |
| 1 | 1aaa1665-c1e7-4205-a293-4c60438b6f90 | LF多行片段成功修复CRLF加法函数；结果逐字节等于预期，1项项目测试通过 |
| 2 | a2d5cbcc-642c-4a7d-9d7a-26a2daf108ca | 单行旧片段插入LF多行空值检查，实际文件仍统一CRLF；2项测试通过，页面展示真实差异 |
| 3 | 6bfff651-c8de-47c6-9f32-287b153a868f | 重启、刷新后模拟过期指纹；edit_file以file_changed拒绝，页面直接展示原因；原代码及测试文件SHA-256保持，原代码的2项测试仍通过 |

共14次离线模型轮次、11次工具调用，供应商HTTP尝试为0。重启读取未创建额外运行，两轮核心记录SHA保持；第三轮未产生代码差异，首轮固定产物与差异仍与第一轮结束时一致。此验收证明功能链路，不用于推算真实模型成功率或调用成本收益。

证据：[后端完整回归](../.runtime/newline-edit-tests/backend-full.txt)、[前端完整回归](../.runtime/newline-edit-tests/frontend-full.txt)、[页面事实核对](../output/playwright/newline-edit-20261028/proof.json)、[第二轮差异](../output/playwright/newline-edit-20261028/second-code.png)、[冲突原因](../output/playwright/newline-edit-20261028/conflict-message.png)。专属项目和运行记录保留于 `D:/Codex/Temp/harness-newline-edit-browser-20261028`。

### 2026-10-28：自动收集本轮文件，删除手工预期产物

CLI、原目录及Git隔离运行统一按本轮开始/结束文件证据登记新增、修改文件，包括命令生成的文件；未修改的旧文件、依赖缓存及运行记录不列入，删除只保留在差异。Git隔离复用已有结束快照，逐轮产物与累计代码审阅分别按各自基线计算。Runner在唯一终态之前固定证据，失败、超时或取消已经产生的文件同样可以保存。

删除前端“预期产物”、请求与任务的expected_outputs、结果missing_artifacts及CLI --expected-output。artifact_status与artifact_warnings独立描述收集状态；超限不改写执行、测试和清理结论，没有可信索引时也不宣称没有文件变化。所有工作区下载读取固定快照，移除当前目录下载回退。

验收结果：

- 后端全量 **658 passed、10 skipped**，312.63秒；跳过来自Windows链接权限及POSIX能力。全量收集后新增的CLI回归另行通过；最后收紧下载路径后，CLI/逐轮收集/Web下载/原目录的受影响回归 **46 passed、1 skipped**，31.19秒，已包含该新增CLI用例。
- 前端全量 **22个文件、103项通过**，93.54秒；最终补充空索引的错误提示后，产物面板 **3项通过**。TypeScript检查、生产构建通过；保留既有JSDOM样式模拟与Vite包体积提示。
- 页面使用专属18808离线服务，真实执行命令、Git、项目检查与文件采集。只在夹具中将快照预算缩小到4096字节，产品默认预算保持。三轮隔离任务共用一个工作树，重启和刷新读取没有新增运行；普通目录保持原地执行。

| 场景 | 运行ID | 页面与证据结果 |
| --- | --- | --- |
| 隔离首轮 | 0c54539c-7521-4103-8e6f-6aa83741b76f | 自动收集app.py、report.json、image.bin，项目检查通过；USER.txt与缓存不列为新产物 |
| 隔离续轮 | 401de4f9-4af5-4b27-a02a-83497bbce3bc | 自动收集三个修改文件，检查通过；首轮文本及二进制下载仍为原字节 |
| 重启后第三轮 | 4eafc748-e412-457a-8a56-5bf93ad8328f | 只收集extra.txt，检查通过；累计代码审阅仍显示四个改动文件 |
| 普通目录采集超限 | 2df81a64-ad83-4d40-8b2c-00f45394b0d4 | 4097字节文件真实保留在目录；completed/unverified、收集incomplete，页面明确告警并保留stdout/stderr下载 |

共8次离线模型轮次、4次工具调用，供应商HTTP尝试为0。前两轮43个固定证据文件的SHA-256、来源仓库代码、用户未提交文件和Git索引保持；四轮摘要与API结果一致，artifacts_collected均在唯一run_finished之前，所有登记文件均可下载。第三轮后首轮页面仍显示round=1；390×844手机预览可用，页面宽与滚动宽均390。主动停服期间有1次预期连接拒绝，最终普通目录页面控制台0错误、0警告。初次下载核对将Windows文本换行误写成LF预期，已按命令实际写出的CRLF字节重新核对；没有为验收改变产品文件行为。

证据：[后端全量](../.runtime/auto-artifacts-tests/backend-full.txt)、[最终下载回归](../.runtime/auto-artifacts-tests/download-final.txt)、[前端全量](../.runtime/auto-artifacts-tests/frontend-full.txt)、[最终产物面板](../.runtime/auto-artifacts-tests/panel-final.txt)、[页面事实核对](../output/playwright/auto-artifacts-20261028/proof.json)、[第三轮产物](../output/playwright/auto-artifacts-20261028/third-artifacts.png)、[历史预览](../output/playwright/auto-artifacts-20261028/first-after-third.png)、[手机历史预览](../output/playwright/auto-artifacts-20261028/mobile-history.png)、[采集不完整](../output/playwright/auto-artifacts-20261028/incomplete.png)。专属浏览器与18808服务已关闭，项目及记录保留于 `D:/Codex/Temp/harness-auto-artifacts-browser-20261028`；该验收说明工程链路，不用于推算真实模型任务成功率或成本收益。

### 2026-10-29：公开项目测试失败后的自动修复

获批目标为公开项目测试失败后的完整修复闭环：默认关闭、最多两次修复，在同一 Run 中共享消息、压缩状态、工作区和模型/工具/时间总预算。独立检查不回传，错误、超时、取消或清理异常不触发修复。前端和 CLI 同步接入；逐次失败输出保存，最终文件只固化一次。

后端完整回归 678 passed、10 skipped，433.23秒。跳过为 Windows 符号链接权限和 POSIX FIFO；第一次完整回归的两项取消阶段断言失败后，已修复为收尾前固定取消阶段，原断言保持，相关26项复验通过。前端完整23文件112项通过；取消后文案改为当前代码尚未取得新检查结果，组件追加停止状态后3项复验通过。并行界面修改保留后，相关后端30项、前端4文件18项复验通过；17:58的合并源码类型检查与生产构建通过，构建期间源码哈希保持。既有Vite大包提示保留。并行界面任务仍在迭代，其后续改动由该任务继续做整体验收。

独立18809服务使用离线模型，实际执行文件工具、Git、Python测试与快照；供应商HTTP尝试为0。运行记录保留于D:/Codex/Temp/harness-auto-repair-browser-20261029。

| 用户轮次 | 运行ID | 事实 |
| --- | --- | --- |
| 1 | 14f9c28e-43cf-4443-8e38-d658ddf9e4ee | 开启后首次失败，一次修复通过；4次模型、2次工具 |
| 2 | 6040f7ae-a302-479f-a3aa-8c941f0dcdb4 | 重启后继承开启，三次检查失败/失败/通过；6次模型、3次工具 |
| 3 | 31e62d54-2e47-4237-a483-ba338391ecb6 | 页面关闭修复，只检查一次且失败；2次模型、1次工具 |
| 4 | d66d7f2b-00b8-4235-af6b-80bc83af94ff | 修复中环境中断，无终态；重启只读显示中断，保留首次失败和流，不自动续跑 |
| 5 | ed69ac8a-56c3-45e7-beac-47076961ba5f | 页面明确继续，在修复中主动停止；api_user取消，当前未验收，首次失败仍可下载 |

五轮共18次离线模型请求、8次工具调用、8次真实检查。16份检查原始输出下载字节与磁盘一致；首轮17个固定证据SHA-256保持，源码索引及原有USER.txt保持。四个正常收尾运行的summary与API一致，且artifacts_collected在唯一run_finished前；第四轮中断没有伪造终态。390×844手机页面宽与滚动宽均390，首轮代码预览仍为ROUND=1；最终浏览器控制台0错误、0警告。

验收操作曾被自动审批服务的账户用量限制拦截，未执行点击；恢复后另起第五轮完成页面主动取消。一次只读验证脚本把interrupted误当作execution_status，已按真实RunView.interrupted契约修正，未修改产品状态或放宽断言。结论仅说明工程功能链路，不用于推算真实模型的成功率和收益。
证据：[后端全量](../.runtime/auto-repair-tests/backend-final.txt)、[前端全量](../.runtime/auto-repair-tests/frontend-full.txt)、[合并后端复验](../.runtime/auto-repair-tests/integrated-final.txt)、[合并前端复验](../.runtime/auto-repair-tests/frontend-integrated.txt)、[类型检查](../.runtime/auto-repair-tests/typecheck-final.txt)、[构建](../.runtime/auto-repair-tests/build-final.txt)、[构建期间源码稳定性](../.runtime/auto-repair-tests/build-consistency.json)、[页面事实与下载哈希](../output/playwright/auto-repair-20261029/proof.json)。截图：[两次修复历史](../output/playwright/auto-repair-20261029/second-history.png)、[重启读取中断记录](../output/playwright/auto-repair-20261029/restarted-interrupted.png)、[合并界面取消结果](../output/playwright/auto-repair-20261029/integrated-cancelled.png)、[手机历史检查](../output/playwright/auto-repair-20261029/mobile-history.png)、[手机首轮产物](../output/playwright/auto-repair-20261029/mobile-first-artifact.png)。

本次专属18809服务和浏览器已关闭，代码、运行记录及截图保留。按用户要求未暂存、提交或推送；原有自动产物收集与并行界面改动均保留。


## 2026-10-29 工作台精简与界面统一

按获批 Plan 完成一轮前后端同步调整。删除已被会话列表取代的 `getRuns`、`GET /api/runs` 和 `RunManager.list_runs`，以及 `/tasks`、`/tasks/{id}` 页面入口；`POST /api/runs` 与逐轮详情、事件、产物接口继续使用当前协议。`GET /api/options` 只提供 `recent_projects` 作为项目列表，删除重复的 `projects/workspaces` 响应、前端拼装回退与两套选项类型。宿主 TOML 的项目和普通目录登记仍各自承担配置职责。

长期 Python/公开测试配置迁入项目页“项目设置”，任务草稿在跨页往返时保留；提交表单只收集当次执行方式、基线、技能和自动修复等选择，没有宿主验收器可选时不展示空下拉框。详情统一为结果、文件、诊断：当前及历次检查保留完整证据；本地与隔离共用文件清单、补丁下载和分文件差异组件，增删行与两侧行号可读，二进制与重命名仍以结构化清单为准。上下文、配置与原始事件收进诊断，切换轮次或分类复位详情滚动，事件刷新保持位置。

以同一份暖灰/橙色设计变量重写全站样式，覆盖新建、项目、全部会话、持续对话、设置、详情、预览及手机抽屉，删除旧布局选择器与重复覆盖。修正当前 Ant Design 标签内容容器的边距与滚动、发送按钮宽度和输入提示对比度；桌面和390px手机无整页横向溢出。移除退役列表的缓存刷新；旧清单专属测试改为当前格式准备中断不补造指令的有效边界测试，不增加版本兼容实现。原有自动修复与产物收集改动均保留。

### 验收结果

- 后端相关回归：`tests/web`、自动修复与产物收集核心共164项通过、1项Windows符号链接权限跳过；最后当前导航契约/准备中断两项补验通过。临时项目置于仓库外的D盘目录，避免“普通目录”被祖先Git仓库识别为子目录。
- 前端默认 `npm.cmd --prefix frontend test`：25文件、116项通过；最后详情/状态14项及轨迹2项补验通过。默认测试并行上限设为3，保留原交互超时与断言。
- 类型检查和生产构建通过。Vite仍提示主包超过500kB；本轮未新增运行依赖。
- 18819专属离线服务完成4次真实执行：原目录两轮修改并通过项目测试、第三轮由页面取消；另一个隔离会话完成修复与测试，并由页面保留工作树。隔离请求明确携带自动修复开关。历史列表只有2个会话，分别1轮与3轮，最新取消没有覆盖首轮测试结果。
- 首轮产物在第二轮覆盖同名文件后仍返回首轮字节；原目录模式没有保留/丢弃操作，隔离模式保留审阅入口。手机详情抽屉宽390px，页面宽390px。文件区滚动至88px后切换诊断，滚动归零；浏览器错误与警告为0。

本次使用离线模型替身，目录选择器返回专属测试目录；文件工具、Git、Python项目测试与快照访问真实执行，不评估真实模型成功率或成本。原生系统目录弹窗未在本轮重新操作。

证据：[页面与固定事实](../output/playwright/ui-refresh-20261029/proof.json)、[后端相关回归](../.runtime/ui-refresh/backend-final.txt)、[当前契约补验](../.runtime/ui-refresh/format-contract-final.txt)、[前端默认回归](../.runtime/ui-refresh/frontend-default-final.txt)、[最终布局补验](../.runtime/ui-refresh/final-layout-tests.txt)、[构建](../.runtime/ui-refresh/build-final.txt)、[类型检查](../.runtime/ui-refresh/typecheck-final.txt)。

| 步骤 | 检查与结果 | 截图 |
| --- | --- | --- |
| 1 | 新建输入、项目设置与草稿往返通过 | [新建](../output/playwright/ui-refresh-20261029/01-start-desktop.png)、[项目设置](../output/playwright/ui-refresh-20261029/02-project-settings.png) |
| 2 | 测试结果、旧轮回看与固定输入通过 | [会话结果](../output/playwright/ui-refresh-20261029/03-conversation-result.png) |
| 3 | 原目录分文件差异与固定产物预览通过 | [文件差异](../output/playwright/ui-refresh-20261029/04-files-diff.png)、[源码预览](../output/playwright/ui-refresh-20261029/05-artifact-preview.png) |
| 4 | 诊断收纳、事件与滚动复位通过 | [诊断](../output/playwright/ui-refresh-20261029/06-diagnostics.png) |
| 5 | 390px会话和详情抽屉通过 | [手机会话](../output/playwright/ui-refresh-20261029/07-mobile-conversation.png)、[手机详情](../output/playwright/ui-refresh-20261029/08-mobile-result.png) |
| 6 | 全局模型、工具及预算分区通过 | [桌面设置](../output/playwright/ui-refresh-20261029/09-settings-desktop.png)、[手机设置](../output/playwright/ui-refresh-20261029/10-settings-mobile.png) |
| 7 | 隔离工作树与独立审阅操作通过 | [隔离审阅](../output/playwright/ui-refresh-20261029/11-isolated-review.png) |
| 8 | 项目/全局按会话聚合，最新状态正确 | [全部会话](../output/playwright/ui-refresh-20261029/12-history-desktop.png)、[项目](../output/playwright/ui-refresh-20261029/13-project-desktop.png) |

## 2026-10-29 V0.2 当前实现可靠性复验

获批迭代一使用冻结的当前源码和178份核验一致的真实历史任务起点，从正常页面、CLI服务和真实DeepSeek完成修复、小功能及重启后追加；六轮三次完成并通过项目检查，两次达到轮次上限、一次model_transport及三条人工接续均保留。最终外部独立检查120项通过、3项平台跳过；公开测试与外部检查分开记录，独立结果不反馈模型。真实任务均未触发宿主自动修复，不将其记为自动修复成功率。

第三轮后重启保留1126份运行证据，最终55份历史下载与原字节保持。额外离线页面链路实际运行Git、文件与Python测试，覆盖原目录一次修复、隔离两次修复、保留后同工作树续轮、修复中确认取消、丢弃后的历史下载及手机预览；20份离线下载保持，来源文件和索引未受隔离操作影响。第一次只展开停止确认框的自然完成另行保留，随后一轮完成真正的确认取消。

本轮后端全量679通过、10项平台跳过；最终默认前端全量25文件116项通过，类型检查和生产构建通过。首次并行全量的一项前端路由超时，经原断言/时限的独立复查和默认全套复跑通过；沙箱目录/子进程限制的初次失败也保留。桌面、390px详情及历史预览核对通过，两个浏览器错误/警告记录均为零。

本轮没有发现需要改产品代码的新程序缺陷，前后端实现与契约保持；V0.2 既定首版范围验收完成，但仍是允许人工接续的指导式交付，V0.3 尚未执行。完整运行编号、源码/检查指纹、用量、已知问题和原始证据入口见 [可靠性复验报告](RELIABILITY_ACCEPTANCE_20261029.md)。

## 2026-10-30 Agent主动规划与人机决策

本轮获批实现C03及相关C05/C12首版，未重复V0.2验收，也未执行G02—G04候选比较。Agent可主动规划、提问、提交方案和维护步骤；网页回答、批准/修改/拒绝及取消等待，历史检查器只读。宿主required策略使自动入口从首个动作起只读，写入/编辑/命令与规划期宿主测试均无法提前执行；默认auto仍由模型判断是否需要进入规划。

真实DeepSeek专属18825服务完成只读研究、问题、网络失败后普通接续、提交版本化方案、390px刷新后批准及执行交付。原型自动入口曾在进入规划前用命令列目录，原始事实保留，随后补全宿主强制策略及只读目录工具。严格链路批准前零写入、零命令；批准后产生3份样例交付，宿主复跑21项项目测试与另4项CLI检查通过。模型回答中的示例计数曾不准确，实际程序输出与独立检查为INFO=2、WARN=1、ERROR=2，不将模型文字当作验收事实。

手机待批准页刷新后保持请求，文档宽度与滚动宽度均390；实际打开旧计划详情时没有批准按钮。真实浏览器零错误、零警告。前端全量26文件121项通过，类型检查和生产构建通过，保留既有Vite大包提示与测试环境的CSS/伪元素提示。后端最终全量及完整运行、失败和用量记录统一见[规划验收](PLANNING.md#6-本轮验收)。


### 2026-10-30：项目命名绑定与首页选择

按批准的Plan补齐项目管理：每个项目保存稳定ID、用户名称与唯一规范目录，Git和普通目录统一进入项目选择器；首页支持按名称/路径搜索已有项目，以及填写名称、选择文件夹、确认绑定的新建流程。同目录复用原ID、名称和运行设置，选择草稿及取消不写登记。项目设置可独立改名，侧栏、项目标题和会话来源同步更新；绑定目录只读。切换保留任务文字，隔离执行、基线与自动修复授权须重新选择。

前后端同步替换选择协议：POST /api/projects/pick返回临时selection，POST /api/projects确认绑定，PATCH /api/projects/{id}保存名称；删除旧的“选目录即登记”页面流程与对应断言。原生目录选择凭据有效600秒、最多100份，仅存服务内存；任务提交继续只接受登记ID。确认前核验目录仍存在且规范路径未变，目录移动/删除或链接变化返回受控错误；登记原子保存和运行证据规则保持。

验收结果：

- Web与桌面后端全套162项通过、1项Windows符号链接权限跳过（384.76秒）；最后目录失效边界修正后，项目登记完整测试文件19项通过（23.88秒），未扩大产品范围。
- 前端最终默认全套28个文件134项通过（132.26秒）；类型检查与生产构建通过。构建保留现有大包体提示，jsdom仍有样式/伪元素模拟提示，真实页面控制台为0错误、0警告。
- Electron测试5项通过，包含父子进程的原生选择、确认绑定、任务、下载与重启历史。
- 专属18831页面服务真实创建“订单服务”和“资料分析”，分别绑定Git根与普通目录；确认前登记为空，重复目录复用，取消后仍为2个项目。按名称和路径搜索、切换保留草稿、改名为“订单核心服务”、刷新和服务重启均通过。
- 两次首页发送分别在各自绑定目录写入app.py，最终value=2；共2个会话，原生选择和刷新没有额外运行。改名、重启前后两份events.jsonl的SHA-256相同。390×844页面滚动宽与视口宽均390。

本轮使用离线模型与固定原生选择结果；API、Runner、Git探测、持久登记、文件写入和历史查询真实执行，未重新人工操作系统目录窗口。专属测试资源保留于D:/Codex/Temp/harness-project-binding-browser-20261030。

证据：[绑定与重启核对](../output/playwright/project-binding-20261030/proof.json)、[桌面首页](../output/playwright/project-binding-20261030/08-final-desktop.png)、[窄屏首页](../output/playwright/project-binding-20261030/09-final-mobile.png)、[创建项目](../output/playwright/project-binding-20261030/01-create-desktop.png)、[重复目录](../output/playwright/project-binding-20261030/06-existing-folder.png)、[项目改名](../output/playwright/project-binding-20261030/07-renamed-settings.png)。可复用离线服务见tests/helpers/serve_project_binding_fixture.py。


### 2026-11-05 跨项目后台调度与安全恢复

默认双槽、资源冲突排队、Run 归属控制、单项停止、启动偏好、终态对账及未知资源局部阻塞已接入同一桌面/Web 链路。最终后端全量 943 项通过、11 项平台跳过，收尾专项 11 项通过；前端全量 210 项通过，补充后的后台页面 5 项通过；桌面父子链路 6 项通过，类型检查及构建通过。各批有重叠，不相加。双项目真实模型均通过独立检查，页面与 390px 窄屏核对通过；范围、配置及证据见[调度协议](SCHEDULING.md)。
