# 本地任务控制台

本地网页复用现有 Runner：任务 API 负责提交/查询/取消，SSE 传输已落盘事件，React 负责展示和交互。任务执行、工具和独立验收使用同一后端核心。

本文维护 Web 操作、宿主配置、HTTP/SSE 契约与集中验证证据；整体交付阶段和完整文档导航见 [项目概览](../PROJECT.md)。

## 1. 启动与开发

安装依赖、设置密钥与首次构建按 [README](../README.md#安装与密钥) 执行。构建完成后，在项目根目录启动：

```powershell
uv --cache-dir .uv-cache run harness serve --web-config web.example.toml --config harness.example.toml
```

打开 [本地任务工作台](http://127.0.0.1:8765/runs)。启动终端需已有 DEEPSEEK_API_KEY，页面只报告是否配置，不接收、保存或回显密钥。没有密钥仍可查看历史，提交时会提前拒绝。

前端依赖由 package-lock.json 锁定，后端依赖由 uv.lock 锁定。前端构建后由 Python 服务统一提供，不需要常驻第二个 Node 服务；修改前端源文件后重新 build 并刷新。--port 支持 1 至 65535，默认 8765，固定绑定 127.0.0.1，单 worker 运行。
开发时分别启动Python服务与：

```powershell
npm.cmd --prefix frontend run dev
```

Vite默认在127.0.0.1:5173提供页面，将/api代理到127.0.0.1:8765。前端只需相对API路径，生产构建沿用同源访问。使用其它开发端口时需要同步调整代理与宿主Origin规则。

## 2. 操作流程

- **普通任务**：先将输入文件放入登记工作区，填写任务、选择工作区、声明可选预期产物，再开始执行。任务产物可以是代码、文本、CSV等格式；不要求输出JSON。未选择验收器时最终明确显示“未验收”。
- **日志样例**：点击“填充日志样例”，检查任务与固定验收器后提交。表单会为本浏览器启用所需四工具。后端每次复制输入到新的独立目录，Agent生成脚本和报告后，宿主运行固定日志检查器。该验收器只适用于仓库样例。
- **运行详情**：显示阶段、耗时、模型/HTTP/工具计数与预算，轨迹按序号展示；可展开工具参数和结果，切换最终回答与产物。产物提供64KiB以内UTF-8文本预览，二进制文件可下载。
- **运行配置**：修改模型、四类工具和已有预算，仅影响本浏览器之后提交的任务；刷新页面恢复宿主默认。正在运行的配置不变，页面不修改服务端TOML。
- **取消**：明确点击并确认取消后，先显示正在取消，后端清理完成后才显示终态。刷新、页面切换、关闭网页或SSE断开只清理订阅，不取消Agent。
- **历史**：重新启动服务后可读取既有记录；读取不会重放工具动作。缺少可信结束事件的记录显示中断与警告，不推断其副作用已经完成。

执行、验收、清理状态分别展示。只有最终execution_status=completed、validation_status=passed且cleanup_status为not_needed/complete时展示成功。报告错误是验收failed；验收进程出错是error；两者都保留模型已完成的执行事实。

## 3. 宿主配置

web.example.toml采用表数组登记工作区和验收器。相对路径以该TOML所在目录解释：

```toml
runtime_root = ".runtime/web-workspaces"
frontend_dist = "frontend/dist"

[[workspaces]]
id = "workspace"
label = "本地任务工作区"
path = ".runtime/workspace"
copy_on_run = false

[[workspaces]]
id = "logs"
label = "日志样例（每次独立复制）"
path = "examples/logs"
copy_on_run = true

[[verifiers]]
id = "logs"
label = "固定日志样例验收"
description = "仅适用于仓库固定样例。"
path = "examples/verifiers/logs.toml"
```

默认的project/.runtime/workspace可按需创建，其余自定义普通目录必须事先存在。copy_on_run=true的目录复制到runtime_root下的UUID新目录，限制1000目录项和16MiB，拒绝链接与特殊文件。工作区/验收编号必须唯一。网页只能提交登记编号，不能传入任意宿主路径、验收argv或凭据。

普通工作区的历史产物沿用现有路径引用：后续任务覆盖同名文件时，历史下载读取的是该路径当前内容。历史验收结论属于当次运行，不代表文件之后未变化。日志复制样例每次独立，仍不应手动修改已交付的副本。当前未增加不可变产物快照或容器隔离。

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
| `schemas.py` | 受限请求与运行视图 |
| `service.py` | 单槽后台任务、进度、取消、历史、样例复制与产物 |
| `api.py` | HTTP/SSE、静态资源和受限产物访问 |

CLI 的 serve 只组装模块，不另建执行循环。核心目录和阅读路线见 [技术设计](TECH_DESIGN_V0.1.md#2-模块职责与调用方向)，前端阅读入口见 [前端工程说明](../frontend/README.md)。

提交预检和槽位占用在服务内串行处理。通过on_context等待Runner已创建记录后才返回202，避免“提交后立即取消”打断尚未开始的协程。任务由服务持有，生命周期独立于请求和浏览器连接。网页取消记录source=api_user，CLI信号仍记录user_sigint，停服记录service_shutdown。

停服先给HTTP/SSE请求2秒退出宽限，再由lifespan取消后台任务并等待已有进程清理。清理有单独预算，因此总退出时间不保证恰好2秒。开启多个worker、自动reload或同时从CLI操作相同工作区不属于当前调度保证。

## 5. HTTP契约

| 方法与路径 | 请求/返回含义 |
| --- | --- |
| GET /api/options | 默认运行配置、密钥是否配置、工作区/验收/代码项目预设与忙碌任务ID |
| POST /api/runs | 接收prompt、workspace_id/project_id二选一、项目base_ref或普通verifier_id、expected_outputs及config；成功202，忙碌409 |
| GET /api/runs | offset从0开始，limit为1至100，可按执行status筛选；返回items/total/busy_run_id |
| GET /api/runs/{id} | 返回统一RunView，不恢复执行 |
| POST /api/runs/{id}/cancel | 幂等请求取消，返回视图；任务可能仍在清理 |
| GET /api/runs/{id}/events | 返回seq>after的分页事件，limit为1至500；last_seq是当前磁盘有效最大序号 |
| GET /api/runs/{id}/stream | SSE补发与持续订阅，支持after和Last-Event-ID，取较大非负游标 |
| GET /api/runs/{id}/review | 读取终态项目审阅；中断遗留pending可补捕获一次，不恢复模型 |
| POST /api/runs/{id}/review | decision为keep/discard；独立保存审阅决定，不自动提交或合并 |
| GET /api/runs/{id}/diff | 有界UTF-8补丁预览，返回content/truncated/size_bytes |
| GET /api/runs/{id}/patch | 下载已核对完整性的完整补丁，丢弃后仍可访问 |
| GET /api/runs/{id}/artifacts | location+path必须匹配登记引用；preview=true返回文本预览，否则附件下载 |

RunView包含run_id、prompt、workspace、created_at、result、phase、event_count、last_seq、active、cancelling、interrupted、warnings、本次config及可空project审阅详情。result继续使用RunResult及可空verification；毫秒、字节、字符等单位沿用后端定义。所有可展示正文按文本渲染。

config覆盖只接受requested_model、tools和limits，limits按字段合并并复用RunLimits严格校验。提交配置不会改写宿主默认值。受控错误统一为{error:{code,message}}；校验错误不回显输入值、密钥或底层异常正文。

SSE逐条发送event=run_event、id=seq、data=完整RunEvent；每10秒注释心跳。终态或无活动任务的中断历史补齐已落盘事件后发送stream_end，其中包含last_seq与interrupted。前端按run_id+seq排序去重、遇缺口补历史；重新建立连接时显式携带游标，收到终止通知后重新查询持久化快照。

运行记录仍以events.jsonl为事实来源，单写入者不变；服务按文件签名缓存最近历史，减少多个订阅对不变日志的重复解析。前端轨迹有显示数量上限，原始记录继续保留在后端。

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

该服务位于8766，仅使用脚本化模型，仍执行真实工具和验收，记录写入.runtime/browser-runs；它不证明真实模型能力。普通serve不启用这个替身。真实模型从页面运行会使用后端环境密钥并消耗额度，结果按实际记录核对。

当前为本地单用户、单任务运行控制台。CSV/目录清单样例、demo 入口及后续恢复/容器/自优化的阶段安排统一见 [项目概览](../PROJECT.md)。缓存、依赖和本地输出保留原路径，只由 `.vscode/settings.json` 在编辑器文件树隐藏；`.runs/` 与 `.runtime/` 不因整理而删除或迁移。

## 7. 已保留的验证证据

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
