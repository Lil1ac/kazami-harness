# Agent Harness

面向工具型任务的 Agent 运行系统，逐步实现工具执行、上下文管理、会话恢复和基于执行反馈的受控自优化。

## 当前可用功能

V0.1 第二切片已实现真实的只读任务闭环：CLI 提交任务 → DeepSeek 请求 read_file → 本地读取工作区文件 → 回传工具结果 → 模型继续读取或输出回答。

- DeepSeek 异步适配器：复用连接、有限 HTTP 重试、单轮超时、协议检查。
- read_file：严格参数校验、UTF-8 字节分页、工作区路径检查、禁止读取运行记录。
- 执行循环：同轮多个工具串行配对，限制模型轮次、工具调用数和总时间，处理取消。
- 运行记录：保存请求计数、完整 assistant 消息、工具结果、已知用量及最终回答，show 只读恢复历史。
- --prepare-only：无需密钥，只创建 created/unverified 准备记录。

当前仅支持 read_file。写文件、搜索、命令执行、独立验收、demo、会话续跑和自动优化待后续切片实现。普通 run 会在启动前拒绝配置中的未实现工具与 --verifier。

## 安装

需要 Python 3.12 和 uv。uv.lock 固定依赖，uv 使用项目独立环境。

```powershell
uv --cache-dir .uv-cache sync --frozen
uv --cache-dir .uv-cache run harness --help
```

## 运行日志样例

在项目目录执行。通过 PowerShell 安全提示输入密钥，避免把明文放进命令历史；程序只读取当前进程的 DEEPSEEK_API_KEY，不读取 .env。

```powershell
$credential = Get-Credential -UserName 'deepseek' -Message '在密码框输入 DeepSeek API Key'
$env:DEEPSEEK_API_KEY = $credential.GetNetworkCredential().Password

uv --cache-dir .uv-cache run harness run --task-file examples/logs/task.md --workspace examples/logs --config harness.example.toml
```

API 请求会发送任务描述及读取文件的内容。样例使用虚构日志；真实运行会消耗模型服务额度。适配器固定连接官方地址，不使用系统代理环境变量。

样例包含 6 条 ERROR：timeout 3 条、database 2 条、auth 1 条。该预期便于手动核对，当前尚无独立验收器，模型回答不能自动当作正确性证明。

执行正常结束时显示 completed / unverified，并返回码 3；这是“已执行、未验收”的明确状态。PowerShell 可用 $LASTEXITCODE 查看。只有后续独立验收通过，执行命令才返回 0。

也可指定自己的 UTF-8 文件，任务中写明工作区内文件名：

```powershell
uv --cache-dir .uv-cache run harness run --task "读取 app.log，统计 ERROR 类型和次数，分页读完再回答" --workspace 'D:\data\logs'
```

仅准备任务的旧用法继续可用，不要求密钥，也不发请求：

```powershell
uv --cache-dir .uv-cache run harness run --prepare-only --task "统计日志中的错误类型" --workspace .
```

## 查看记录

用命令输出中的 UUID 替换下方 RUN_ID：

```powershell
uv --cache-dir .uv-cache run harness show RUN_ID
uv --cache-dir .uv-cache run harness show RUN_ID --json
```

show 返回 0 表示读取成功，与原任务执行退出码无关。--json 输出完整摘要和最终回答；events.jsonl 保存工具轨迹。当前未提供续跑能力。

每次运行写入 .runs/<run_id>/：

```text
run.json        # 任务和配置快照，不包含认证头或环境密钥值
events.jsonl   # 顺序事件日志，读取时的事实来源
summary.json    # 真实运行的派生终态摘要；准备流程不生成
```

--runs-dir 可更换记录根目录；show 时应传入同一路径。日志尾部损坏会保留有效前缀并警告，不改写源文件。

## 配置与限制

配置按“内置默认值 → 显式 TOML → CLI 覆盖”合并，limits 按字段合并。支持 --model 和 --max-model-turns。默认只启用 read_file；旧配置若声明其他工具，需先将 tools 改为 ["read_file"]。

read_file 的 offset 和 next_offset 是字节偏移，max_bytes 默认 8192、最多 65536；内容另受 tool_output_preview_chars 限制。截断会标记 eof=false，并给出下一页偏移。越界路径、外部符号链接、运行记录和非普通文件会返回结构化错误。

逻辑模型轮次与 HTTP 尝试分开计数。只重试连接/读取失败、429 和选定暂时性服务错误；认证、配置、协议错误不重试。读取超时重试可能产生重复计费，已完成的工具不会随 HTTP 请求重做。用量缺失或出现重试时 usage_complete=false，累计值只代表已知部分。

总时限包含模型请求、重试与工具。文件读取是有限大小的短同步操作；超时和取消在异步检查点生效，无法强行中断卡住的文件系统调用。当前适合可信本地任务，路径检查不等同于容器隔离。

| 执行命令退出码 | 含义 |
| --- | --- |
| 0 | 准备操作成功；未来真实执行需独立验收通过 |
| 1 | 执行失败或达到调用上限 |
| 2 | 启动参数、配置或密钥缺失 |
| 3 | 执行完成但未验收 |
| 124 | 总运行或单模型轮次超时 |
| 130 | 用户或上游任务取消 |

取消来源会单独记录：只有 CLI 实际收到 SIGINT 才标记 user_cancelled；上游任务取消标记 task_cancelled；没有 task.cancel 请求却抛出取消异常时，按 failed / unexpected_cancellation 处理。cancellation_received 事件包含阶段、取消计数和仅有文件名/函数/行号的调用位置，不保存异常正文或局部变量。

## 测试与验证范围

```powershell
uv --cache-dir .uv-cache run pytest -q
```

测试使用 HTTPX MockTransport，不联网、不依赖真实密钥，覆盖工具配对、重试、严格协议、字节分页、路径边界、额度、超时、取消、存储故障、CLI 与历史回答恢复。Windows 缺少符号链接权限以及不支持 POSIX FIFO 时，对应用例会跳过。

本切片已完成离线协议与闭环验证，并于 2026-10-19 使用真实 DeepSeek API 跑通日志样例：2 轮模型调用、1 次文件读取，统计结果与样例预期一致。该结果只验证这个样例；独立验收尚未实现。

## 后续实现

接下来扩展 search_files、write_file、run_command，并接入宿主指定的独立验收，完成 V0.1 的产物与验收闭环。

随后在 V0.2 首批交付前端运行控制台：提交任务、实时执行过程、结果与产物、历史查看。前端属于项目明确交付内容，当前尚未实现；页面与验收范围见 [前端需求](docs/FRONTEND_REQUIREMENTS.md)。

- [产品需求](docs/PRD.md)
- [V0.1 技术设计](docs/TECH_DESIGN_V0.1.md)
- [项目概览](PROJECT.md)
- DeepSeek 官方协议参考：[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。当前显式关闭 thinking，保留 assistant 的工具调用与 reasoning_content 供协议回传。
