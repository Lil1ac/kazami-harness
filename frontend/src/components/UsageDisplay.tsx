import type { RunView } from '../types';

/** 展示供应商已返回的 token 用量；在途请求和缺失计数不伪装成精确零或完整账单。 */
export function UsageDisplay({ run }: { run: RunView }) {
  const usage = run.result.usage;
  const hasKnownUsage =
    usage.total_tokens > 0 || usage.prompt_tokens > 0 || usage.completion_tokens > 0;
  if (run.active && run.result.model_turns > 0 && !hasKnownUsage)
    return (
      <span>
        待统计<small> · 模型尚未返回用量</small>
      </span>
    );
  if (!hasKnownUsage && !run.result.usage_complete) return <span>暂无已知用量</span>;
  return (
    <span>
      {usage.total_tokens.toLocaleString()}
      {(run.active || !run.result.usage_complete) && <small> · 已知部分</small>}
    </span>
  );
}
