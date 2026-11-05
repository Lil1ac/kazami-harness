# 子 Agent 委派（C07）

当前包含探索/审阅只读角色及独立工作树开发角色。开发、动作审批接续及成果采纳见[写入型子 Agent](DEVELOPMENT_SUBAGENTS.md)；下文的共享目录、只读限制和默认4/8/60额度专指只读角色。

本机长任务迭代支持父子统一安全暂停及受控接续，沿用可信历史、原目录归属和各自剩余预算；只读研究与开发角色的权限边界保持分别核验。它不提供任意旧段或未知进程的自动恢复，协议与新增回归见[长任务协议](LONG_RUNNING.md)。

探索/审阅提供只读委派：同步等待与异步并行共用同一个delegate_readonly接口，通过background明确选择执行方式。异步最多两个活动子任务，父模型可以继续独立读取或思考；子执行不进入顶层单机队列，不另占执行槽。当前模型、供应商与实际工作区沿用父运行。

## 角色、权限和输入

explore和review使用独立系统职责及宿主只读集合，与父已开放能力取交集：read_file、search_files、list_files、read_context、memory_search、memory_get。只读子项不能写文件、执行命令、修改任务/记忆、提问或递归委派。父聊天及工具正文不自动传给子模型，完整子对话不回灌父模型。

2026-11-02 接入统一权限入口：子项继承父宿主策略、会话模式及目录范围，再取只读交集；父批准一个具体动作不会扩大子项能力。子报告和历史保存实际权限快照；范围外文件与自动项目指令加载均不能借委派绕过。协议与本轮回归见[权限控制](PERMISSIONS.md)。

明确objective为执行目标，父要求、批准方案和可选同会话task_id只是背景及关联，不表示子项认领或完成工作项。根AGENTS.md、记忆修订与实际阶段在接纳时固定；skill_names只能选择父已冻结的技能，默认空，不继承父角色system_prompt或全部技能。每个子项独立观察路径、选择记忆、压缩和保存日志。

当前共享目录没有文件快照隔离。因此存在活动只读子项时，宿主拒绝父write_file、edit_file、run_command、memory_propose，以及进入/提交新计划，返回subagent_readers_active；先等待或停止研究，再修改。外部程序/用户修改目录仍可能使来源过期，父须核对指纹；本能力不提供文件系统沙箱或外部写入锁。

## 模型工具与交接

- delegate_readonly(title, objective, role, task_id, skill_names, background, max_model_turns, max_tool_calls, timeout_seconds)：background默认false，同步返回当前研究报告；true接纳后立即返回子编号、当前状态及accepted，不等同完成。两个活动槽占满返回subagent_concurrency_limit，单轮最多8次接纳。同批连续委派可接纳多个独立研究；后续预生成的其他动作仍跳过，让父重新决定。
- wait_subagents(run_ids=[], mode="all", timeout_seconds=30)：空集合表示本轮全部；all等全部终态，any等任一终态；有终态时any立即返回。0秒只查询，最长60秒，等待超时不会取消研究。返回各子真实状态、已有结构报告和finished（所选全部终态）；跨父编号整批拒绝。
- read_subagent(run_id, offset=0, max_bytes=8192)：当前父Run范围内读取固定结构报告，返回content、真实状态和UTF-8安全next_offset；每页4—8192字节，字符内部或越界偏移拒绝，不执行模型。
- cancel_subagent(run_id)：单独停止并等待封存，返回真实部分报告，兄弟及父继续；重复取消返回原终态。只读子上下文不提供resume；开发型动作审批的专用接续见新协议。

父模型最终回答前，宿主等待并交接尚未收取的异步报告，要求父再核对。网页单项停止不假设父模型已经看过结果。父取消、超时、等待用户、额度耗尽或异常退出均在客户端关闭和父终态前取消并等待所有未完成子协程，保留各自终态。存储故障向父传播，不吞为研究成功；服务重启只读旧记录，不恢复模型或重放工具。

## 预算和用量

默认子上限为4次模型、8次工具、60秒，参数最大12次模型、30次工具、180秒；实际额度裁剪到父级未使用且未分配余额。仍在原总预算内为父保留2次模型、父有写入/编辑/命令能力时1次工具，以及剩余时间四分之一（最多10秒）。预算不够一次子模型/工具时拒绝接纳，不创建执行。

接纳时无await地预留子模型/工具额度；每次子调用消费自己分配并在父累计一次，终态归还未消费余额。父自己只能消费未预留部分，多个子项不能重复领取同一余额。非法调用和摘要也占预算，跳过未执行调用不占工具次数。所有模型请求与HTTP尝试通过ContextVar按asyncio任务归属路由，复用同一HTTP客户端也不会将父或兄弟用量写入错误子日志。已知token、HTTP尝试及模型/工具次数已计入父总数，页面不能重复相加；缺失用量保持未知。

子项及已尝试委派的父在最后可用模型轮或工具耗尽后以空工具声明总结；宿主再次拒绝违规工具请求，保留部分证据。摘要不能吃掉末轮或其他子项的预留。硬超时、取消和输入超限不追加模型请求；收尾空间不保证研究或交付必然完成。

## 统一结构报告

模型提交summary、findings、limitations、pending及complete。summary最多1200字符；findings最多8项，每项title160、detail600字符、最多2个evidence_call_ids；limitations/pending各最多8项、每项240字符。required表示范围内必须修复的缺陷，optional是可选加强，observation是事实观察；分类不扩大授权，required必须引用真实成功只读回执。complete=false必须提供限制或未完成事项。

宿主核验引用属于本子运行的实际成功读取，补充最多24条evidence和partial/reason，文件保留真实path、sha256及截断事实。UTF-8总上限32KiB；结构/超长/伪造引用无效则封存失败，原模型输出留在日志，回传不虚构发现的部分证据。失败、取消、超时和限额始终partial；执行completed也可为partial。引用存在不证明自然语言、行号或分类判断正确，父须核对并实际交付。

统一报告固定在子RunResult.subagent_report、父SubagentRecord.report及回执；新协议直接替换旧字符串预览，无旧格式兼容层。子报告不代表任务完成、用户采纳或项目独立检查通过。

## 持久接口与前端

子日志位于<runs_root>/<parent_id>/subagents/<child_id>/。RunManifest.delegation表达直属归属，不与会话续轮parent_run_id混用；子项不进入顶层会话索引。subagent_snapshot持久发布有界进度，历史缺父终态时各子最新计数只累计一次；无子终态且父不活动时显示interrupted。

- GET /api/runs/{run_id}/subagents：本轮快照及父活动状态。
- GET /api/runs/{run_id}/subagents/{child_id}?after=0&limit=100&offset=0：subagent、report_page.content、events、context、warnings。事件序号与报告字节偏移独立；未知直属项404、非法偏移422、归属或日志损坏503。
- POST /api/runs/{run_id}/subagents/{child_id}/cancel：同源校验及会话生命周期控制下单项停止；只允许当前父控制器操作，历史已终态只读返回，未活动返回409。重复操作幂等，不停止其他子项。

会话和详情展示所有子项的真实状态、摘要、分类发现、限制和待办；partial明确提示。展开读取实际来源、技能、额度与父预留、已计入父级的用量，完整结构/轨迹/上下文按需折叠和分页。活动子项提供“停止此子任务”，请求期间防双击，失败保持提示并可重试；历史隐藏停止。手机与桌面共用同一HTTP界面；未增加原生权限或凭据来源。

## 参考与后续边界

最初参考[Claude Code公开文档](https://code.claude.com/docs/en/sub-agents)的独立角色/上下文和显式技能，[Deep Agents文档](https://docs.langchain.com/oss/python/deepagents/subagents)及[开源实现](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py)的隔离结果交接，以及[OpenCode](https://opencode.ai/v2/docs/agents#steps)的末轮总结。未声称审阅Claude闭源调度实现；并发限流、额度预留和收拢协议是本项目的设计判断。

任意子上下文续接、多模型路由和Teams继续保留后续；开发委派及工作树集成首版见[本轮协议](DEVELOPMENT_SUBAGENTS.md)。确定性回归证明生命周期与边界，有限真实样例不证明稳定成功率、普遍节省token或G02—G04完成。

## 2026-11-01 首版历史验收记录

以下记录对应交接改造前的首版时点，仅保存原始验收事实；当前协议以本文前述规则为准。

### 工程回归

- 新增只读委派及HTTP专项17项，覆盖独立上下文、宿主只读及父权限交集、运行记录保护、任务队列幂等、报告分页、初始化失败、共享预算、父取消/总超时、子局部超时，以及固定项目约定、记忆修订和实际阶段继承。
- 后端首轮全量780通过、6失败、10跳过，原始报告 `.runtime/subagents-backend-all.xml`。6项上下文失败来自输入准备夹具漏计新增委派工具声明；补齐真实声明后关联28项通过，不放宽生产预算。
- 后端后续全量 **798通过、10跳过**，报告 `.runtime/subagents-backend-verified.xml`。跳过均为当前Windows权限或POSIX能力限制。最后补齐子执行器初始化失败收尾及父实际记忆阶段继承后，子Agent、记忆、规划和自动修复关联 **84项通过**，报告 `.runtime/subagents-phase-final.xml`；此前子Agent和压缩关联44项通过，报告 `.runtime/subagents-last-related.xml`。关联集合有重叠，不累加到全量总数，也不改写首轮失败记录。
- 前端完整回归 **161项通过**，报告 `.runtime/subagents-frontend-verified.json`；子报告、上下文、会话和详情关联30项通过，报告 `.runtime/subagents-frontend-related.json`。类型检查与最终生产构建通过，构建日志 `.runtime/subagents-frontend-build-verified.txt`。保留既有Vite大包与jsdom样式提示。
- 共享工作区同期存在会话管理开发，以上全量为对应时点整个工作区的联合结果，不等于C07新增用例数。此前前端中间失败报告仍保留，最终以已通过报告为准。

### 页面、独立检查与重启

离线服务只监听127.0.0.1:18843，使用真实前端、HTTP、SQLite队列、父子执行核心、文件工具和宿主独立检查；模型响应确定化，不作为真实模型质量证据。

从页面选择专属项目和独立金额检查后，父Run `8eccf468-f9ca-4505-9787-fd61e22e173b` 委派子研究读取service.py，使用子报告写出 `result.json`，实际内容为 `{"amount_minor":4200}`，状态completed/passed。总计5次模型、3次工具，包含子执行的2次模型、1次工具；页面可展开报告、真实文件来源、子上下文和完整轨迹。服务重启后重新读取原报告和检查结果，历史不触发模型重跑。

手机390×844下文档宽390，子报告面板约335，没有横向溢出。父Run `168786f8-780b-40b9-9160-163f6d7678a9` 在页面正式确认“停止执行”后，父状态cancelled/user_cancelled，子状态cancelled/parent_stopped。首次停止演练 `130a3d11-4892-47dc-8d26-e53f5e50f597` 仅打开确认框而未确认，子研究达到60秒上限，离线夹具当时没有处理失败回执，父运行失败；该记录保留。随后完善夹具错误响应并以新任务完成正式取消，不修改产品记录或冒充首次已通过。

审计为 `.runtime/subagents-browser-audit.json`、`subagents-browser-cancel.json`、`subagents-browser-restart.json`；截图位于 `output/playwright/subagents-20261101`。验收结束关闭专属服务与浏览器；未把浏览器证据描述为Windows安装包验收。

### 有限真实模型复验

仅使用本轮独立生成的金额接口小项目、测试约定及工具协议，不发送Harness主仓库源码。两次Run分别测试explore与review，不因失败追加重跑；每次最多8次模型调用、20次工具、180秒，每轮HTTP最多1次尝试。审计 `.runtime/subagents-real-20261101/audit.json`，完整父子轨迹在该目录的runs中。

| 场景 | 父Run | 实际结果 |
| --- | --- | --- |
| 探索接口并生成JSON | b54630c8-0dab-4ce3-a962-667a2ea316da | 子报告完成；父实际交付，通过预先固定的独立检查；completed/passed，7次模型、7次工具 |
| 审阅并修复合计计算 | b9921620-6b98-4e39-9c78-d9517b32cfd5 | 子报告完成，父已修复代码但达到8次模型上限；仍为limit_exceeded/unverified，10次工具。随后单独执行原固定检查通过，记录 `.runtime/subagents-real-review-independent.txt`，不把该结果回写为原Run验收通过 |

两次已知用量共109050 token，均为供应商返回的累计值，未推算费用。样例证明真实模型能够调用只读委派、回收报告及使用结果；也保留父任务收尾耗尽的限制，不能据此宣称稳定成功率、并行收益、普遍节省token或完成G02—G04。

本轮代码、前后端联动和验收文档已完成；Git暂存、提交及推送仍须用户单独明确批准。

## 2026-11-01 委派交接改造验收

本轮按后续明确批准的四项Plan调整批次屏障、预算收尾、独立角色/技能和结构报告；直接采用统一新协议，未增加并行、resume或供应商路由。

### 工程与异常边界

- 最终后端关联 **83项通过**，覆盖父子Runner、HTTP、压缩、主动规划上下文及自动修复；报告`.runtime/handoff-final-related.xml`。包括成功/失败委派后预生成写入均跳过、父子末轮恶意工具请求均被宿主拒绝、父总额度不增加、无效JSON/空限制部分报告/伪造引用失败封存、显式冻结技能、取消和超时、UTF-8分页、历史续轮。
- 首轮后端全量 **807通过、1失败、11平台跳过**，原始报告`.runtime/handoff-backend-all.xml`。失败项是新增非法报告测试误用helper默认的合法pending；修正为无任何限制和待办的非法部分报告后，最终关联83项全部通过。首轮在收集参数后才修正夹具，因此原报告保留该失败；没有将关联结果改写为全量重跑通过。
- 前端全量最终 **164项通过**，报告`.runtime/handoff-frontend-verified.json`；类型检查和生产构建通过，日志为`.runtime/handoff-frontend-typecheck.txt`及`.runtime/handoff-frontend-build.txt`。首轮全量163通过、1项详情页交互超时；该文件单独10项通过后，以2个worker完整重跑通过，未放宽断言或单项时限。既有jsdom样式/导航提示与Vite大包提示保留。
- 当前工作区另有同期会话管理改动，全量数字对应这一时点整个工作区；不是C07新增用例数。Git未暂存、未提交、未推送。

### 页面与重启

使用独立D盘数据目录`D:/Codex/Temp/harness-handoff-browser-20261101`，仅监听127.0.0.1:18843。模型响应确定化，前端、HTTP、队列、父子执行核心、文件工具与独立检查使用实际实现。

| 页面操作 | 父Run | 结果 |
| --- | --- | --- |
| 新建项目任务，选择独立金额检查 | 01cf488a-ff1a-4987-b98b-e81d766e2617 | 子读取service.py，父生成result.json金额4200分；completed/passed，页面展示结构摘要、观察、实际来源和父保留额度 |
| 请求部分研究 | 8c4138e8-ec2b-4009-9e3f-18480a5fbb43 | 子正常返回但partial=true，保留限制和“尚未核对调用方”；父解释未生成新文件，completed/unverified；页面显示“报告部分返回” |
| 子研究等待时停止并正式确认 | cc20016b-b51e-40d4-ba0f-b2516193f47e | 父cancelled，子cancelled/parent_stopped，partial报告封存；无后台子执行继续运行 |

手机390×844下文档宽390、报告面板335，无横向溢出。关闭空闲专属服务再启动，HTTP重新读取三份报告及页面刷新均成功；报告正文、父状态、父子事件数逐项一致，无新增模型请求或工具重放。审计为`.runtime/handoff-browser-audit.json`和`.runtime/handoff-browser-restart.json`，截图位于`output/playwright/handoff-20261101`。这属于共享HTTP界面验收，不作为Windows安装包验收。

### 两次有界真实模型复验

自动审批首次拒绝了外部发送；用户随后明确批准向已配置的DeepSeek API发送独立生成的金额函数夹具和工具协议，最多两个任务，不发送主仓库代码。之后只执行这两次Run；每次最多8次模型、20次工具、180秒，每轮HTTP最多1次尝试。审计`.runtime/handoff-real-20261101/audit.json`保存完整结果，父子日志在同目录runs下。

| 场景 | 父Run | 结果与额度 |
| --- | --- | --- |
| explore研究单价并生成JSON | 348e6915-2c42-49e9-be8c-df0e50315fa3 | completed/passed，7次模型、8次工具，51294已知token；子结构报告及实际引用通过核验 |
| review审阅合计计算并由父修复 | 8733e790-8d46-489e-96dc-da741bc5543e | completed/passed，8次模型、7次工具，49444已知token；父只将加法改为乘法，可选参数校验未扩大实现范围 |

两次共100738已知token，均为供应商实报，不推算费用。探索样例把普通事实标为required，说明结构及引用核验无法保证模型分类和自然语言结论正确；随后收紧角色提示，明确事实归observation，此提示措辞未追加真实请求复测。两例可证明有限样例的实际交接和交付，不能推导稳定成功率、普遍节省token或因果收益；G02—G04保持原状态。

## 2026-11-01 受控并行只读迭代验收

本轮按获批范围交付双槽异步委派、查询/等待、单项取消、原总预算预留及父退出收拢；同步等待仍是同一个delegate_readonly的明确执行选项，未保留另一套旧接口。目录基于同期重组后的runtime/、subagents/、context/等包，未回退其他功能改动。

- 新增5项并发专项用门闩确认两个子请求真实重叠，覆盖父/子模型、HTTP及token归属只计一次、第三项限流、未消费预算不得重复分配、等待0秒仅查询、共享目录父写入拒绝、单项取消后兄弟继续、父停止全部封存以及跨父编号拒绝。
- 后端全量814通过、11项Windows权限或POSIX能力跳过，报告.runtime/parallel-backend-all.xml。最后固定并发请求/响应轮号后，运行核心、子Agent、HTTP、压缩和规划关联136项通过，报告.runtime/parallel-final-related-v2.xml；该集合与全量重叠，不相加。早期一轮关联69通过、2失败来自拒绝回执data=None时的屏障读取，修复后关联72项通过，再完成后续全量及最终关联；原日志保留。
- 前端全量165项通过，报告.runtime/parallel-frontend-all.json；类型检查及生产构建通过，日志.runtime/parallel-typecheck.txt和.runtime/parallel-frontend-build.txt。新增单项停止失败提示、重试和父子编号断言；保留既有jsdom样式/导航与Vite大包提示。
- 页面使用127.0.0.1:18844与独立D盘夹具D:/Codex/Temp/harness-parallel-browser-20261101。Run 2fc4e502-e799-45cf-aa39-499c8fefc158从正式表单选择独立金额检查，两个子项均返回完整报告，父生成result.json金额4200分，completed/passed。
- 手机390×844上的Run 2dc26ae3-39ac-4c02-9b0b-8cdd99f79680从页面单独停止第一项后，第一项cancelled/user_cancelled，第二项仍running；随后父正式确认停止，第二项cancelled/parent_stopped，第一项原因不变。文档宽390，无横向溢出；部分报告和取消状态均可回看。
- 停止并重启专属服务后，两运行的报告、父终态、父子快照与事件数逐项一致，无模型重跑或工具重放。审计.runtime/parallel-browser-audit.json及parallel-browser-restart.json，截图output/playwright/parallel-20261101。最初Run 21ebfa76-1bec-4692-be9c-25444e85d984因离线夹具把注入AGENTS当用户要求而走同步路径；修正夹具从实际TaskSpec取场景后重新验证，原记录保留且不作为并行证据。

本轮模型响应均确定化，实际前端、HTTP、队列、Runner、文件工具及独立检查使用真实实现；未新增外部真实模型请求，不将离线并发测试视为真实模型质量或性能收益证明。服务和专属浏览器在验收后关闭。Git暂存、提交及推送仍需单独明确授权；同期会话管理、目录重组及原有文档改动独立保留。
