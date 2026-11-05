/** 当前 Run 所属命令的固定归属和最新生命周期；PID 只展示，不用作历史控制入口。 */
export interface CommandExecution {
  id: string; // 宿主命令 UUID。
  run_id: string; // 实际拥有进程及日志的主段或子段。
  parent_run_id: string | null; // 子段的日志父归属；主段为空。
  member_id: string | null; // 持久团队成员编号；普通委派为空。
  call_id: string; // 原工具调用，不能作为跨段唯一身份。
  model_turn: number; // 调用开始的模型请求轮号。
  workspace: string; // 实际执行工作区，包含隔离目录。
  argv: string[]; // 已获授权的原参数数组。
  cwd: string; // 工作区内相对工作目录。
  background: boolean; // 启动时是否明确选择后台。
  status:
    'starting' | 'running' | 'stopping' | 'completed' | 'failed' | 'cancelled' | 'needs_review'; // 历史缺终态投影为待核对。
  pid: number | null; // 未获得句柄时未知；重启不会按旧 PID 停止进程。
  created_at: string; // 接纳时间，UTC ISO 字符串。
  finished_at: string | null; // 真实收尾时间；在途为空。
  cleanup_status: 'not_needed' | 'complete' | 'incomplete' | 'unknown'; // 清理状态独立于退出码。
  result: {
    exit_code: number | null;
    error_code: string | null;
    captured_bytes: number;
    truncated: boolean;
  } | null; // 接纳成功不代表命令成功。
}

/** 同一请求的固定运行中输入意图；重试必须使用相同正文和编号。 */
export interface RuntimeInputRequest {
  request_id: string; // 浏览器生成的幂等 UUID。
  prompt: string; // 用户补充，最多 16000 字符。
  delivery: 'steer' | 'next'; // 当前运行边界处理或下一轮执行。
}

/** 收件箱中的一条持久输入；处理确认来自实际模型响应。 */
export interface RuntimeMessage {
  id: string; // 原请求编号。
  prompt: string; // 固定用户正文。
  delivery: 'steer' | 'next'; // 提交时固定的投递意图。
  status: 'received' | 'delivered' | 'processed' | 'retracted' | 'queued' | 'forwarded'; // 接收、投递、处理、排队与转入后续段分别展示。
  created_at: string; // 接纳时间。
  consumers: Record<string, 'delivered' | 'processed'>; // 主/子执行各自的消费确认，不能把子处理冒充主处理。
  job_id: string | null; // 下一轮实际入队的原作业编号。
  error: string | null; // 未能自动入队时的受控原因。
}

/** Run 控制与父子命令的只读投影；历史查询不会启动或恢复工作。 */
export interface RuntimeView {
  active: boolean; // 主 Run 是否仍由当前宿主持有。
  state: {
    run_id: string; // 本段控制归属。
    session_id: string; // 会话的稳定编号。
    status: 'running' | 'pausing' | 'paused' | 'finished' | 'needs_review'; // 暂停意图与安全暂停分别展示。
    messages: RuntimeMessage[]; // 有界持久收件箱。
    checkpoint: {
      spent_model_turns: number;
      spent_tool_calls: number;
      spent_seconds: number;
    } | null; // 安全停止后固定的累计预算。
    finished_at: string | null; // 完成收尾后才有时间。
  } | null; // 尚未绑定或旧历史没有控制记录时为空。
  commands: CommandExecution[]; // 本段及直属子段命令，按实际归属停止。
}

/** 命令原始字节分页；next_offset 即使文本包含替换字符也仍按原字节计数。 */
export interface CommandOutput {
  command: CommandExecution; // 与读取输出时一致的生命周期快照。
  output: {
    content: string;
    offset: number;
    next_offset: number;
    size_bytes: number;
    eof: boolean;
    stream: string;
  }; // 双流使用独立游标。
}
