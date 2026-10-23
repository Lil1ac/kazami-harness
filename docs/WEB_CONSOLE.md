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
- **运行详情**：消息区展示用户要求、Markdown回答和折叠过程；右侧或手机抽屉按轮查看测试、代码、产物、上下文、完整轨迹与预算，工具参数/输出继续按纯文本展开。产物提供64KiB以内UTF-8文本预览，二进制文件可下载。
- **运行配置**：修改模型、四类工具和已有预算，仅影响本浏览器之后提交的任务；刷新页面恢复宿主默认。正在运行的配置不变，页面不修改服务端TOML。
- **取消**：明确点击并确认取消后，先显示正在取消，后端清理完成后才显示终态。刷新、页面切换、关闭网页或SSE断开只清理订阅，不取消Agent。
- **历史**：重新启动服务后可读取既有记录；读取不会重放工具动作。缺少可信结束事件的记录显示中断与警告，不推断其副作用已经完成。

执行、验收、清理状态分别展示。只有最终execution_status=completed、validation_status=passed且cleanup_status为not_needed/complete时展示成功。报告错误是验收failed；验收进程出错是error；两者都保留模型已完成的执行事实。

任务结束后在会话工作区底部输入消息继续修改。后续运行核对前轮历史与代码，派生独立工作区，再执行、验收并重新待审阅；每轮可独立查看轨迹、上下文、产物和累计差异。发送始终继续最新轮；查看旧轮只切换详情面板，运行中保留编辑但禁止新提交。快照上限、网络重试及旧记录兼容见 [多轮任务交互](MULTITURN_TASKS.md)。

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

普通工作区的历史产物沿用现有路径引用：后续任务覆盖同名文件时，历史下载读取的是该路径当前内容。历史验收结论属于当次运行，不代表文件之后未变化。日志复制样例每次独立，仍不应手动修改已交付的副本。普通工作区新增的有界快照仅用于核验与派生下一轮，不改变产物下载的路径引用语义；容器隔离尚未实现。

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
| `conversations.py` | 会话查询、追加条件、历史与工作区继承和幂等调度 |
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
| GET /api/runs/{id}/conversation | 返回所属会话的逐轮要求/回答、最新轮与可继续条件；只读 |
| POST /api/runs/{id}/followups | request_id、prompt及可选config/expected_outputs，成功202；同编号同载荷复用原运行，不重放 |
| POST /api/runs/{id}/cancel | 幂等请求取消，返回视图；任务可能仍在清理 |
| GET /api/runs/{id}/events | 返回seq>after的分页事件，limit为1至500；last_seq是当前磁盘有效最大序号 |
| GET /api/runs/{id}/stream | SSE补发与持续订阅，支持after和Last-Event-ID，取较大非负游标 |
| GET /api/runs/{id}/review | 读取终态项目审阅；中断遗留pending可补捕获一次，不恢复模型 |
| POST /api/runs/{id}/review | decision为keep/discard；独立保存审阅决定，不自动提交或合并 |
| GET /api/runs/{id}/diff | 有界UTF-8补丁预览，返回content/truncated/size_bytes |
| GET /api/runs/{id}/patch | 下载已核对完整性的完整补丁，丢弃后仍可访问 |
| GET /api/runs/{id}/artifacts | location+path必须匹配登记引用；preview=true返回文本预览，否则附件下载 |

RunView包含run_id、prompt、workspace、created_at、result、phase、event_count、last_seq、active、cancelling、interrupted、warnings、本次config、可空project审阅详情及可空context上下文详情；追加session_id、可空parent_run_id及从1开始的turn_index描述用户轮次。result继续使用RunResult及可空verification；毫秒、字节、字符等单位沿用后端定义。用户消息、工具输出、日志、指令和补丁按纯文本转义，Agent最终回答按受限Markdown渲染，不启用原始HTML。

config覆盖只接受requested_model、tools、limits和context；limits/context按字段合并，分别复用RunLimits/ContextPolicy严格校验。提交配置不会改写宿主默认值。context保存本次指令快照与最新输入检查，旧记录为null；配置、事件和大小单位见 [上下文管理](CONTEXT_MANAGEMENT.md)。受控错误统一为{error:{code,message}}；校验错误不回显输入值、密钥或底层异常正文。

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

该服务位于8766，仅使用脚本化模型，仍执行真实工具和验收，记录写入.runtime/browser-runs；它不证明真实模型能力。普通serve不启用这个替身。多轮页面的专用离线服务为 `uv --cache-dir .uv-cache run python tests/helpers/serve_conversation_fixture.py`，默认18771；预设files/code分别验证普通文件与Git项目，首轮输入 `first`、追加输入 `second`，记录保存在 `.runtime/conversation-browser/runs`。真实模型从页面运行会使用后端环境密钥并消耗额度，结果按实际记录核对。

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

正式页面沿用原会话输入框发送“继续”，默认不创建检查点，不展示独立恢复面板。新运行核对可信中断前缀及当前代码，复用原多轮派生流程；未知工具结果只补充待核对标记，重新使用独立预算、运行宿主测试并等待审阅。协议见[多轮任务交互第10节](MULTITURN_TASKS.md#10-中断后直接发送继续2026-10-23)。旧检查点实现与测试保留兼容，默认执行不启用；原方案见已暂缓的[检查点草案](CHECKPOINT_RECOVERY.md)。

最终后端全量516 passed、9 skipped（Windows符号链接权限及POSIX FIFO），耗时339.40秒；前端18个文件84项通过，类型检查与生产构建通过。保留已有Vite包体积提示和JSDOM样式模拟提示。新增回归覆盖Git与普通文件工作区、中断尾部已开始但无结果/尚未开始调用、重复请求、坏记录拒绝、默认旧接口停用及Windows换行组合；原检查点专属回归仍验证兼容协议，不代表正式页面启用了该功能。

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
