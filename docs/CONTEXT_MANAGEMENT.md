# 上下文管理：项目指令、分层压缩与输入预算

本阶段对应 [PRD](PRD.md) 的 V0.2 上下文管理第一片。CLI 与网页共用同一实现：启动任务时固定项目约定，每次请求模型前检查完整输入。原始事件继续保留。已接入输出外置、规则遮蔽、结构化摘要与只读证据回读；中断后通过普通消息接续；递归指令发现和容器仍待实现。

## 1. 使用方式

在实际任务工作区根目录放置 UTF-8 的 `AGENTS.md`，写入代码风格、测试要求等项目约定。普通工作区读取自身文件；代码项目读取固定基线创建的独立工作树版本，源仓库未提交改动不会带入。只自动加载这一份文件，README 与其他文件由 Agent 根据任务通过工具读取。

运行配置支持以下 TOML，默认值与示例一致：

```toml
[context]
load_project_instructions = true
max_input_bytes = 262144
max_project_instruction_bytes = 32768
```

三个值分别为是否加载、整体输入字节上限、原始项目指令文件字节上限。两个上限都必须是严格正整数。配置按“默认值 → TOML → 覆盖值”逐字段合并 `context`，不会因为只改整体预算而丢掉其他宿主设置。

网页在“运行配置”设置加载开关及两个上限，输入单位为 KiB（1024 字节），提交时转换回字节。应用只影响后续提交，已开始任务使用固定配置。任务详情的上下文展示来源、内容、摘要、最新拟请求轮次、输入大小和上限。刷新和服务重启后读取同一份保存记录。

`--prepare-only` 仍只创建准备记录，不读取项目指令或技能文件、不调用模型。没有上下文事件的旧任务显示“未记录”，不能由当前文件或新增默认值推断当时加载情况。

## 2. 输入组装与计量

`context.py` 的 `ContextBuilder` 在一次运行内固定任务、配置与项目指令；Agent 维护原始消息列表。每次请求前构建独立的消息和工具定义副本，适配器接收的正是本次检查的副本。

消息依次为宿主系统规则、可选的固定上下文 user 前缀（项目约定与所选技能共用一条）、实际用户任务，以及后续模型与工具消息。项目约定被标识为项目输入，不能改变宿主权限、预算或验收入口；这些边界仍由后端代码执行。工具调用、对应结果及供应商保留的 `reasoning_content` 一起参与计量，原始历史完整保存，实际模型输入可以使用下述压缩投影。

计量版本 `json_utf8_v1` 定义为：

```python
len(json.dumps(
    {"messages": messages, "tools": tools},
    ensure_ascii=False,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8"))
```

计入 JSON 结构、转义、工具参数/结果和工具定义；空工具列表也保留在固定结构中。不计 model、stream、max_tokens 等 HTTP 封装字段。这里是确定的本地输入字节预算，不是 token 数、完整 HTTP 包大小或精确费用，不保证避免模型厂商的上下文窗口错误。供应商返回的 token 用量仍按原有字段独立统计。

输入恰好等于上限可以请求；超过上限则记录拒绝并停止，执行状态 `limit_exceeded`，结束原因 `max_model_input_bytes`。该拟请求不会增加实际模型轮次、HTTP 尝试或启动后续工具。已执行的工具和已产生文件保留，不做回滚。单次工具输出预览限制继续生效，但不代替整体输入检查。

## 3. 指令快照与错误

启动时只读取一次根 `AGENTS.md`，保存完整内容、相对来源、原始 UTF-8 字节数与 SHA-256。运行期间文件变化不会改变该次已固定输入；后续新任务会重新读取。

| status | 含义 |
| --- | --- |
| loaded | 已加载并保存完整内容，空文件也是有效快照 |
| missing | 根文件不存在，正常继续 |
| disabled | 配置关闭，不访问该文件 |
| error | 路径、读取、编码、类型或大小异常，模型请求前停止 |

文件读取受工作区及运行记录路径保护，拒绝指向外部或运行记录的链接。只读允许上限加一个字节，并检查读取前后文件身份，拒绝特殊文件及检测到的并发变化，不静默截断。当前仍是可信本地执行模式，这些检查不构成命令沙箱。

错误状态使用 `project_instructions_` 前缀：`too_large`、`invalid_utf8`、`unreadable`、`outside_workspace`、`run_records_forbidden`、`not_regular_file`、`changed`、`invalid_path`。错误快照不包含部分正文或底层异常内容；页面展示可读原因，运行状态为 failed，未请求模型。

## 4. 事件与 HTTP 契约

`RunConfig.context` 保存本次完整策略；`POST /api/runs` 的 `config.context` 可按字段覆盖。浏览器不能提交任意项目说明路径，宿主系统提示词和凭据仍不可从此入口修改。

新增事件沿用单写入者与连续序号：

| 事件 | payload 与时机 |
| --- | --- |
| context_prepared | `{instruction, serialization}`；指令固定后提交，包括 missing、disabled 和 error |
| context_checked | `{model_turn, input_bytes, max_input_bytes, message_count, allowed}`；每次拟请求前提交，包括超限拒绝 |

`model_turn` 是拟请求的正整数轮次；被拒绝时它可能比实际 `result.model_turns` 多一。所有大小单位为字节。`run_started` 保存初始消息与工具定义，后续消息继续增量追加，不每轮重复保存全部输入。

`RunView.context` 为 `{instruction, last_check, serialization}` 或 null。`instruction` 包含 status、固定 path=`AGENTS.md`、可空 content/sha256/size_bytes/error_code；`last_check` 尚未发生时为 null。服务从完整已落盘事件还原最新值，不依赖前端当前加载的轨迹窗口，也不会在查询时读取当前项目文件。上下文事件格式受损时显示警告与未记录，仍保留原始事件供检查。

## 5. 验证与后续边界

核心测试覆盖字节规则、恰好边界、一次快照、文件异常、首轮及工具后超限、消息配对和既有产物。Web 测试覆盖配置合并、跨应用重启、旧记录、SSE/分页和 Git 基线。前端检查配置换算、实际提交、上下文展示与未知状态；页面操作使用真实 Runner 和工具核对结果。

离线浏览器辅助服务可显式运行 `uv --cache-dir .uv-cache run python tests/helpers/serve_context_fixture.py`，默认端口 18769，专属记录在 `.runtime/context-browser/runs`。辅助模型检查实际收到的项目规则；“持续增长”任务执行真实文件工具后增大输入。它只证明运行链路，真实模型接入证据与本轮测试结果集中记录在 [控制台验证记录](WEB_CONSOLE.md#7-已保留的验证证据)。

[多轮任务交互](MULTITURN_TASKS.md) 已接入：从直接前轮记录重建用户、回答及配对工具消息，移除旧宿主/项目指令前缀，为新工作区重新固定当前指令；完整继承历史与新要求共同计入已有输入预算，超限明确停止。历史来源的事件序号及消息摘要在模型调用前再次核对。历史压缩通过统一构建入口接入，采用下述可追溯投影；中断后沿用普通消息核对当前代码与未知工具结果，不维护独立检查点协议。


## 6. 分层压缩策略（2026-10-22）

压缩配置逐字段合并；兼容默认是off，旧清单不会被新版本自动改变。仓库harness.example.toml明确采用hybrid。

~~~toml
[context]
load_project_instructions = true
max_input_bytes = 262144
max_project_instruction_bytes = 32768
compression = "hybrid"          # off / masking / hybrid
trigger_ratio = 0.8
target_ratio = 0.6              # 必须小于trigger_ratio
inline_tool_bytes = 8192        # 外置阈值，UTF-8字节，最小512
keep_recent_groups = 3          # 近期完整assistant/工具批次，1至100
max_summary_calls = 2           # 本运行摘要次数上限，0至20
max_summary_bytes = 8192        # 摘要正文上限，最小512字节
# model_context_tokens = 65536  # 可选声明；不按模型名称猜测
token_safety_margin = 2048
~~~

- off：完整历史检查，超限停止，不提供额外回读工具。
- masking：成功大输出先外置，接近预算时遮蔽旧成功结果，保留元数据与引用；不调用摘要模型。
- hybrid：规则处理后仍有压力时，才对完整旧工作片段生成结构化摘要。

compression.py维护输入投影，原列表不覆盖。run_started继续保存完整初始历史，assistant_message/tool_result追加原始执行消息；摘要用独立事件，不能混入用户真实对话。

固定保留宿主规则、当前AGENTS.md、全部用户原文、最近完整批次和含失败结果的批次；不拆工具调用与结果。摘要候选不能跨这些边界，优先选择内容较多的旧片段；小于1KiB的零散片段保持原文，避免为极少字节反复调用模型。当前不声称自动识别所有语义冗余或当前有效需求。

执行顺序：

1. 成功结果超过inline_tool_bytes，保存当时捕获的正文；输入副本换为状态、路径/游标、截断标记、短预览和ctx引用。
2. 每次请求前计量；超过触发比例时按最旧优先遮蔽旧成功工具正文，尝试降至目标比例。
3. 仍有压力且有摘要额度时选择旧区间。摘要输入自身也检查字节和声明窗口预算，必要时缩小选区。
4. 无工具摘要请求返回严格JSON，字段为goal、decisions、changes、evidence、open_issues、next_steps；拒绝格式无效、工具调用、截断、过大或没有实际缩小的结果。
5. 成功变换先写事件，再提交内存投影。摘要失败明确停止，不采用部分替换；最终仍超限时按字节或估算token的原因停止，不无界重试。

摘要与执行共用供应商适配器、输出上限、单轮超时、HTTP重试和运行总截止时间。model_turns、http_attempts和usage包含摘要；另有max_summary_calls子预算，并给后续正常执行至少保留一轮。结构校验不能保证语义完整，历史测试只作参考，不改变本轮宿主验收或审阅。

## 7. 证据外置与回读

启用压缩后自动提供只读read_context，不扩大四类基础工具的文件或命令权限。额外定义参与输入计量，调用占工具次数，页面明确显示历史回读。

~~~json
{"reference":"ctx:<run_uuid>:<sha256>","offset":0,"max_bytes":4096}
~~~

offset与next_offset按字节；单页64至16384字节，使用返回游标继续。UTF-8末尾保持字符完整；非UTF-8捕获流以替代字符展示并标记decoding_replaced。

归档在受保护的运行context目录，以内容SHA-256命名。模型只提交引用，不能提交磁盘路径。单对象最多8MiB、本运行新增对象合计64MiB；容量不足不发布引用，原结果保留并继续受输入预算限制。读取检查摘要、普通文件身份与路径，拒绝链接。祖先回读沿同会话父链核对可信终态，最多256个关系，不开放其他会话。

引用保存的是当时返回的工具结果，不等于完整源文件；原分页或进程输出已截断时保留其语义。成功命令结果外置时，额外尝试提供captured_streams的stdout/stderr引用，指向实际捕获日志；不可用流列入unavailable_streams，捕获超限保留capture_output_limit_exceeded。未保存的部分不能称为完整输出。

read_context的最新结果不会立即再次外置，避免刚取回就被替换。读取历史不重放工具，也不能证明当前文件与历史相同。

## 8. 跨轮摘要与事件契约

追加仍先重建完整父原始历史并核验事件序号和消息摘要。开启压缩时只复用前轮最终仍生效的工作摘要，已被覆盖的中间版本不重复导入：核对原始消息范围、SHA-256和归档正文，按新旧指令前缀长度调整索引，在新运行中发布摘要引用。reason=inherited不产生额外模型调用；来源无法核对以context_projection_unverified停止。旧运行日志、摘要和代码不变。

| 事件 | 事实 |
| --- | --- |
| context_action | offload/mask/summary/summary_failed；原始消息零基半开区间、指纹、引用、整体输入前后字节数、可空结构化摘要与原因 |
| context_compaction_started | 选区、实际摘要请求messages/tools、输入大小、本运行摘要序号 |
| context_summary_response | 实际摘要响应，和执行assistant_message分别保存 |
| model_request_started / model_response | purpose区分execution与summary，统一模型和用量计数 |
| context_checked | 新增purpose、raw_input_bytes、estimated_input_tokens、max_input_tokens；旧字节计量版本不变 |

RunView.context新增actions（最近50条）、action_count和summary_calls，完整记录仍在事件中。查询/重启只读，不触发压缩、模型或工具。上下文检查器显示前后大小、范围、来源、摘要字段、失败和跨轮复用；聊天内容保持完整。

token估算使用UTF-8字节启发式、消息开销和15%余量，可用实际供应商prompt_tokens保守上调比例。未声明窗口时只展示估算并执行字节硬限制；声明后减去最大输出和token_safety_margin作为估算输入限制。它不是精确tokenizer，不能保证厂商不会拒绝请求。

## 9. 验证与边界

回归覆盖中间证据回读、UTF-8分页、对象篡改和跨会话拒绝、失败/近期保护、规则模式不调用摘要、摘要有效/无效/非缩小、取消、零摘要预算、命令捕获流、跨轮复用、旧记录与重启只读。页面辅助服务：

~~~powershell
uv --cache-dir .uv-cache run python tests/helpers/serve_compression_fixture.py --port 18774
~~~

此服务使用脚本化模型，执行真实Runner、文件工具、回读、独立宿主验收和文件快照；专属记录在.runtime/compression-browser/runs。验证证据集中维护于WEB_CONSOLE.md。

首版尚无向量检索、子Agent隔离、可进化过滤规则或摘要语义质量保证。摘要只投影模型输入，不保存文件或进程的执行状态。全部用户要求、失败信息或单个受保护工具批次本身占满预算时仍会停止，不能宣称无限会话或已证明能力提升。

参考：[DeepSeek裁剪](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)、[DeepSeek压缩](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/compaction)、[Deep Agents](https://docs.langchain.com/oss/python/deepagents/context-engineering)、[Observation masking实证](https://arxiv.org/html/2508.21433v3)。本项目采用自己的Python实现，不声称复现其评测成绩。


## 手动技能与固定前缀（2026-10-23）

所选 SKILL.md 在启动时完整加载并固定于项目约定所在的 user 前缀；即使没有 AGENTS.md，只要有技能就仍有这条前缀。续轮移除旧宿主/项目/技能前缀，依据本轮选择重新读取，不在原历史中反复附加旧正文。压缩器按本轮前缀长度映射继承摘要，支持选择、清空及再次选择时的 2↔1 条前缀变化。

context_prepared 新增 skills 与 skill_error，RunView.context 原样聚合；正文与工具定义一起计量，不能被输出外置、遮蔽或摘要替换。加载失败保存受控原因并在调用模型前停止。宿主目录、快照字段、选择继承和大小限制见 [任务技能](SKILLS.md)。
