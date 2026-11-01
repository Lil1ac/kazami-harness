# 只读子 Agent（C07）

本轮按用户批准的 Plan 交付探索／审阅型串行委派。主 Agent 在当前 Run 中调用 `delegate_readonly`，等待独立子执行返回有界报告，再决定下一步修改和验收。子执行不进入单机队列，不另占执行槽；不会出现父 Run 占槽等待子 Run 排队的死锁。

## 权限、输入与任务关系

子执行使用父级实际工作区，只能使用父级当前已开放的读取工具与宿主只读集合的交集：`read_file`、`search_files`、`list_files`、`read_context`、`memory_search`、`memory_get`。宿主在参数分发前拒绝其他调用；文件修改、命令、任务与记忆写入、人机决定和递归委派均不可用。没有进程或写入者需要单独清理。

explore和review使用宿主内置的独立角色提示与只读工具集合，不复制父级system_prompt；共同宿主规则继续生效。子上下文包含明确研究范围、作为背景的父轮要求与批准方案，以及可选关联工作项的范围和完成条件。根AGENTS.md和项目记忆修订继承父运行固定快照；技能默认为空，只有delegate_readonly.skill_names显式选择的父级已冻结技能才进入子输入，未知或重复名称在创建前拒绝。子模型拥有独立的路径观察、记忆选择、回读和压缩状态。父聊天历史及工具输出不会自动注入子上下文，完整子对话也不会自动回灌父上下文。

可选 `task_id` 必须属于父会话清单，仅建立研究关联，不认领、完成或修改工作项。父 Agent 继续承担代码修改、任务状态更新和最终交付；报告返回不代表任务完成、检查通过或用户采纳。

## 工具、额度与失败语义

`delegate_readonly`参数为非空title、objective，可选role=explore/review、task_id、skill_names及子预算。默认最多4次模型、8次工具、60秒；参数上限为12次模型、30次工具、180秒。分配时从父剩余额度中保留2次模型、父有写入/编辑/命令能力时1次工具，以及剩余时间的四分之一（最多10秒）；记录parent_reserve与实际子额度。不足一次子模型或工具时返回subagent_budget_exhausted，不创建子执行。单轮最多8次委派，无自动递归和无限重试。

父子共用模型轮数、工具调用数和父总截止时间；摘要及非法工具调用也计数，HTTP重试及已知token同样累计。子明细已经包含在父总数中，页面不能重复相加。父级每次尝试委派返回后，无论成功、失败或拒绝接纳，同批剩余工具均以subagent_result_requires_review跳过且不占实际工具额度；父模型必须看到回执，再重新提交后续动作，避免执行报告产生前已生成的修改。

子运行以及已经尝试委派的父运行，在最后一次可用模型请求或工具额度耗尽后进入收尾：发送工具列表为空，仅在此次输入投影追加交接通知，不污染原始消息、压缩索引或续轮历史；摘要不得吃掉保留的末轮。供应商仍返回工具调用时宿主全部跳过，以limit_exceeded/handoff_tools_disabled结束。正常总结仍由原有独立检查判断交付结果；超时、取消和输入超限保持硬边界，不额外调用模型。预留额度只提供收尾空间，不保证研究或父任务一定完成。

父运行等待子执行期间保持可取消；父取消、服务关闭及父总超时向子传播，子终态先封存再继续父级收尾。子自身失败、超时、额度耗尽或partial报告以subagent_not_completed和真实子状态回传，父可以解释限制或使用剩余额度继续。模型失败和中断用量保留未知标记，存储错误直接上抛，不能伪造成功。

## 统一结构报告

模型最终提交JSON：summary、findings、limitations、pending及complete。summary最多1200字符；findings最多8项，每项title最多160、detail最多600字符、最多2个evidence_call_ids；limitations/pending各最多8项，每项240字符。发现分required、optional、observation；required必须引用实际读取，optional不授予父Agent扩大任务范围的权限。complete=false必须给出限制或未完成事项。

宿主只从本子运行成功只读回执核验引用、补充evidence（最多24项），并计算partial和reason；文件依据保留实际path、sha256及截断事实，搜索/记忆依据以真实tool_call_id追到子轨迹。不把模型写出的路径、指纹、行号或验收作为已核验元数据；引用存在不等于自然语言判断、分类和行号语义正确，父Agent仍须核对。全文UTF-8最多32KiB。普通文本、结构超限、伪造引用分别拒绝为稳定报告错误；原始模型输出仍在子日志，返回无虚构发现的部分报告和实际证据。失败、取消、超时和限额永远partial；模型正常结束也可主动partial，不能据此显示完整交接。

同一份SubagentReport固定在子RunResult.subagent_report、父SubagentRecord.report和父工具回执中。`read_subagent(run_id, offset, max_bytes)`仅分页读取该结构JSON，返回content，单页最多8192个UTF-8字节；偏移必须落在字符边界。该工具只读取当前父Run已登记子项，不重新运行子Agent。新协议直接替换旧字符串预览和report_truncated，不提供旧会话格式兼容层。

## 持久记录、接口和界面

完整子日志位于 `<runs_root>/<parent_run_id>/subagents/<child_run_id>/`，复用 RunStore 的清单、连续事件和唯一终态协议。`RunManifest.delegation` 保存父归属，与多轮会话 `parent_run_id` 分开；子项不出现在顶层会话索引或后台队列。嵌套日志仍使用真实 runs_root 保护运行记录及记忆边界。

父日志以 `subagent_snapshot` 保存有界进度，`RunResult.subagents` 固定该轮委派。父历史缺少终态时，以各子最新快照累计一次计数，不重复累计每条快照；父已经停止且子缺终态时显示中断，不自动恢复。

- `GET /api/runs/{run_id}/subagents`：本轮委派列表及父活动状态。
- `GET /api/runs/{run_id}/subagents/{child_id}?after=0&limit=100&offset=0`：返回subagent、report_page（正文content）、events、context、warnings；报告及来源统一由subagent.report提供，不再重复顶层report/sources。after为子事件序号，offset为报告字节偏移，不能混用。
- 未登记子项返回404；非法报告偏移返回422；日志损坏或归属变化返回503。读取不投递作业、不调用模型、不重放工具。

会话内及本轮详情显示委派目标、职责、状态，展开后读取摘要、分类发现、限制、未完成事项及实际来源；partial有明确提醒，completed与partial组合显示“报告部分返回”。显式技能、子预算、父预留和已计入父级的用量一并可查；完整结构JSON、轨迹和上下文继续折叠，报告及轨迹各自分页。历史只读，后续轮不修改旧报告。Windows桌面复用同源HTTP界面和已有父任务停止入口，子Agent没有新增原生权限或凭据来源。

## 官方参考与本项目取舍

- [Claude Code](https://code.claude.com/docs/en/sub-agents)：公开文档描述独立系统提示、独立上下文、显式子技能及maxTurns行为。Claude Code本体闭源，此处不声称审阅了其内部调度源码。
- [Deep Agents文档](https://docs.langchain.com/oss/python/deepagents/subagents)和[开源中间件](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py)：同步委派、子消息隔离及最终文本/结构结果回收支持当前串行方向。
- [OpenCode steps](https://opencode.ai/v2/docs/agents#steps)：参考最后一轮关闭工具并要求总结的方式。

同批工具屏障、固定父预留额度、有界统一报告及宿主证据核验是本项目结合原有Runner、预算和验收体系做出的设计判断，不表述为上述产品全部采用同一实现。并行、resume和子模型路由继续保留后续，不在本轮扩展。

## 验收范围与后续边界

验收覆盖真实Runner与HTTP队列：报告被父模型用于文件交付、上下文隔离、父权限交集、运行记录保护、固定项目约定、共享预算及同批跳过、供应商失败、子限额、父取消/超时、子局部超时、中文报告分页、重复作业及重启只读。前端覆盖展开才读取、来源和用量语义、独立分页以及中断/读取错误展示；真实页面验证从新建操作到独立检查和手机布局。

当前仍是同一供应商、同一模型、一个活动子执行的串行研究。并行、独立停止子项、写入委派、隔离工作区集成和Teams不在本轮范围。工程和确定性离线模型只能证明链路与约束，不证明真实模型委派收益、无人干预成功率或token节省；G02—G04保持原状态。

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
