# 项目长期记忆：类型策略

2026-10-31 用户批准首版及后续类型策略 Plan。当前类型为 rule、decision、fact、reference、experience；feedback 已改为独立的 user_correction 形成方式。type 现在决定结构校验、召回触发、输入呈现、冲突和复核，不决定权限、确认状态或固定有效期。

## 本轮目标与架构

围绕类型完成一个可运行闭环：结构化保存 → 确认/冲突审阅 → 固定 Run 修订 → 按任务和工具动作召回 → 有界输入 → 逐次使用记录。memory/types.py 集中定义类型内容、适用范围、呈现和确定性冲突策略；memory/service.py 负责修订事务、来源、证据和运行控制；网页、桌面与 CLI 共用同一服务。

持久化仍为 `<runs_root>/.memory/memory.sqlite3`，scopes、revisions、operations 分别保存项目身份、不可变修订及幂等回执。数据库结构标记为2，仅维护当前结构，不保留旧版兼容或迁移逻辑。按用户要求，已直接删除项目 `.runtime` 下核验为旧版的73个记忆开发库，新版库保持不变；后续保存按当前结构创建。格式无效或存储损坏仍返回 memory_storage_error。

项目归属仍由规范来源路径与目录对象身份决定；宿主隔离 worktree 沿用源项目，实际文件、分支和提交按执行目录核对。状态保持 proposed、active、rejected、deleted。模型只能提出候选；人工确认才能生效。

## 类型结构与实际行为

| 类型 | details 主值 | 使用和复核 |
| --- | --- | --- |
| rule | topic、behavior；可选 reason、exceptions | 按范围/阶段提供条件与行为；项目通用约定不依赖任务关键词；普通代码修改不使约定失效 |
| decision | topic、choice、rationale；可选 revisit_when | 提供选择和理由；同主题重叠范围的新选择须审阅，代码修改不自动撤销 |
| fact | subject、attribute、value；可选 observed_at | 按对象属性检索；证据变化或不可读后需复核，停止直接注入及回读旧断言 |
| reference | target、purpose；可选 consult_when | 默认短入口，不自动注入补充正文；本地目标不存在时不可用，内容更新不使入口失效；URL 不在保存/列表读取时联网 |
| experience | situation、action、outcome、limitations | 提供真实案例和限制；成功和失败均可保留；环境不符不采用，不代表当前测试通过，不自动生成规则或 Skill |

content 仅为可选补充说明，主值不重复存入另一套字段。类型切换须重填匹配 details。单条公共内容和条件合计最多8 KiB；必填空白、错误类型结构、凭据及非法时间拒绝保存。

origin 表示 user_correction、user_confirmation、tool_observation 或 run_experience，与宿主 source（人工保存或经核验的 Run/事件）分开；origin 不构成可信等级或授权。

## 范围、证据与入口

- applies_to.paths：工作区相对 glob，支持 *、**、?，**/ 包含零层目录；不支持字符集合。空列表覆盖整个项目，路径本身不捕获指纹。
- applies_to.phases：planning、execution、verification（公开验收失败后的修复）；空列表不限。阶段来自宿主状态。
- applies_to.platforms：win32、linux、darwin；空列表不限，依据实际执行平台。
- evidence：最多8个具体相对文件，保存时由宿主捕获完整指纹，单文件最多1 MiB。只有 fact 据证据变化停止当前断言使用；其他类型的证据用于说明历史来源。
- reference.details.target：安全工作区相对入口或无凭据 HTTP/HTTPS URL；作为查阅目标，不当作证据指纹。
- branch、commit、expires_at：仍为明确条件，不按类型设置默认 TTL；observed_at 未提供时不伪造为保存时间。

## 召回、工具与预算

作业接纳时固定 use/capture 与宿主限额，实际出队读取最新已确认修订。Run 期间固定版本，候选确认、编辑和删除从新 Run 生效；文件事实证据和目标可用性在使用前重新核对。

初始按任务文本匹配；通用 rule 按适用阶段加入。实际文件工具提供安全相对路径，触发范围匹配。优先提供适用行为约定，再按文本相关性选择知识，不设置 type 分数或每类固定配额。中文支持二字片段。默认自动输入最多6条、16 KiB，同时受原整体字节/token预算约束，条目整体减少，不能提高总限额。

写入/编辑前首次命中尚未出现在模型输入中的 rule 时，工具返回 memory_context_updated，变更不执行；同批尚未执行的调用全部配对为未执行，交给下一次模型请求重新决定。命令无法可靠推断副作用，保守考虑全部路径约定，不靠解析 argv 猜测写入范围。宿主只保证规则被提供，不宣称模型已遵守规则。

自动输入将类型主值编译为直接的中文使用说明，省略存储摘要和创建时间等元数据，reference 不展开补充说明。max_entries=0 时可以明确回读约定；必须在实际发给模型的输入中凑齐全部 UTF-8 分页才计为已展示，同批刚返回或仅尾页都不能放行变更。

ContextBuilder 只替换输入投影中的记忆片段，不改变原消息索引或原始日志。memory_prepared/read/proposed 与 memory_selected 保存准备、真实回读、候选和各次执行模型输入；uses 带模型轮次、阶段、已观察路径、实际类型视图、原样记忆输入及完整可见约定编号。旧轮成功记忆工具结果和覆盖它们的旧摘要不直接继承，防止删除知识绕回；原日志保留。

memory_search 按当前条件返回短检索结果；memory_get 从固定修订返回 UTF-8 安全分页和完整 JSON 指纹，并重新核对事实证据。reference 的补充笔记可以明确回读，入口资料仍通过原工具读取，不增加权限。规划只允许 search/get。

## 冲突与人工操作

rule 使用 topic/behavior、decision 使用 topic/choice、fact 使用 subject+attribute/value 比较同键不同主张；平台、阶段、分支和提交互斥时不冲突。路径只有能确定互斥时才排除，复杂 glob 保守要求审阅。reference/experience 不假装存在唯一主张键；自由文本矛盾不宣称已全部识别。

候选可以保留冲突供审阅，不自动停用旧值；直接保存或确认冲突返回409和同项目 conflicts 摘要。人工通过 replacements=[{id,revision}] 明确选择旧值，确认时复核版本，旧值失效和新值生效同事务完成。相同 request_id/载荷返回原回执，载荷不同拒绝；expected_revision 保护编辑，失败保留草稿。

## 前后端联动与验收 Plan

项目页、消息保存和候选审阅采用动态字段；形成方式与类型分开，范围/证据/入口分别编辑。冲突展示旧值和适用路径，勾选替代后保存；上下文展示类型主值、采用原因和各次输入。导出包含结构内容、来源、范围与证据，桌面仍走统一原生下载。

验收覆盖五类各6个固定行为场景、真实 HTTP/SQLite/Runner 的变更暂停与重审、版本化替代、跨会话删除、前端类型切换和冲突审阅、桌面导出及手机布局。真实模型仅使用本轮自建夹具，最多5组/10 Run，固定模型、任务和预算；标签基线为独立输入投影对照，不保留旧生产实现。失败保留，不无界补跑，不把小样本推导为总体成功率。

向量检索、后台提取/整合、全局用户记忆、跨设备同步和自动生成 Skill 仍在后续。

## 调研依据

- [Claude Code](https://code.claude.com/docs/en/memory)：四类 notes 和短索引/按需正文；公开文档未证明按 type 固定加权或差异 TTL。
- [OpenAI Docs：Codex Memories](https://learn.chatgpt.com/docs/customization/memories)：提取/整合与使用/生成分开，长期必需规则保留在 AGENTS.md。
- [LangGraph](https://docs.langchain.com/oss/python/concepts/memory)：事实、案例和行为指导采用不同使用方式，由应用实现。
- [Mem0 实现](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)：procedural 有独立生成分支，也标记未来移除，不仅看枚举推断能力。
- [Zep facts](https://help.getzep.com/facts)：事实适用时间与记录时间分开，时间标记不证明事实正确。

## 本轮验收记录

2026-10-31 完成实现、前后端联动、工程回归和有界真实模型复验；保留此前失败记录，不将小样本描述为总体质量提升。提交范围仅包含项目记忆及必要的前后端联动；Tasks/队列保留在主工作区，不随本提交纳入。前述774项后端及150项前端结果来自完整联调工作区。拆分后的提交独立复验记忆后端56项、前端关联43项、类型检查与生产构建通过；压缩/会话/规划关联59项通过。

### 工程回归与环境复验

- 五类各6项固定场景，加完整/不完整分页展示、中文类型输入及到期事实替代4项边界，共34项；最终纳入全量回归。
- 最终后端 **774通过、10跳过**，报告 `.runtime/type-backend-verified.xml`，日志 `.runtime/type-backend-verified.txt`。最后核对摘要耗尽执行额度不记录虚假采用、修复重新规划优先级后，压缩/修复/记忆关联72项通过，报告 `.runtime/type-last-related.xml`。跳过来自当前 Windows 符号链接权限或 POSIX 专属能力。
- 前端全量 **32文件、150通过**，日志 `.runtime/type-frontend-all.txt`；最后调整中文阶段和冲突展示后，关联 **2文件、15通过**，日志 `.runtime/type-frontend-final.txt`，类型检查与生产构建通过。保留既有 Vite 大包和 jsdom 样式提示，不将重叠测试相加。
- 真实 HTTP/SQLite/Runner 验证：未展示的路径约定阻止首次变更和同批剩余调用；下一次输入出现约定后，新参数才写入文件。另验证409冲突、版本化原子替代、跨会话删除及历史输入不变。
- 首次沙箱全量因子进程 command_spawn_failed 中止；正常权限下复验单项通过。第一次正常权限全量761通过、9失败、10跳过，9项来自临时目录位于当前 Git 仓库内而被识别为仓库子目录；改用独立 `D:/Codex/Temp` 后目录相关28项通过，随后最终全量零失败。未修改目录登记逻辑来迎合夹具。

### 实际页面与重启

离线服务只监听127.0.0.1:18841，使用真实前端、HTTP、SQLite、队列和文件工具。真实表单完成类型切换、保存、相反约定409提示、明确勾选替代和旧值停用；手机390×844下文档和抽屉均宽390。服务重启后数据仍可读取。

学习运行 `e0822efd-905c-4a47-a97c-6ac9f43e9065` 在页面提出结构化决策候选，人工点击保存；另一个会话运行 `27ef025d-4876-4f52-8d89-e635b7ffa86d` 采用该决定并实际写出 `{"field":"legacy_name"}`。运行详情能回看两次模型请求的类型说明、阶段、路径和来源。没有独立 verifier 的样例仍显示“完成·未验收”。截图在 `output/playwright/memory-types-20261031`，本段离线模型不作为真实模型质量证据。

### 有限真实模型结果与边界

首批最多10个Run；用户随后明确批准追加最多4个Run。两批均仅发送独立生成的 JSON 夹具、测试记忆及工具协议到 `https://api.deepseek.com/chat/completions`，不发送主仓库源码。每Run最多8次模型调用、20次工具、180秒；首批每模型轮最多1次HTTP尝试，追加批最多2次。无无限补跑。

| 批次/样例 | 标签基线 | 类型方案 | 可得结论 |
| --- | --- | --- | --- |
| 首批：约定 | 生成数字2.4 | 生成数字2.4 | 已召回仍未遵守格式，保留失败；据此改为直接中文类型说明 |
| 首批：决策 | 询问用户、无文件 | 询问用户、无文件 | 未观察到收益，合理询问不冒充错误实现 |
| 首批：事实 | model_transport | model_transport | 无有效对照 |
| 首批：入口 | model_transport | external_name: ada | 单个类型样例通过，无有效配对 |
| 首批：经验 | model_transport | model_transport | 无有效对照 |
| 追加：最终约定 | model_transport | amount: "2.40" | 最终方案样例通过，基线无有效结果 |
| 追加：最终决策 | model_transport | legacy_name: ada | 最终方案样例通过，基线无有效结果 |

首批网络失败实际为5次，追加为2次，全部保留。首批审计 `.runtime/type-real-20261031/audit.json`；追加审计 `.runtime/type-real-final-20261031/audit.json`。最终通过Run为 `34ae2363-a55d-4148-a95e-176199cf8d07`（约定）与 `d7ac9205-24e7-417f-aaa1-da28bf54c072`（决策）。两批已知用量合计176566 token，失败轮用量不完整，不能当作完整成本。

标签基线为独立输入投影模拟，未保留旧生产实现；不是旧版全栈回归。最终约定和决策的单例行为已核对，但没有最终版本的有效成对比较，不能据此宣称总体成功率提升、普遍节省token或所有类型均有真实模型收益；G02—G04状态不变。

## C09 首版验收记录（历史）

验收日期为 2026-10-31。日志及样例在 D 盘工作区 `.runtime`，测试临时项目在 `D:/Codex/Temp`，截图位于 `output/playwright/memory-20261031`。这些生成目录不进入版本控制。

### 工程回归

- 后端首轮全量 724 通过、9 失败、10 跳过；失败为无记忆时仍插入空前缀，影响旧会话断言。修复为无可用条目不新增前缀后，上下文、会话、中断接续、技能、规划导出和桌面 68 项关联回归全部通过，报告 `.runtime/memory-backend-related.xml`。
- 后端后续全量 735 通过、1 失败、10 跳过，报告 `.runtime/memory-backend-final.xml`。唯一失败为记忆导出缓存篡改时 StorageError 未转受控响应；已修复为 memory_storage_error/503，最终领域、API、部分配置继承、桌面导出和队列 23 项通过，报告 `.runtime/memory-final-contract.xml`；全量失败用例单独复验 1 项通过，报告 `.runtime/memory-last-failure-check.xml`。不把全量原日志改写为零失败。
- 最后补验删除后真实续轮不重注入旧 memory_get 正文，连同记忆 API 和桌面导出 8 项通过，报告 `.runtime/memory-final-input.xml`；桌面记忆/计划导出最终 2 项通过，日志 `.runtime/memory-native-final.txt`。这些与全量重叠，不相加为总数。
- 前端最终完整回归 32 文件、147 项通过，日志 `.runtime/memory-frontend-final.txt`。补充桌面记忆链接接管后，记忆管理、桌面下载和上下文组件 16 项全部通过，日志 `.runtime/memory-frontend-last-related.txt`；类型检查和最终生产构建通过。保留既有 Vite 大包及 jsdom 样式能力提示。
- 单机无文件条件的短文本夹具含 1000 条记忆，20 次 SQLite 读取、适用过滤、构建读取集及搜索的 p50 为 23.87 ms、p95 为 29.66 ms，无额外模型请求。仅描述本机该夹具，不作为任意项目性能保证，记录 `.runtime/memory-capacity-20261031/audit.json`。

### 真实页面与重启

18839 专属离线服务驱动真实 HTTP、SQLite、Runner 和文件工具。页面学习运行 `d51a8ec0-937d-4bae-aa2f-705cc74b272d` 提出候选，正式点击“保存记忆”后，新会话 `ec05b91f-6963-4af5-854a-31bf2072a088` 使用 v2 约定并生成 `result.json`，field 为 legacy_name。项目页编辑为 v3，历史可回看 v1/v2/v3，Markdown 下载包含当前正文及来源。

手机页面明确删除为 v4 后，运行 `0704e418-df64-4c35-a110-ce3d130082f6` 不再读取约定，样例输出 field 为 need_user_decision；旧运行仍保留 v2 原文。审计在 `.runtime/memory-browser-20261031/browser-audit.json`。最终后端服务重启、页面刷新后继续读取同一数据库；新运行 `1b62a6b2-24de-4669-baa7-6a7898ea5f98` 可以重新提出候选，未批准该布局检查候选。

390×844 视口下，文档宽度和抽屉宽度均为390，候选位于消息滚动区，输入区底边830保持可操作。截图：`01-project-memory-desktop.png`、`02-project-memory-mobile.png`、`03-candidate-mobile.png`。受控服务重启期间出现连接中断记录，恢复后页面链路正常。没有把浏览器和原生宿主协议测试描述为 Windows 安装包验收。

### 有限真实模型对照

按批准范围最多四次真实 Run，仅发送本轮新建 JSON 样例、测试记忆和工具协议，不发送 Harness 主仓库源码。每轮最多8次模型调用、20次工具、180秒，不因失败额外补跑。原始记录及独立文件检查在 `.runtime/memory-real-20261031/audit.json`。

| 样例及选择 | Run | 实际结果 |
| --- | --- | --- |
| 姓名字段，关闭记忆 | d0794320-37d6-4bbc-a0f1-1c6558c9be4d | 3次模型调用后主动询问字段规则，waiting_input；未生成文件，不能把合理询问算作错误实现 |
| 姓名字段，开启记忆 | 54e9b37f-b179-4a12-b8a0-bd6ca72eafe0 | 5次模型调用，独立读取输出为 legacy_name: ada，与已确认决定一致 |
| 金额单位，关闭记忆 | 2ea8ed63-1f00-430e-8bc1-50a9852cb587 | 首次模型请求发生 model_transport，保留失败，不可据此进行有效质量比较 |
| 金额单位，开启记忆 | 461ba40e-2724-46f4-8a7f-26535702ecf8 | 6次模型调用，实际 memory_get 回读，独立输出为 amount_yuan: 1.25 |

开启记忆的两个样例均符合独立脚本预期，Run 未配置独立 verifier，因此状态仍为 completed/unverified。三轮有已知用量合计73192 token，网络失败轮用量不完整，未计算完整费用。样例说明已确认知识能够被真实模型采用，不能推导普遍节省 token、总体成功率或自动候选质量；本轮不因此把 G02—G04 标为完成。

实现、前后端联动、回归和文档同步已完成，Git 暂存、提交和推送仍须用户另行明确授权。
