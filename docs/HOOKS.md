# 生命周期扩展与 Hooks

Hooks 由宿主在确定节点执行项目脚本，适用于格式化、项目检查和动态上下文。权限、Plan、预算、记忆规则、取消、进程清理和独立验收继续由核心层负责，不能通过关闭 Hook 绕过。当前没有操作系统命令沙箱，脚本按本机用户权限运行。

## 宿主生命周期组件

[Agent Loop](../src/agent_harness/runtime/agent.py) 编排模型轮次、工具批次与交付阶段。[组件注册表](../src/agent_harness/runtime/assembly.py) 决定各角色在这些位置执行哪些策略。输入投递、后台命令和脚本队列、团队、委派、记忆和压缩、审批接续与交付修复各自维护。CLI、网页、自动化及子 Agent 共用这套入口。TOML Hooks 继续使用下文的脚本协议与审核流程。

| 阶段 | 可接入的策略 |
| --- | --- |
| 启动与接续 | 装入团队事实、校验启动条件、接续已批准的动作 |
| 准备模型输入 | 投递新要求与后台报告、选择记忆、压缩、投影交接输入 |
| 确认模型响应 | 确认本次已投递的用户和团队输入 |
| 工具前、工具后、整批结束 | 检查新要求、根据回执决定是否等待或重试，配齐回执后建立安全检查点 |
| 检查候选与交付前 | 等待在途成果、运行 Stop 或其他交付前检查 |
| 独立验收与验收后确认 | 执行宿主验收或子交接协议，再处理修复与新输入 |

新增宿主策略时实现接收当前段状态的异步组件，在注册表声明标识、阶段、角色、顺序依赖和所读写的事实。需要加入上下文的策略在压缩前写入完整历史；只修改发送副本的策略在投影后声明读取和写入 `input`。结束前的项目修改须发生在独立验收之前，验收后的策略可读取结果并请求下一轮。

组件返回 `Proceed` 继续、`Replan` 重决策、`Wait` 等待，或 `Abort` 终止。宿主恢复已批准的交付动作时可返回 `Finish`，仍会经过必需的完成阶段。内核统一处理剩余工具回执、反馈持久化和终态。最终模型输入按实际发送副本重新计量；辅助摘要与执行共享同一模型请求网关和父子预算。工作区安全与验收后禁止写入属于宿主组件契约，注册表只供受信任的项目源码维护。

运行详情的「诊断 → 生命周期组件」显示各阶段最近一次实际执行的组件、来源和结果。完整顺序可在执行轨迹中查看。刷新和查看历史时从已保存事件恢复。

## 配置与审核

宿主 TOML 的 `hooks.rules` 与实际工作区 `.agents/hooks.toml` 的 `[[hooks]]` 使用同一规则模型。宿主先、项目后，同来源编号唯一；规则串行运行，不自动安装依赖。页面只选择已登记项目并提供读取、重新加载、审核与启停，详细规则由 TOML 维护。

```toml
[[hooks]]
id = "format-python"
event = "PostToolUse"
argv = ["python", "hooks/format_python.py"]
tools = ["write_file", "edit_file"]
paths = ["*.py"]
timeout_seconds = 10.0
on_failure = "block"
```

`argv` 是显式参数数组，不经过隐式 shell。Python 使用当轮项目解释器，PowerShell 通过显式 `pwsh -NoProfile -File ...` 启动。`cwd` 默认 `.`，必须在实际工作区内；工具名与路径使用 glob，空列表匹配全部。`agent_scopes` 默认 `["root"]`，开发子项需显式加入 `develop`，只读子项不运行外部命令。

设置页与项目设置中的 Hooks 区域展示定义、源码预览、入口内容摘要及上次审核内容。审核绑定配置、工作区身份、显式入口脚本内容和解析程序路径；定义或脚本变化后旧审核失效。源码预览每文件最多16KiB，完整入口最多2MiB；脚本的传递依赖不是静态冻结对象。

CLI 可使用同一审核记录：

```powershell
harness hooks list --workspace D:\project --runs-dir D:\records
harness hooks trust --workspace D:\project --runs-dir D:\records --key project:format-python --fingerprint <list展示的完整指纹>
```

`enable`、`disable` 同样要求 key 和 fingerprint；宿主必需门禁不能从页面禁用。宿主规则放在运行配置 TOML 中时，CLI 审核也传同一个 `--config`。

**审核不等于命令授权。** Hook 仍要求运行启用 `run_command`，并遵守原命令权限：`deny` 不执行、`ask` 生成具体动作审批、明确 `allow` 才自动运行。仅研究、规划和只读子 Agent 不运行脚本。`required=true` 的前置门禁未审核或不可用时阻止操作，普通规则记录跳过原因。

## 事件与控制语义

| 事件 | 时机与结果 |
| --- | --- |
| RunStart | 新输入首次启动；审批与安全暂停接续不重放，可补充上下文或拒绝 |
| PreToolUse | 核心预检后、实际工具前，可拒绝，不开放参数改写 |
| PostToolUse / PostToolUseFailure | 原工具实际执行并保存回执后，成功或失败分别触发 |
| CommandFinished | 同步或后台命令真正结束，句柄返回、轮询与等待不重复触发 |
| PreCompact / PostCompact | 模型摘要前后，不替代原摘要算法与预算 |
| SubagentStart / SubagentStop | 父宿主接纳子执行起止事实，处理器在安全边界消费 |
| Stop | 模型正常最终回答后、宿主验收前，可请求最多一次续跑 |

共享工作区有活动命令或只读子研究时，不运行可能修改目录的脚本。后台完成和子执行事件排队到安全边界；普通工具 Hook 会记录跳过，必需门禁或 block 策略则阻断。取消、等待用户、预算耗尽不触发 Stop 续跑。所有外部 Hook 在最终验收和产物固定之前执行，最终完成通知沿用宿主事件。

## 脚本协议

stdin 接收一个 UTF-8 JSON 对象：`version=1`、`event`、`event_id`、`session_id`、逻辑 `run_id`、实际 `workspace`，以及事件对应的 `tool_name/tool_input/tool_result`、`command`、`subagent` 或 `final_answer`。审批绑定该次固定输入；最大标准输入为256KiB。不要在脚本中内嵌密钥；子环境不继承宿主模型凭据。

stdout 只能输出一个 JSON 对象或留空（等价 `{}`），stderr 用于日志：

```json
{"decision":"continue","additional_context":"已完成项目格式检查。"}
```

`decision=deny` 仅适用于 RunStart/PreToolUse，`retry` 仅适用于 Stop，两者必须有非空 `reason`。不接受改写权限、工具参数、验收结论或模型请求的字段。反馈标明为宿主协作数据，按完整输入预算检查，不能提升为用户授权。

退出0解析输出；退出2在前置事件表示拒绝、Stop表示请求续跑，原因读取 stderr；其他退出码、超时、无效 JSON、未知字段和不支持的控制决定属于脚本故障。stdout 协议最大64KiB，原始输出继续按进程捕获限制保存。`on_failure=continue` 记录故障后继续，`block` 停止后续动作；后置故障不撤销或隐藏已经完成的原工具。

每项默认10秒、最多60秒；一次逻辑执行父子共用默认64次、120秒 Hook 上限，开销也计入原运行总时间。并行子项预留共享时间，Hook 进程不经过工具调度，因此不会递归触发自身。

## 审批、暂停与历史

工具先保存原回执，再运行后处理。等待 Hook 批准时，检查点保存原工具归属和已完成阶段；批准后从原阶段接续，不能重新执行文件写入或命令。严格模式下，批准 Hook 不替代原文件动作的单次批准。配置、入口脚本、工作区或权限变化会使旧批准失效。

Hook 开始前持久标记，结束后保存回执；缺少可信结束记录时不自动重放。超时和取消复用进程树清理，清理不完整保持待核对。安全暂停继承逻辑归属、Hook 开销与 Stop 续跑计数。会话彻底删除同步清理阶段检查点，项目审核记录独立保留。

运行诊断展示实际调用、等待、跳过、拒绝、输出、耗时及当轮定义。刷新或更改当前配置不会覆盖旧轮快照。独立验收与修复保持核心流程；Hook 通过不代表交付通过验收。

## 验收记录

2026-11-05 按获批 Plan 完成实现和下列验收。各组存在重叠，不累加为一次全量结果。

| 验收 | 结果与边界 |
| --- | --- |
| 后端首轮完整回归 | 910项通过、11项平台跳过；另9项普通目录识别失败，原因是该次 `--basetemp` 放在 Git 仓库内 |
| 仓库外复验 | 在 `D:/Codex/Downloads/AgentHarness-hooks-20261105-01a1ab05` 运行目录与项目两组完整测试，28项全部通过，包含上述9项；未修改项目识别代码 |
| 最终相关回归 | Hooks、审批、压缩、长任务、记忆与父子并行共126项通过 |
| 最终 Hooks 专项 | 29项通过，含路径规范化/Windows大小写、前置拒绝、后处理、独立验收顺序、后台结束、Stop限次、取消、父子作用域与审核继承、压缩前审批、事件队列多次审批、重启、未知结果不重放、CLI及预算耗尽 |
| 前端 | 既有全量206项通过，新增 Hooks 页面3项通过；导航、项目/运行设置及 Hooks 的最终联动17项通过；类型检查与生产构建通过 |
| 真实模型与页面 | 下述真实任务及桌面宽度/390px窄屏 Web 页面核对通过；单例证明集成链路，不推断总体任务成功率 |

真实模型任务由页面发起：先审核 `project:format-result` 的实际源码，再选择独立验收并创建 `result.py`。运行编号 `179ce410-7ed0-49bd-b524-f9843012d9bc`，实际2次模型请求、1次工具调用、1次成功 PostToolUse。Hook 规范化文件并添加标记；独立检查核对 `add(2,3)==5`、负数输入及标记，输出 `HOOK_FINAL_CODE_VERIFIED`，终态 completed/passed，最终差异和运行快照均保存格式化后的文件。测试工作区、记录和脚本位于 `.runtime/hooks-qa/`，只作用于本轮独立临时目录。

Playwright 页面核对了设置中的审核/启用状态、源码预览、真实任务完成、独立验收与诊断中的 Hook 事实，截图位于 `output/playwright/hooks-run-expanded.png`、`hooks-detail-mobile.png`、`hooks-settings-mobile.png`。临时服务结束后关闭，不改变用户正式服务或项目配置。

Electron 共用本轮 React 页面及 HTTP 接口；未修改原生桥接或打包配置，本轮没有另行运行安装包验收。

## 设计参考

- [Claude Code Hooks](https://code.claude.com/docs/en/hooks)：事件、匹配、脚本协议与失败策略。
- [Codex Hooks](https://learn.chatgpt.com/docs/hooks)：具体定义审核及变化后的重新信任。
- [Gemini CLI Hooks](https://geminicli.com/docs/hooks/reference/)：结构化输入输出与事件控制语义。
- [OpenCode 插件](https://opencode.ai/v2/docs/build/plugins/server)：区分事实事件和操作拦截，按顺序组合。
- [LangChain Middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)：生命周期节点与工具调用包裹接口。
