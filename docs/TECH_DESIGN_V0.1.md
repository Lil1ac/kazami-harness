# V0.1 技术设计

- 文档版本：0.9
- 更新日期：2026-11-01
- 状态：维护当前实现的架构与协议；交付进度统一见 PROJECT.md
- 对应需求：[PRD](PRD.md)
- 交付目标：CLI / 前端提交任务 → 模型调用工具 → 保存产物与轨迹 → 独立验收 → 展示明确结果

## 1. 已选方案与范围

| 项目 | V0.1 决策 | 理由 |
| --- | --- | --- |
| 语言与环境 | Python 3.12，uv 管理项目环境与锁文件 | 方便接入任务验收和后续优化生态，不改系统 Python |
| 模型接入 | HTTPX AsyncClient 直接调用 DeepSeek Chat Completions | 接口范围小，协议与重试行为可见，不依赖完整 Agent 框架 |
| 首个模型 | deepseek-flash，显式关闭 thinking，stream=false | 先减少接入变量；模型名称通过配置保存 |
| 运行时 | 项目内实现轻量工具循环；单任务、串行工具 | 执行、记录和结束边界易理解；以后可替换执行端 |
| 数据与参数校验 | Pydantic v2，严格校验并禁止多余工具参数 | 数据模型同时生成工具 JSON Schema，减少定义漂移 |
| CLI 与配置 | argparse、tomllib和环境变量；FastAPI任务API/SSE，React＋TypeScript＋Vite＋Ant Design前端 | 保留现有 CLI，网页入口复用同一运行核心；前端构建后由Python服务同源提供 |
| 持久化 | UTF-8 JSONL 事件日志＋原子更新的 JSON 摘要 | 适合单写入者和本地排障，V0.2 再评估数据库 |
| 测试 | pytest、asyncio.run、HTTPX MockTransport 和本地子进程 | 验证外部行为，真实模型运行作为单独检查 |

当前架构支持 CLI 与网页复用同一执行核心，组件按模型、工具、存储、验收和 Web 接入划分。运行命令见 [README](../README.md)，交付进度与路线见 [项目概览](../PROJECT.md)。

V0.1 不直接接入完整 DeepSeek Harness/Cordis，也不实现热更新、容器、恢复续跑或自动优化。Web 操作与接口细节见 [控制台说明](WEB_CONSOLE.md)，页面与验收要求见 [前端需求](FRONTEND_REQUIREMENTS.md)。保留必要接口，在有实际需求时替换组件。

## 2. 模块职责与调用方向

| 模块 | 职责 | 不应承担的职责 |
| --- | --- | --- |
| CLI | 解析参数、启动前校验、显示阶段事件和结果、处理用户中断 | 不直接组织模型与工具循环 |
| 任务 API / SSE（已实现） | 提交、查询和取消任务，订阅持久化事件，提供受限产物访问；复用 Runner | 不另建模型与工具循环，不将运行生命周期绑定到浏览器连接 |
| 前端控制台（已实现） | 调用任务接口，展示真实事件、状态和产物，提供配置与取消操作 | 不持有模型密钥，不自行判定任务执行或验收成功 |
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

网页调用主线为“前端 → 任务 API → WebRunManager → Runner”，执行事件经 Run Store 和 SSE 展示到前端。契约复用运行标识、状态和事件序号；WebRunManager 持有后台任务，浏览器连接断开不终止运行。

当前主要目录如下，只列已存在的内容：

```text
src/agent_harness/
├── cli.py、__main__.py # CLI 与 python -m 入口
├── config.py          # 运行配置加载与启动前校验
├── schemas.py         # 公共配置、运行、事件和结果契约
├── runtime/
│   ├── runner.py      # 生命周期、共享预算、取消、验收与收尾
│   ├── agent.py       # 多轮模型/工具循环
│   └── verifier.py    # 宿主独立验收
├── context/
│   ├── builder.py     # 项目约定快照与整体模型输入构建、计量
│   ├── compression.py # 分层压缩与输入投影
│   ├── archive.py     # 外置内容保存及只读回读
│   └── skills.py      # 宿主技能发现与正文冻结
├── execution/
│   ├── python.py      # 当轮解释器与项目源码目录核验
│   ├── processes.py   # 子进程、输出、超时和清理
│   ├── workspace.py   # 路径约束与原子文件提交
│   └── local.py       # 原目录身份核验及逐轮变更证据
├── storage/
│   ├── runs.py        # JSONL 运行事件、输出与摘要
│   ├── sessions.py    # 会话关联、请求幂等与历史重建
│   └── snapshots.py   # 逐轮文件快照及固定产物
├── work/
│   ├── planning.py    # 方案版本、批准和交互决定
│   ├── tasks.py       # 工作项、依赖、执行证据与租约
│   └── exports.py     # 可信计划的 Markdown 导出
├── memory/
│   ├── service.py     # 记忆修订、候选和运行读取快照
│   └── types.py       # 类型结构、触发与复核规则
├── subagents/
│   ├── service.py     # 只读串行委派及父子执行归属
│   ├── profiles.py    # 子角色与只读工具范围
│   └── reports.py     # 结构报告及证据引用核验
├── projects/          # Git 工作树、补丁与审阅
├── providers/         # 模型协议与 DeepSeek 适配
├── tools/             # 工具注册、文件与命令执行
├── web/               # HTTP/SSE、后台队列、会话及本机项目接入
└── desktop/           # Electron 后台入口、设置及原生通道
frontend/src/          # 页面、组件、API 类型与就近行为测试
desktop/src/           # Electron 主进程、预加载及原生操作
examples/
├── logs/              # Agent 可操作的固定输入和任务说明
├── skills/            # 宿主可选的 SKILL.md 流程说明
└── verifiers/         # 宿主验收脚本与 TOML
tests/
├── runtime/、context/、execution/、storage/
├── work/、memory/、subagents/、providers/、tools/
├── projects/、web/、desktop/ # 对应职责的行为与接入回归
├── helpers/           # 共享测试构建器与显式离线服务
└── test_*.py          # CLI、配置与公共契约测试
```

`harness.example.toml` 与 `web.example.toml` 保留在根目录，分别配置运行核心和 Web 宿主预设。`examples/logs/` 与 `examples/verifiers/` 分开，避免将宿主验收程序混入样例工作区。CSV、目录清单样例及 demo 入口作为可选回归素材，不是近期产品交付目标。

源码阅读可先沿 `cli.py → runtime/runner.py → runtime/agent.py` 理解主流程，再读 `providers/`、`tools/`、`execution/processes.py` 与 `storage/runs.py`；网页从 `web/api.py → web/service.py` 进入同一个 Runner。依赖、缓存和运行输出仍保留原路径，由项目级编辑器规则隐藏，完整目录职责见 [项目概览](../PROJECT.md)。

跨包依赖使用 `agent_harness.<职责包>.<模块>` 绝对导入，源码位置与调用方定位保持一致。新增包的 `__init__.py` 仅说明职责，直接从实现模块导入所需对象，避免包初始化时隐式加载整个执行链；旧平铺路径已删除。测试按相同职责分组，Web 与桌面测试继续覆盖接入链路。

每个模块解释关键机制和设计理由；类、函数和方法均提供中文职责说明，参数与返回值说明含义、单位、范围、默认值及特殊值行为，类属性和模型字段说明用途、初始状态与关联约束，变更时同步维护。前后端共享字段使用一致语义。不为单次使用的简单函数额外引入多层抽象。

## 3. CLI、配置与输入

当前 CLI 提供 run、show 与 serve：run 按配置调用文件与命令工具，完成未验收返回 3；支持 --verifier，启动前校验固定入口，未知工具或无效验收入口提前拒绝。show 只读取记录，serve 提供本地网页服务。

| 入口 | 行为 |
| --- | --- |
| harness run | 接收 task 或 task-file、workspace、可选 config 和 verifier |
| harness show RUN_ID | 只读展示历史摘要、轨迹和产物位置；不继续执行 |
| harness serve | 提供本机任务API、SSE及React构建页面，后台仍使用同一Runner |

通过 uv run 调用安装到项目环境的 harness 命令。task 与 task-file 二选一；所有路径在启动时解析为绝对路径。CLI 与网页自动收集本轮新增、修改文件，删除保留在差异；不需要声明输出路径。

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

CLI 退出码：0 为真实执行完成、验收通过且清理可确认；1 为执行失败、达到调用上限、验收未通过/异常或清理状态为 incomplete/unknown；2 为启动参数/配置错误；3 为执行完成但未验收；124 为运行超时；130 为用户/上游取消或服务关闭。取消和超时保留各自退出码，同时在摘要展示清理状态；show 的 0 仅表示读取成功，不代表原任务成功。

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
| RunResult | execution_status、validation_status、可空 verification、stop_reason、cleanup_status、计数、用量完整性、产物索引 |
| VerificationResult | status、reason、exit_code、有界stdout/stderr、elapsed_ms、中断/超限/截断标记、cleanup_status、artifacts |
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

## 6. 文件与命令工具及本地执行边界

| 工具 | 参数与结果约定 |
| --- | --- |
| read_file | path、offset、max_bytes；按字节分页返回 content、next_offset、size_bytes；可编辑大小文件另返回完整原字节 sha256 和完整换行类型 newline，超限时两者为 null；截断保持字符边界 |
| search_files | query、directory、glob、limit；首版做字面量文本搜索，返回相对路径和行号；限制扫描文件数/体积并报告跳过项，不依赖系统 rg |
| write_file | path、content、overwrite（默认 false，覆盖必须显式指定）；UTF-8 写入，超出大小限制先拒绝；同目录临时文件后原子替换，返回相对路径和修改标识 |
| edit_file | path、old_text、new_text、expected_sha256；原文唯一匹配且指纹一致才替换，返回 replacements=1、新 sha256 和 bytes_written；空 new_text 表示删除 |
| run_command | argv 字符串数组、可选相对 cwd；返回退出码、输出预览、原始输出引用、超时和清理信息 |

所有路径经解析后检查是否位于工作区内，拒绝父级越界、指向外部的符号链接和对运行记录目录的文件工具访问。写文件先检查父目录和最终目标；不能用字符串前缀判断路径归属。

run_command 使用 create_subprocess_exec 和 shell=false，不隐式解析管道或重定向；确需解释器时显式传入解释器与参数。向模型提供操作系统、工作区和可用 Python 解释器信息，避免默认假定 bash。

子进程环境从必需的系统变量白名单构建，不继承模型密钥。文件工具的路径限制不约束任意 Python/Shell 程序的访问能力，首版仍只运行可信本地任务。

read_file 的 offset/next_offset 是字节偏移，max_bytes 默认 8192、最多 65536；内容另受 tool_output_preview_chars 限制。search_files 默认最多 50 条匹配，可设至 200；最多扫描 1000 个文件、10000 个目录项，单文件 1 MiB、总计 8 MiB，跳过项与截断明确报告。write_file 使用 UTF-8，默认禁止覆盖，显式 overwrite=true 才替换，单次写入受 max_file_write_bytes 限制。

run_command 的 python/python3 映射到当前项目解释器，Windows 脚本需显式解释器启动。原始 stdout/stderr 合计受 max_process_capture_bytes 限制，模型只接收有界预览。

read_file 使用有界短同步读取，取消和超时在协作让出执行时生效，不承诺强行打断阻塞文件系统调用。文件写入的原子提交是短临界区，不能承诺撤销已经完成的写入；取消 to_thread 也不等于终止底层线程。



edit_file 的 expected_sha256 必须使用最近 read_file 或成功 edit_file 返回的完整原字节指纹，不能使用分页哈希或过期历史值。old_text 区分大小写，空白及其他字符必须保持原文。仅当整份文件统一使用 LF 或 CRLF 时，将 old_text/new_text 的 LF、CRLF 转为原文件格式后唯一匹配并替换；单行匹配后插入的多行内容也保持该格式。混合换行、仅 CR 或无换行文件按传入片段精确处理，不推断转换方式。

匹配不存在返回 edit_not_found；混合换行未命中时说明必须保留原换行。多处或重叠匹配返回 edit_ambiguous；按原格式及 BOM 处理后字节不变返回 edit_no_change。统一 LF/CRLF 文件的请求片段含独立 CR 时返回 edit_newline_mismatch，防止截断 CRLF 或制造混合换行。上述失败都发生在写临时文件之前；文件已变化仍先按 file_changed 拒绝。只处理单文件单片段，不做大小写、缩进等模糊匹配或全局替换。

原文件与编辑结果均受 max_file_write_bytes 限制（默认1 MiB）。read_file 在同一64 KiB分块扫描内计算完整SHA-256与 newline：lf、crlf、cr、mixed、none（无换行），CRLF跨块也按一对计数；此值描述整份文件，不扩大分页正文。超预算文件仍可分页，但 sha256/newline 均为null，不能使用 edit_file。原文件必须是无空字节的有效UTF-8；不可编码、二进制及超限均在提交前拒绝。编辑保留原BOM、未匹配字节与权限位；整段替换省略原BOM时也保留标记，不额外补文件末尾换行。成功后登记工作区产物，沿用逐轮固定差异与产物快照。

编辑先核对内容指纹和文件状态，写同目录临时文件并落盘，再次核对版本、对象和路径后原子替换；file_changed 要求重新读取再判断，不自动退回 overwrite=true。读取和编辑遇到变化均返回受控错误，系统异常不回显路径或输入。Windows stat/fstat 的 ctime 语义可能不同，元数据核验采用文件对象、大小、修改时间和权限位，内容版本采用SHA-256。该机制是乐观并发检查，不是对任意宿主进程的强锁：最后一次核验与替换之间仍有小窗口，沿用可信本地边界；已有 write_file 的显式整文件覆盖不因此获得版本核验。

## 7. 时限、输出采集与进程清理

Runner 用 loop.time 建立绝对截止时间，覆盖请求、退避、工具和验收。各阶段时限取自身上限与剩余总时限的较小值。HTTPX 的连接/读取等分项超时另外配置；它们不替代外层 asyncio.timeout_at 墙钟截止时间。[HTTPX 超时](https://www.python-httpx.org/advanced/timeouts/)、[Python 超时与取消](https://docs.python.org/3.12/library/asyncio-task.html)

stdout 与 stderr 必须并发、持续排空并落盘，不能先等待进程结束再读取，也不能对无界输出直接 communicate。模型只接收有截断标记的预览；在存储上限内保存完整原始输出。超过 8 MiB 时终止命令，记录 output_limit_exceeded 和已保存字节数，明确剩余输出未保存。[Python 子进程](https://docs.python.org/3.12/library/asyncio-subprocess.html)

取消、超时和输出超限共用收尾路径：停止调度 → 请求取消网络操作/终止进程 → 排空或关闭输出 → 记录清理结果 → 保存终态。清理另有最多 5 秒的宽限，不能承诺恰好在任务截止时刻退出。

- Windows：登记本次子进程 PID，根进程仍存活时通过参数数组调用 taskkill /PID <pid> /T /F；检查返回值并记录结果。默认事件循环必须支持 Windows 子进程。
- POSIX：创建独立会话/进程组，先终止，再在宽限后强制结束仍存活的同组进程。
- V0.1 不支持脱离父进程、后台守护或主动逃逸进程组的程序。Windows 任务树清理是最佳努力；根进程已退出而输出未关闭等情况标记 unknown/incomplete，不冒险操作可能复用的 PID，也不报告完整清理成功。
- 首版用“父进程仍在运行并创建阻塞子进程”的用例验收清理。强隔离和更强进程生命周期保证留给容器/后续执行器。[taskkill 官方说明](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)

取消来源与执行阶段分别记录：CLI 实际收到 SIGINT 为 user_sigint，网页主动取消为 api_user，两者的结束原因为 user_cancelled；服务关闭为 service_shutdown；上游 task.cancel 为 task_cancelled。没有取消请求却抛出取消异常时记 unexpected_cancellation：模型执行阶段记 failed，验收阶段保留 completed 并记验收 error。cancellation_received 保留阶段、取消计数和仅有文件名/函数/行号的调用位置，不保存异常正文或局部变量。

## 8. 记录存储与产物

运行记录默认保存在启动目录的 .runs/<run_id>/，启动时规范化路径；--runs-dir 可覆盖，show 应使用相同记录根目录。工作区单独指定；Web 日志样例复制到新的独立目录。

    run.json          # 任务、脱敏配置、提示词和工具声明快照
    events.jsonl      # 事实来源，单写入者追加
    summary.json      # 派生的最终结果，临时文件后原子替换
    artifacts/        # 工具和验收的 stdout/stderr、分页输出等

events 包含运行开始、模型请求开始/失败/完成、assistant 消息、工具开始/结果、验收开始/结果、运行结束。请求事件记录消息引用和计数，避免每轮复制全部历史；响应事件保留用于重建请求的完整消息字段。原始输出保存在 artifacts，事件仅引用。

开始产生副作用前，工具开始事件需写入并刷新；完成后再写结果。正常结束只写一个 run_finished。摘要可从事件重建；事件写入失败立即停止调度，在 stderr 报存储错误，不承诺磁盘故障时还能持久记录终态。

读取不完整记录时保留有效前缀并警告尾部损坏，不改写原日志、不自动续跑。V0.1 的历史读取与 V0.2 的恢复执行是不同能力。

文件产物索引由Runner统一收尾生成：运行前固定文件基线，模型/工具/验收结束后固化结束证据，登记本轮新增、修改的文件，再由唯一RunStore写入run_finished与摘要。文件工具和命令采用相同结果规则，运行内stdout/stderr引用继续独立保存。未修改的已有文件、Git控制目录、依赖缓存和运行记录不列为新交付；删除文件只进入差异，不提供不存在文件的下载。

CLI和原目录运行复用LocalChangeStore的前后快照；Git隔离以同一逐轮开始基线对比ProjectManager已有的结束文件快照，不再次复制结束目录，也不把相对会话基线的累计Git差异当成本轮产物。采集范围内的权限或内容变化都算修改；该差异描述运行期间观察到的变化，不证明文件由哪个进程写入。

RunResult.artifact_status为pending/complete/incomplete，artifact_warnings保存受控采集提示。超限、损坏或目录变化时明确收集不完整，不改写已确认的执行、验收和清理状态；失败、超时及取消后已经产生的文件也在收尾中保存。文件证据收尾在模型运行预算之后完成，受现有文件数量、大小、差异和Git命令边界约束，不因用户取消而打断正在固定的历史证据。原始记录写入失败仍传播StorageError，不补造可信终态。

用量累加已知调用的 prompt_tokens、completion_tokens、total_tokens。usage 缺失时标记 usage_complete=false，展示“已知部分＋存在缺失”；失败请求也可能已计费，因此不把已知用量当精确账单。日志不保存认证头、密钥或完整进程环境。

## 9. 独立验收协议与状态收尾

本节后端实现在 runtime/verifier.py、runtime/runner.py 和 schemas.py，日志参考入口为 examples/verifiers/verify_logs.py。验收仅在模型执行正常完成后启动，不占用模型轮次或工具调用数。启动预检不会运行验收程序。

VerifierSpec 由宿主启动时读取的 TOML 声明，包含 argv、可选 cwd 和 timeout_seconds，例如：

```toml
argv = ["python", "verify_logs.py"]
cwd = "."
timeout_seconds = 10.0
```

cwd 相对 TOML 所在目录；argv[0] 启动前解析为绝对可执行入口，裸名称可查 PATH，后续参数按验收程序解释。uv run 中的 python 来自项目环境，Windows 脚本需显式解释器。省略 timeout_seconds 时使用运行配置的 verifier_timeout_seconds，默认 30 秒。

命令只能从该清单获取；当前实现通过固定环境变量 HARNESS_WORKSPACE 传入工作区绝对路径，通过 HARNESS_RUN_ID 传入运行UUID；额外环境只允许这两个名称，不继承模型密钥。验收资源位于宿主侧，模型工具定义中没有“设置验收器”能力。

验收使用同一 Process Runner，因此同样受总时限、输出上限和清理规则约束。判定顺序为：先检查取消、超时、输出超限、启动失败以及 unknown/incomplete 清理状态；存在任一项即记为 error，即使随后读到退出码 0 也不能判通过。只有无这些异常且清理状态为 not_needed/complete 的正常退出，才按退出码映射：

| 验收结果 | validation_status |
| --- | --- |
| 未配置入口；或执行未完成而跳过 | unverified，verification_skipped.payload.reason 为 not_configured / execution_not_completed |
| 无上述异常且正常退出码 0 | passed |
| 无上述异常且正常退出码 1 | failed |
| 其他退出码、启动失败、超时、输出超限或取消 | error，附具体原因 |

宿主验收程序需区分自身错误与产物断言失败：正常断言不符返回1，实现/配置异常返回2等其他值；仅凭stderr或Python异常退出1无法自动区分。日志入口捕获内部异常并返回2。输出预览截断不单独判错，原始输出超限则为error。

RunResult.verification为当前代码的检查，未执行或修复期间为None；非空时status与顶层validation_status相同且执行已completed。verification_attempts按顺序保存每次repair_attempt（0为初次、1/2为修复后）和result。repair_attempts记录已启动修复次数。详情含reason、可空exit_code、stdout/stderr预览、elapsed_ms、超时/取消/截断标记、cleanup_status与artifacts；所有次原始输出都合入顶层产物，不覆盖旧失败。

新增事件按顺序写入，前端/接口接入时复用其语义：

| 事件 | payload与用途 |
| --- | --- |
| execution_completed | model_turns、tool_calls；仅表示模型执行已完成，不等于整个任务成功 |
| verification_started | timeout_seconds、repair_attempt、kind；开始事件落盘后才启动检查 |
| verification_result | verification、repair_attempt；包括取消/总超时后的真实输出和清理结果 |
| repair_started | repair_attempt、max_repair_attempts=2及message；公开失败反馈落盘后才可继续模型 |
| repair_stopped | reason及repair_attempts；达到修复次数或剩余调用预算时保留当前失败 |
| verification_skipped | 当前最终代码未检查时说明原因；此前检查历史和当前未验收分别保留 |
| run_finished | 唯一最终结果，仍沿用原有持久化契约 |

只有执行 completed、验收 passed 且 cleanup_status 为 not_needed/complete，才展示任务成功并返回 0。unknown/incomplete 不得返回 0；用户取消和总运行超时优先保留退出码 130/124。执行完成后验收异常，不改写原执行状态；验收阶段耗尽总时限时保留 execution_status=completed、validation_status=error，并返回 124。此前尚未完成的执行若发生取消或超时，则使用对应执行状态。

RunConfig.auto_repair默认False。VerifierSpec.kind默认为independent，结果不反馈模型；Web项目测试固定为project_tests，CLI公开测试清单须显式声明该kind。开启修复但没有公开测试在启动前拒绝。普通设置页不提供全局修复授权，页面在本次任务及后续消息中明确选择。

Agent在无工具回答处调用Runner的on_completion回调，Runner检查并决定结束或返回公开反馈。返回反馈时沿用同一消息、ContextBuilder、ContextCompressor和模型连接，不重新加载指令或重置计数。只允许正常断言失败且清理安全进入最多两次修复，异常不触发。修复开始清空当前verification/validation_status和临时final_answer，旧检查保留在verification_attempts。已无模型/工具预算则保留刚取得的失败结果并停止；修复执行中耗尽预算则当前代码未验收。最后一次检查后不再改代码，统一采集产物并写入唯一run_finished。

repair_started保存的宿主反馈包含有界公开输出及截断标记，不能提升权限。history_messages按原顺序继承该消息，中断读取保留检查历史和原始输出，不自动重放。可信本地模式不宣称验收资源能抵御恶意宿主命令篡改。

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
| 预置任务 | 日志固定输入和独立验收保留作回归；真实项目使用已有测试命令与代码审阅 | A01、A07 |
| 前后端联动（基础已验证） | 页面提交与取消、状态和事件契约、历史与重连、配置与产物访问；每次后端变更覆盖对应页面回归 | F01—F08，见前端需求 |

用脚本化 ModelClient 和 MockTransport 测确定性行为，不对类结构或内部函数调用次数做镜像测试。命令测试运行本地 Python 小程序，不依赖网络。真实模型检查单独标记并由使用者显式运行，普通测试无密钥也能通过。

至少有一个真实模型任务留下可核对产物与验收；项目工作流另验证基线、源改动保留、真实测试和审阅决定。固定响应只验证运行机制，不作为模型能力结果。

## 11. 实施顺序与完成条件

| 步骤 | 交付切片 | 本步完成条件 |
| --- | --- | --- |
| 1（已完成） | 项目环境、核心数据对象、配置和只读/追加记录 | CLI 启动校验可运行，能生成和读取一份脱敏运行记录 |
| 2（代码完成） | 一个真实模型请求＋read_file | 已通过离线配对闭环和真实 API 日志样例 |
| 3（已完成） | 其余工具、工作区和基础循环 | 真实 API 已生成并执行日志脚本，交付 report.json 与输出记录 |
| 4（已实现） | 轮次/调用限制、时限、输出采集与取消清理 | 正常、错误、超限和中断路径都有明确结果 |
| 5（部分完成） | 独立验收、日志真实闭环、任务API、SSE与基础前端已完成；扩展真实项目与代码审阅 | 已有页面提交、事件、取消、历史、配置及产物链路；项目工作流按真实Git、测试与审阅补充验收 |
| 6 | 前后端使用说明、确定性与联动测试，以及显式真实模型检查 | 用户可按文档从 CLI 或前端重跑闭环，前端 F01—F08 验收通过，普通测试不消耗模型额度 |

当前进度与下一步统一维护在 [项目概览](../PROJECT.md)，历史测试结果和页面联动证据集中在 [控制台说明](WEB_CONSOLE.md#7-已保留的验证证据)。实施表描述交付顺序，不代替最新验证记录。

每步保持入口可运行；不先把全部抽象层建完再接模型。后续切片先约定接口、字段、状态和事件，再在同一迭代中交付后端与对应前端，并验证“页面操作 → 后端执行 → 事件与结果展示”。Plan 包含前后端变更与验收，确实不影响前端的内部变更在交付时说明原因。上下文第一阶段与 [多轮任务交互](MULTITURN_TASKS.md) 已交付，沿用唯一 Runner 和事件写入者；分层历史压缩、普通消息中断接续与手动技能已实现。

真实代码任务按 [PRD 验收要求](PRD.md#44-真实代码任务验收v02) 复用当前工作台，保留实际目录、配置、差异、独立检查和人工干预证据。2026-10-26 完成本仓库源码修复、小功能及服务重启后的追加回归，证据与问题见 [真实任务验收记录](REAL_TASK_ACCEPTANCE.md)。副本采用共享 Python 环境时需明确源码导入路径，不能仅以 cwd 推断被测试代码；整体交付继续根据已确认问题处理并复验。工程测试证明机制，不代替真实代码成果验收。

有可复现问题后，再实施获批的一项受限改进，固定模型、起始代码、环境、任务和预算，分别记录基线与候选结果。沿用源码标识、每轮配置快照、事件、补丁与现有验收，不先增加预设管理器、通用插件注册表或优化管理页面。命名任务预设、通用策略组件框架与 Docker 均按实际需求单独规划；自动诊断、候选搜索和经验库后置。首版候选的修改面为提示词、工具说明或策略配置，宿主验收与预算保持独立。

## 12. 参考契约

本文技术取舍属于项目实施方案，不代表参考项目的完整复刻或效果复现。

- [DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。
- [HTTPX 异步客户端](https://www.python-httpx.org/async/)、[超时](https://www.python-httpx.org/advanced/timeouts/)。
- [Python 3.12 取消与超时](https://docs.python.org/3.12/library/asyncio-task.html)、[子进程](https://docs.python.org/3.12/library/asyncio-subprocess.html)。
- [Pydantic 严格校验](https://docs.pydantic.dev/latest/concepts/strict_mode/)。
- [Windows taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)、[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

## 13. 本地 Web 接入实现

`web/service.py` 的 WebRunManager 在应用生命周期内持有唯一后台任务，预检成功且收到Runner.on_context后返回202。on_context只交出只读使用的实时上下文，页面无法更改其状态。明确的网页取消设api_requested，服务关闭设shutdown_requested，Runner分别记录api_user/service_shutdown，沿用统一进程清理与终态提交。

页面刷新或EventSource关闭只释放订阅。SSE每条数据携带现有seq，前端按运行ID和序号去重、补缺口、重连；stream_end前再次检查最新序号，避免发送旧页期间新写入终态被漏掉。任务快照由TanStack Query读取，终态通知后重新查询持久化事实。历史读取用受限签名缓存减少重复解析。

Uvicorn单worker不启reload，Windows使用支持子进程的asyncio循环；HTTP/SSE优雅关闭宽限2秒后进入lifespan，取消并等待Runner清理，避免SSE一直阻挡服务退出。配置、HTTP字段、64KiB产物预览及开发/构建部署方式见 [控制台说明](WEB_CONSOLE.md)。


## 14. 真实项目任务与代码审阅

`projects/models.py` 定义宿主项目与审阅契约，`projects/manager.py` 负责固定基线、detached worktree、身份核验、包含未跟踪文件的完整 Git 补丁和独立审阅元数据。WebRunManager 在唯一执行槽中组合工作树准备、现有 Runner、宿主项目测试和最终代码快照；快照结束后才释放槽，SSE 不将 run_finished 与审阅是否准备好混淆。

VerifierSpec 增加宿主可选 `python_paths`，默认空列表表示不额外设置源码搜索目录。非空时要求显式 cwd，路径必须绝对且位于 cwd 内；TOML 输入相对验收 cwd 解析。ProcessRunner 对该专用参数单独校验后构造子进程 PYTHONPATH，继续拒绝经 extra_env 注入任意环境变量，不继承模型密钥或宿主 Python 路径。项目测试 cwd 为本次工作树，避免可编辑安装误测源代码。

执行 JSONL 保持唯一写入者；审阅元数据与完整补丁另存运行根目录 `.projects/<uuid>/`。运行、测试、审阅状态相互独立，取消后仍尝试保存已有改动。服务重启只恢复读取和审阅，不自动恢复模型或重放工具。保留不改源分支，丢弃前核验登记目录、Git注册和当前快照，记录/补丁继续保留。完整使用和HTTP契约见 [代码任务与审阅](PROJECT_TASKS.md)。


## V0.2 扩展：上下文管理第一阶段

Agent 每轮通过 ContextBuilder 构建独立请求副本：启动时固定根 AGENTS.md，完整保留消息及工具定义，按 json_utf8_v1 检查整体输入字节预算，允许后才增加实际模型轮次并调用适配器。Runner 继续负责状态收尾，Store 继续作为唯一事件写入者。指令错误停止为 failed，预算超限停止为 limit_exceeded，不把被拦截的拟请求算作实际请求。

完整配置、消息优先级、指令异常、输入计量、context_prepared/context_checked事件及RunView.context契约集中在 [上下文管理](CONTEXT_MANAGEMENT.md)。原始轨迹继续保存；分层历史压缩与普通消息中断接续已交付，Docker 属于尚未实现的后续可选执行后端，不作为 V0.2 完成条件。


## V0.2 扩展：多轮任务交互

ConversationService 在既有 WebRunManager 单槽中准备后续运行，SessionStore 原子保存线性会话与请求意图。每条要求创建新 RunManifest，通过 session_id、parent_run_id、turn_index 关联；Runner 生命周期与 RunStore 唯一事件写入者不变。Agent 启动时再次核对前轮事件序号与历史摘要，移除旧宿主前缀后通过 ContextBuilder 生成新输入。

首次 Git 隔离运行从指定提交建立会话工作树；后续 prepare_turn 只新增本轮证据并复用目录，不创建工作树或应用旧补丁。两轮间手动修改保留，各轮配置、预算、工具轨迹、项目测试、固定产物和相对会话基线的累计补丁分别留存；旧轮证据不重新捕获。 只有最新轮可保留或丢弃共享工作树，旧轮只读，运行中拒绝删除。前轮 kept 不代表新轮已经接受；丢弃后不能继续，但已保存历史文件和补丁仍可下载。不自动提交或合并。新轮尚未启动时准备失败，仅回退会话管理状态，已有目录保持。 协议见 [多轮任务](MULTITURN_TASKS.md)。


前端会话工作区以session_id保持React挂载和会话查询身份，以run_id分别读取独立执行证据。消息展示与所选检查轮解耦，输入始终使用最新轮；确认POST后缓存真实新轮并替换路由，SSE维持最新轮唯一订阅。Markdown渲染只影响显示，原始回答、工具、验收与JSONL契约不变；完整模块关系见 [多轮任务交互第8节](MULTITURN_TASKS.md#8-会话工作区实现)。

## 分层上下文扩展（2026-10-22）

ContextBuilder与ContextCompressor构建有来源的输入副本，ContextArchive保存不可变正文与只读引用。成功大结果外置，压力下遮蔽旧成功观察，再按需生成结构化工作摘要；固定保留规则、用户原文、失败和近期完整批次。摘要共用模型/HTTP/用量/超时预算，原始执行消息保持完整；前轮摘要经核验后复用，来源缺失明确失败。RunView从完整事件聚合变换，设置与检查器同步维护。协议和容量见[上下文管理](CONTEXT_MANAGEMENT.md)，不代表中断恢复或自优化已经交付。


## 中断后普通消息接续（2026-10-23）

ConversationService沿用followups、新运行和逐轮独立工作区。用户明确提交后，宿主核对中断前缀及来源，捕获一次当前代码，再派生下一轮；HistorySource固定末序号、重建消息摘要及interrupted标记，Agent在调用模型前再次核对。原运行清单与JSONL不修改。

Sessions只为最后一批缺失工具结果补充未知或未开始标记，要求先核对当前现场；已完成工具不会由宿主重放。ContextArchive仅在同会话来源序号和摘要匹配时允许祖先正文回读。每条新消息使用独立预算并重新进行宿主验收与代码审阅。

独立检查点模块、Runner回调、恢复协议和前端组件已删除；代码仅维护普通消息接续。Git新副本在应用累计补丁前按索引原字节检出，以兼容LF提交、CRLF检出以及既有CRLF提交。完整协议与限制见[多轮任务交互第10节](MULTITURN_TASKS.md#10-中断后直接发送继续2026-10-23)。


运行与会话格式仅保留逐用户轮次。磁盘读取对普通旧记录的空元数据做受限清理，普通请求的冗余轮次核对线性关系后移除，实际非空恢复来源仍按严格未知字段拒绝；该读取过程不写盘，新模型、API与记录不再输出废弃字段。宿主进程、外部副作用及旧检查点尝试不做迁移。


## 手动技能加载（V0.2）

context/skills.py 只负责从宿主配置的 skill_roots 有界发现技能并冻结显式选择的正文；Agent 启动时加载一次，context_prepared 保存原文和指纹。ContextBuilder 把技能和项目约定合并为同一条固定 user 前缀，避免更改原有 conversation_start 的 1/2 条前缀协议；跨轮摘要偏移同时考虑技能存在。固定正文参与输入计量，受压缩保护。

RunConfig 的 skills 保存当前选择，浏览器 RunConfigPatch 只能覆盖名称数组；目录来源由宿主控制，续轮重新使用当前宿主登记，选择默认沿用前轮。WebRunManager.options 提供可选目录及禁用原因，详情通过既有上下文视图展示实际快照。PyYAML safe_load 解析 frontmatter，正文不解释为代码，不执行附属脚本。模块限额、配置与字段见 [任务技能](SKILLS.md)。

## 当轮 Python 执行配置（2026-10-27）

实际工作区确定后生成 PythonExecution，记录项目设置或 PATH 解析的解释器与共享源码目录。Runner落盘前核验，命令与验收使用相同项目源码范围；ProcessRunner仅对各自子进程注入PYTHONPATH，命令cwd可以是工作区子目录。独立验收的私有依赖仍在其自身cwd内单独校验，不传给模型命令。

项目配置python_paths省略时自动根目录/已有src，空列表只用根目录；隔离工作树及续轮按当轮实际路径重新生成，旧快照不改写。详情页从持久记录展示默认Python与源码目录；工具声明如实描述项目解释器与 PATH。数据、边界和配置见 [Python源码导入](PYTHON_EXECUTION.md)。

## Electron 桌面宿主（2026-10-30）

`desktop/src/main.ts` 管理单实例、窗口、菜单、会话 Cookie 和后台子进程；预加载只暴露正常退出和固定错误通知。React 保留同一套页面与 HTTP/SSE 契约。`desktop/src/backend.ts` 只通过标准输入发送私有启动凭据，按 ready 消息等待随机端口就绪；运行记录仍由 Python Runner 唯一写入。

`src/agent_harness/desktop/server.py` 启动独立后台，`bridge.py` 以专用输入线程配对原生请求与异步结果。主进程只接受固定能力白名单，后台先验证产物引用和真实快照再请求文件操作。父进程发送 shutdown 或输入 EOF 时，后台进入现有 lifespan/Runner 取消与清理流程。Git、命令及验收进程与控制 stdin 隔离，Windows 辅助控制台隐藏。

公开设置和窗口状态分别原子保存，密钥只在主进程使用系统加密后保存。桌面 HTTP 拒绝无会话访问及跨源写入，页面使用 HttpOnly Cookie；会话令牌不进入 argv、URL 或日志。开发与发布的资源路径、运行目录、模块边界及验收见 [桌面端说明](DESKTOP.md)。
