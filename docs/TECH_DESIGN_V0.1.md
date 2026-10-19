# V0.1 技术设计

- 文档版本：0.1
- 更新日期：2026-10-19
- 状态：第二切片已实现并通过离线闭环验证；其余为待实施方案
- 对应需求：[PRD](PRD.md)
- 交付目标：CLI 提交任务 → 模型调用工具 → 保存产物与轨迹 → 独立验收 → 输出明确结果

## 1. 已选方案与范围

| 项目 | V0.1 决策 | 理由 |
| --- | --- | --- |
| 语言与环境 | Python 3.12，uv 管理项目环境与锁文件 | 方便接入任务验收和后续优化生态，不改系统 Python |
| 模型接入 | HTTPX AsyncClient 直接调用 DeepSeek Chat Completions | 接口范围小，协议与重试行为可见，不依赖完整 Agent 框架 |
| 首个模型 | deepseek-flash，显式关闭 thinking，stream=false | 先减少接入变量；模型名称通过配置保存 |
| 运行时 | 项目内实现轻量工具循环；单任务、串行工具 | 执行、记录和结束边界易理解；以后可替换执行端 |
| 数据与参数校验 | Pydantic v2，严格校验并禁止多余工具参数 | 数据模型同时生成工具 JSON Schema，减少定义漂移 |
| CLI 与配置 | argparse、tomllib 和环境变量 | 首版无需 Web、命令框架或数据库服务 |
| 持久化 | UTF-8 JSONL 事件日志＋原子更新的 JSON 摘要 | 适合单写入者和本地排障，V0.2 再评估数据库 |
| 测试 | pytest、asyncio.run、HTTPX MockTransport 和本地子进程 | 验证外部行为，真实模型运行作为单独检查 |

已使用本机 Python 3.12.1 创建独立项目环境，安装 Pydantic、HTTPX 和 pytest。第二切片已接入真实 DeepSeek 请求协议与 read_file，完成离线多轮、限制和取消测试；已于 2026-10-19 使用真实 API 验证日志样例与读取工具闭环。当前状态和运行命令见 [README](../README.md)。

V0.1 不直接接入完整 DeepSeek Harness/Cordis，也不实现热更新、容器、恢复续跑、前端控制台或自动优化。前端控制台是 V0.2 首批必须交付内容，在本版执行与验收闭环后接入任务 API 和 SSE；具体页面与验收范围见 [前端需求](FRONTEND_REQUIREMENTS.md)。保留必要的模型、工具和记录接口；后续在有实际需求时替换组件。

## 2. 模块职责与调用方向

| 模块 | 职责 | 不应承担的职责 |
| --- | --- | --- |
| CLI | 解析参数、启动前校验、显示阶段事件和结果、处理用户中断 | 不直接组织模型与工具循环 |
| Runner | 创建运行记录，管理总时限，调用 Agent Loop 和 Verifier，统一收尾 | 不实现模型协议或具体工具 |
| Agent Loop | 维护本次消息历史，组织模型轮次和工具批次，判断执行终止 | 不访问密钥、不直接创建子进程 |
| Model Client | 构造 DeepSeek 请求，解析响应、用量与协议错误，实施有界 HTTP 重试 | 不执行工具、不判产物正确 |
| Tool Registry / Executor | 注册工具定义，解析参数，校验权限与额度，返回统一工具结果 | 不自行发起下一轮模型请求 |
| Workspace / Process Runner | 文件路径检查、受限读写、进程启动、输出采集与清理 | 不决定模型策略 |
| Run Store | 单写入者追加事件、保存输出和配置快照、生成摘要 | 不重放外部动作 |
| Verifier | 执行宿主预先指定的验收入口，映射验收状态 | 不接受模型临时指定的评分命令 |

调用主线：

CLI → Runner → Agent Loop → Model Client / Tool Executor。
Tool Executor 和 Verifier 共用 Process Runner；各模块通过 Run Store 记录事件。
Runner 是运行生命周期的唯一协调者，避免 CLI、工具和模型客户端分别修改最终状态。

模块目录如下；providers、agent、runner 和只读文件工具已实现，其他工具与进程/验收模块仍属规划：

    src/agent_harness/
      cli.py          # 命令入口和结果显示
      schemas.py      # 配置、任务、运行结果与事件的数据结构和校验规则
      config.py       # 配置加载、校验和脱敏快照
      runner.py       # 生命周期、验收与收尾
      agent.py        # 多轮模型—工具循环
      providers/
        base.py       # 最小模型协议
        deepseek.py   # 单供应商 REST 适配
      tools/
        registry.py   # Schema、参数校验和分派
        files.py      # 读取、搜索和写入
        command.py    # 命令工具
      workspace.py    # 路径边界与文件提交
      processes.py    # 子进程、输出、超时和清理
      store.py        # JSONL 和摘要
      verifier.py     # 验收入口与退出码协议
    examples/
      csv/
      logs/
      inventory/
    tests/

每个模块解释关键机制和设计理由；不为单次使用的简单函数额外引入多层抽象。

## 3. CLI、配置与输入

完整 V0.1 计划提供三个入口。当前提供 run、run --prepare-only 与 show：普通 run 支持 DeepSeek 只读文件任务，完成未验收返回 3；prepare-only 只创建 created/unverified 记录，不读取密钥或执行模型、工具、验收，返回 0 仅表示准备操作完成。普通 run 在启动前拒绝未实现工具与 --verifier；demo 待后续实现。

| 入口 | 行为 |
| --- | --- |
| harness run | 接收 task 或 task-file、workspace、可选 config 和 verifier |
| harness show RUN_ID | 只读展示历史摘要、轨迹和产物位置；不继续执行 |
| harness demo csv / logs / inventory | 复制预置输入到新的独立工作区，再走同一 Runner |

通过 uv run 调用安装到项目环境的 harness 命令。task 与 task-file 二选一；所有路径在启动时解析为绝对路径。demo 永不覆盖已有工作区，验收资源保留在宿主侧。

配置使用 TOML，合并顺序为“内置默认值 → 显式配置文件 → CLI 覆盖”。模型密钥只读取 DEEPSEEK_API_KEY；配置和日志只保存环境变量名称，不保存值。V0.1 只支持已实现的 DeepSeek 适配器，不把任意兼容接口当作已验证能力。

建议默认值，均属于项目设计值而非供应商限制：

| 参数 | 默认值 |
| --- | --- |
| max_model_turns | 20 |
| max_tool_calls | 40 |
| model_max_output_tokens | 4096 |
| max_http_attempts_per_turn | 3（含首次） |
| model_turn_timeout_seconds | 60（含该轮重试和退避） |
| command_timeout_seconds | 30 |
| verifier_timeout_seconds | 30 |
| run_timeout_seconds | 300（包含验收阶段） |
| cleanup_grace_seconds | 5 |
| tool_output_preview_chars | 12000 |
| max_process_capture_bytes | 8 MiB（单次 stdout＋stderr 合计） |
| max_file_write_bytes | 1 MiB |

启动前验证工作目录、配置范围、工具声明、模型密钥、验收入口及存储可写性；任意失败时不发模型请求、不创建半启动运行。创建运行目录失败作为启动错误返回。

CLI 退出码：0 为执行完成且验收通过；1 为执行失败、达到调用上限、验收未通过/异常或清理状态为 incomplete/unknown；2 为启动参数/配置错误；3 为执行完成但未验收；124 为运行超时；130 为用户或上游任务取消。取消和超时保留各自退出码，同时在摘要展示清理状态。

## 4. 核心数据契约

所有持久化对象含 schema_version=1。项目生成的 run_id、attempt_id 等使用 UUID；供应商的 response_id、tool_call_id 保留原值，不要求 UUID 格式。事件序号在单次运行内递增；持久时间使用 UTC，时限计算使用单调时钟。

| 对象 | 必需内容 |
| --- | --- |
| TaskSpec | prompt、workspace、可选的宿主 VerifierSpec |
| RunConfig | provider、requested_model、工具集合、默认提示词、限制；无密钥 |
| ToolCall | provider_call_id、name、arguments_json 原字符串 |
| ToolResult | call_id、ok、data、error、artifact_refs、truncated；命令附 exit_code、timed_out 和清理结果 |
| Message | role、content（assistant 发起工具调用时允许为空）；assistant 可含 tool_calls，tool 必须含 tool_call_id；预留 reasoning_content |
| ModelResponse | 完整 assistant 消息、finish_reason、response_model、可选 system_fingerprint、可空 usage |
| RunResult | execution_status、validation_status、stop_reason、cleanup_status、计数、用量完整性、产物索引 |
| RunEvent | run_id、seq、time_utc、elapsed_ms、type、关联请求/工具标识、payload |

execution_status 取 created、running、completed、failed、cancelled、timed_out、limit_exceeded。validation_status 取 unverified、passed、failed、error。cleanup_status 取 not_needed、complete、incomplete、unknown。

历史记录缺少终止事件时，由读取端展示 interrupted=true；不伪造原进程写过 cancelled，也不据此推断副作用是否完成。

接口保持最小：ModelClient.complete 接收模型请求并返回 ModelResponse；ToolExecutor.execute 接收调用和运行上下文并返回 ToolResult；Verifier.run 返回验收结果；RunStore.append 追加事件。计时、取消信号与存储对象通过显式运行上下文传入。

## 5. 模型协议与执行循环

### 5.1 单模型接入

请求地址为 https://api.deepseek.com/chat/completions，使用 Bearer 认证、model=deepseek-flash、stream=false，并在请求 JSON 顶层显式设置 thinking.type=disabled。仅传当前适配器已支持的参数。

截至 2026-10-18，官方文档将 deepseek-flash 对应到 DeepSeek-V4.1-Flash-1017。该名称是服务别名，不保证未来权重固定；每次响应记录实际 model、system_fingerprint（存在时）、调用时间和运行配置，后续做实验时另处理版本漂移。[模型文档](https://api-docs.deepseek.com/quick_start/pricing/)

内部消息保留供应商返回的必要字段，不能只抽取可见文本。若将来开启 thinking，带工具的后续请求需完整回传历史 reasoning_content；V0.1 预留字段但不开启该功能。[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)

同一运行复用一个 HTTPX AsyncClient，禁止在每一轮新建连接池。401/403、无效请求和协议错误不重试；连接失败、429 和选定的暂时性服务错误可在剩余时限内退避重试。HTTP 尝试不重复增加逻辑模型轮次，但单独计数。读取超时后的重试可能导致重复计费，不能声称请求只被服务端执行一次。

### 5.2 消息配对与批量调用

1. 初始化 system 和 user 消息，写入配置、提示词和工具声明快照。
2. 检查取消、总时限和轮次额度；登记 model_request_started，再发请求。
3. 校验并保存完整响应。有效工具批次必须有唯一、非空的 call_id。
4. 先追加完整 assistant 消息，再按响应顺序串行执行每个工具。
5. 每个调用产生且仅产生一条配对结果；未知工具、非法 JSON、多余参数也返回结构化错误。发送给供应商的 tool 消息必须携带对应 tool_call_id，content 是 ToolResult 序列化后的 JSON 字符串。
6. 全批工具结果配齐后才发下一次模型请求。串行执行不等于只支持一个调用。
7. 无工具调用、正常结束原因且存在有效最终文本时，标记执行完成，交给 Runner 决定是否验收。

finish_reason=length、被过滤、空响应、重复调用标识或无法可靠解析的消息判为明确协议/完成错误。截断或不完整的工具调用不得执行。[工具调用文档](https://api-docs.deepseek.com/guides/tool_calls/)

max_model_turns 计算逻辑请求轮次；max_tool_calls 计算被模型提出且接纳处理的调用，参数错误和未知工具也占用额度。若整批超过剩余额度，整批不执行，逐一记录 skipped_limit 结果并终止；运行已进入终态时不会再向模型补发请求。取消时尚未执行的批次成员记录 skipped_cancelled，已经执行的成员保留真实结果。

工具业务错误反馈模型继续；配置、协议和记录存储错误终止运行。进程清理为 unknown/incomplete 时视为不可恢复的宿主错误，立即停止后续工具调度；已出现的清理问题不得被后续正常结果覆盖。模型客户端只可重试请求，不得连带重做已执行工具。

## 6. 四类工具与本地执行边界

| 工具 | 参数与结果约定 |
| --- | --- |
| read_file | path、offset、max_bytes；按字节分页读取 UTF-8 文本，返回 next_offset，截断保持完整字符边界；不支持的编码返回明确错误 |
| search_files | query、directory、glob、limit；首版做字面量文本搜索，返回相对路径和行号；限制扫描文件数/体积并报告跳过项，不依赖系统 rg |
| write_file | path、content、overwrite（默认 false，覆盖必须显式指定）；UTF-8 写入，超出大小限制先拒绝；同目录临时文件后原子替换，返回相对路径和修改标识 |
| run_command | argv 字符串数组、可选相对 cwd；返回退出码、输出预览、原始输出引用、超时和清理信息 |

所有路径经解析后检查是否位于工作区内，拒绝父级越界、指向外部的符号链接和对运行记录目录的文件工具访问。写文件先检查父目录和最终目标；不能用字符串前缀判断路径归属。

run_command 使用 create_subprocess_exec 和 shell=false，不隐式解析管道或重定向；确需解释器时显式传入解释器与参数。向模型提供操作系统、工作区和可用 Python 解释器信息，避免默认假定 bash。

子进程环境从必需的系统变量白名单构建，不继承模型密钥。文件工具的路径限制不约束任意 Python/Shell 程序的访问能力，首版仍只运行可信本地任务。

当前 read_file 使用有界短同步读取，取消和超时在异步检查点生效，不承诺强行打断阻塞文件系统调用。本地小文件操作限制单次体积；未来文件写入的原子提交是短临界区，取消在下一检查点生效，不能承诺撤销已经完成的写入。避免把取消 to_thread 等同于终止底层线程。

## 7. 时限、输出采集与进程清理

Runner 用 loop.time 建立绝对截止时间，覆盖请求、退避、工具和验收。各阶段时限取自身上限与剩余总时限的较小值。HTTPX 的连接/读取等分项超时另外配置；它们不替代外层 asyncio.timeout_at 墙钟截止时间。[HTTPX 超时](https://www.python-httpx.org/advanced/timeouts/)、[Python 超时与取消](https://docs.python.org/3.12/library/asyncio-task.html)

stdout 与 stderr 必须并发、持续排空并落盘，不能先等待进程结束再读取，也不能对无界输出直接 communicate。模型只接收有截断标记的预览；在存储上限内保存完整原始输出。超过 8 MiB 时终止命令，记录 output_limit_exceeded 和已保存字节数，明确剩余输出未保存。[Python 子进程](https://docs.python.org/3.12/library/asyncio-subprocess.html)

取消、超时和输出超限共用收尾路径：停止调度 → 请求取消网络操作/终止进程 → 排空或关闭输出 → 记录清理结果 → 保存终态。清理另有最多 5 秒的宽限，不能承诺恰好在任务截止时刻退出。

- Windows：登记本次子进程 PID，根进程仍存活时通过参数数组调用 taskkill /PID <pid> /T /F；检查返回值并记录结果。默认事件循环必须支持 Windows 子进程。
- POSIX：创建独立会话/进程组，先终止，再在宽限后强制结束仍存活的同组进程。
- V0.1 不支持脱离父进程、后台守护或主动逃逸进程组的程序。Windows 任务树清理是最佳努力；根进程已退出而输出未关闭等情况标记 unknown/incomplete，不冒险操作可能复用的 PID，也不报告完整清理成功。
- 首版用“父进程仍在运行并创建阻塞子进程”的用例验收清理。强隔离和更强进程生命周期保证留给容器/后续执行器。[taskkill 官方说明](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)

## 8. 记录存储与产物

运行记录默认保存在启动目录的 .runs/<run_id>/，启动时规范化路径；工作区单独指定，demo 使用新的独立目录。

    run.json          # 任务、脱敏配置、提示词和工具声明快照
    events.jsonl      # 事实来源，单写入者追加
    summary.json      # 派生的最终结果，临时文件后原子替换
    artifacts/        # 工具和验收的 stdout/stderr、分页输出等

events 包含运行开始、模型请求开始/失败/完成、assistant 消息、工具开始/结果、验收开始/结果、运行结束。请求事件记录消息引用和计数，避免每轮复制全部历史；响应事件保留用于重建请求的完整消息字段。原始输出保存在 artifacts，事件仅引用。

开始产生副作用前，工具开始事件需写入并刷新；完成后再写结果。正常结束只写一个 run_finished。摘要可从事件重建；事件写入失败立即停止调度，在 stderr 报存储错误，不承诺磁盘故障时还能持久记录终态。

读取不完整记录时保留有效前缀并警告尾部损坏，不改写原日志、不自动续跑。V0.1 的历史读取与 V0.2 的恢复执行是不同能力。

产物索引来自 write_file 和预先声明的预期输出。run_command 可创建任意文件，V0.1 不声称自动识别所有文件修改；没有被索引的成果仍可在工作区查看。

用量累加已知调用的 prompt_tokens、completion_tokens、total_tokens。usage 缺失时标记 usage_complete=false，展示“已知部分＋存在缺失”；失败请求也可能已计费，因此不把已知用量当精确账单。日志不保存认证头、密钥或完整进程环境。

## 9. 独立验收协议与状态收尾

VerifierSpec 是由宿主启动时读取的 TOML 文件，包含 argv、可选 cwd 和 timeout_seconds。命令只能从该清单获取；工作区通过固定参数或 HARNESS_WORKSPACE 环境变量传入，运行标识通过 HARNESS_RUN_ID 传入。验收资源位于宿主侧，模型工具定义中没有“设置验收器”能力。

验收使用同一 Process Runner，因此同样受总时限、输出上限和清理规则约束。判定顺序为：先检查取消、超时、输出超限、启动失败以及 unknown/incomplete 清理状态；存在任一项即记为 error，即使随后读到退出码 0 也不能判通过。只有无这些异常且清理状态为 not_needed/complete 的正常退出，才按退出码映射：

| 验收结果 | validation_status |
| --- | --- |
| 未配置入口；或执行未完成而跳过 | unverified，并记录 skipped_reason |
| 无上述异常且正常退出码 0 | passed |
| 无上述异常且正常退出码 1 | failed |
| 其他退出码、启动失败、超时、输出超限或取消 | error，附具体原因 |

只有执行 completed、验收 passed 且 cleanup_status 为 not_needed/complete，才展示任务成功并返回 0。unknown/incomplete 不得返回 0；用户取消和总运行超时优先保留退出码 130/124。执行完成后验收异常，不改写原执行状态；验收阶段耗尽总时限时保留 execution_status=completed、validation_status=error，并返回 124。此前尚未完成的执行若发生取消或超时，则使用对应执行状态。

完成验收与保存结果前不再调度模型修改工作区，保证验收对应最终交付版本。V0.1 只做一次验收，不自动启动修复循环；这是后续可插拔完成策略的扩展点。可信本地模式不宣称验收资源能抵御恶意宿主命令篡改。

## 10. 测试范围与 PRD 对应

| 测试组 | 关键行为 | 对应 PRD |
| --- | --- | --- |
| 启动和配置 | 无效路径、缺少密钥、验收入口无效；不发 HTTP 请求 | A02 |
| 消息和模型协议 | 同轮多调用配对、保留扩展字段、未知工具、非法 JSON、截断响应、重复 ID、重试次数 | A03、A05 |
| 工具与文件 | 越界路径、UTF-8 分页、原子写入、输出预览引用、命令退出码 | A03、A08 |
| 运行控制 | 达到轮次/调用上限、整批超额、请求超时、取消时不再执行下一工具 | A04—A06 |
| 子进程 | 父子进程阻塞、stdout/stderr 同时输出、无限输出、清理失败状态 | A04、A06、A08 |
| 验收 | 退出码 0/1/其他、无入口、启动失败、验收耗尽剩余预算、保留执行完成状态 | A07、A09 |
| 记录和 CLI | 事件顺序、无明文密钥、摘要与事件一致、无终止事件、历史只读、退出码 | A06、A08 |
| 三个预置任务 | 固定输入和独立验证；参考结果确实通过，错误结果确实失败 | A01、A07 |

用脚本化 ModelClient 和 MockTransport 测确定性行为，不对类结构或内部函数调用次数做镜像测试。命令测试运行本地 Python 小程序，不依赖网络。真实模型检查单独标记并由使用者显式运行，普通测试无密钥也能通过。

至少有一个真实模型端到端样例通过；其余样例可独立运行和评分。固定响应只验证运行机制，不作为模型能力结果。

## 11. 实施顺序与完成条件

| 步骤 | 交付切片 | 本步完成条件 |
| --- | --- | --- |
| 1（已完成） | 项目环境、核心数据对象、配置和只读/追加记录 | CLI 启动校验可运行，能生成和读取一份脱敏运行记录 |
| 2（代码完成） | 一个真实模型请求＋read_file | 已通过离线配对闭环和真实 API 日志样例 |
| 3 | 其余工具、工作区和基础循环 | Agent 可生成小型脚本并运行，交付第一个样例产物 |
| 4（部分完成） | 已有轮次/调用限制与异步时限/取消；进程输出及清理待实现 | 正常、错误、超限和中断路径都有明确结果 |
| 5 | 独立验收、最终摘要和另外两个样例 | 达到 PRD A01—A09，验收状态与执行状态清晰分离 |
| 6 | 使用说明、确定性测试和显式真实模型检查 | 用户可按文档重跑闭环，普通测试不消耗模型额度 |

每步保持入口可运行；不先把全部抽象层建完再接模型。V0.1 完成后再按 PRD 推进上下文、恢复和网页；自优化的修改面将落在提示词、工具声明和策略配置，宿主验收与预算保持独立。

## 12. 参考契约

本文技术取舍属于项目实施方案，不代表参考项目的完整复刻或效果复现。

- [DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。
- [HTTPX 异步客户端](https://www.python-httpx.org/async/)、[超时](https://www.python-httpx.org/advanced/timeouts/)。
- [Python 3.12 取消与超时](https://docs.python.org/3.12/library/asyncio-task.html)、[子进程](https://docs.python.org/3.12/library/asyncio-subprocess.html)。
- [Pydantic 严格校验](https://docs.pydantic.dev/latest/concepts/strict_mode/)。
- [Windows taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)、[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
