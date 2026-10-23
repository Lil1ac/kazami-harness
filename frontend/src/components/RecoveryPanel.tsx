import { Alert, Button, Skeleton, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { Link } from 'react-router';
import { dateTime, duration } from '../format';
import { useRunRecovery } from '../hooks/useRunRecovery';
import type { RunView } from '../types';

/** 展示检查点、阻断原因和累计预算；恢复仅由按钮触发，确认后交付新执行视图。 */
export function RecoveryPanel({
  run,
  onConfirmed,
}: {
  run: RunView; // 检查器当前选择的运行，不改变会话输入。
  onConfirmed: (view: RunView) => void; // POST 成功后更新同一会话和路由。
}) {
  const recovery = useRunRecovery(run, onConfirmed);
  const checkpoint = recovery.view?.checkpoint;
  const inherited = run.result.resume_budget;
  const totals = {
    models: (inherited?.model_turns || 0) + run.result.model_turns,
    http: (inherited?.http_attempts || 0) + run.result.http_attempts,
    tools: (inherited?.tool_calls || 0) + run.result.tool_calls,
    summaries: (inherited?.summary_calls || 0) + (run.context?.summary_calls || 0),
    tokens: (inherited?.total_tokens || 0) + run.result.usage.total_tokens,
    complete: (inherited?.usage_complete ?? true) && run.result.usage_complete,
    seconds: (inherited?.active_duration_seconds || 0) + (run.result.active_duration_seconds || 0),
  }; // resume_budget 仅含前序尝试，累计必须加上当前结果，不能覆盖本次事实。
  return (
    <section className="recovery-panel" aria-label="检查点恢复">
      <div className="recovery-heading">
        <h3>检查点恢复</h3>
        <Tag>{run.resume_source ? '恢复尝试 ' + (run.attempt_index || 2) : '首次执行'}</Tag>
      </div>
      {run.resume_source && (
        <p className="muted">
          从 <Link to={'/runs/' + run.resume_source.source_run_id}>原执行检查点</Link>{' '}
          继续同一个要求。 原记录保留，测试和代码审阅以本次执行结果为准。
        </p>
      )}
      {recovery.reading && <Skeleton active paragraph={{ rows: 2 }} title={false} />}
      {recovery.readError && (
        <Alert
          type="warning"
          showIcon
          title="暂时无法核对恢复条件"
          description={recovery.readError}
          action={
            <Button
              size="small"
              onClick={/* 重新核对仅 GET，不自动确认未知请求。 */ () => void recovery.refresh()}
            >
              重新核对
            </Button>
          }
        />
      )}
      {recovery.view && (
        <>
          <p className="recovery-status" role="status">
            {recovery.view.can_resume
              ? '已有安全检查点，可以继续尚未完成的要求。'
              : recovery.view.blocked_reason ||
                (recovery.view.supported
                  ? '当前没有可恢复的安全检查点。'
                  : '首版仅支持 Git 代码任务恢复。')}
          </p>
          {checkpoint && (
            <dl className="compact-dl">
              <div>
                <dt>最近检查点</dt>
                <dd>{dateTime(checkpoint.created_at)}</dd>
              </div>
              <div>
                <dt>原记录位置</dt>
                <dd>事件 #{checkpoint.event_seq}</dd>
              </div>
              <div>
                <dt>恢复前累计消耗</dt>
                <dd>
                  {checkpoint.model_turns} 次模型 · {checkpoint.tool_calls} 次工具 ·{' '}
                  {checkpoint.summary_calls} 次摘要
                </dd>
              </div>
              <div>
                <dt>恢复前主动时长</dt>
                <dd>{duration(checkpoint.active_duration_seconds * 1000)}</dd>
              </div>
            </dl>
          )}
          {checkpoint && (
            <p className="muted">
              缺失模型响应时，宿主按该次请求时限保守预留耗时上界；停机时间不计入。
            </p>
          )}
        </>
      )}
      {(inherited || run.resume_source) && (
        <div className="recovery-budget">
          <h4>{run.interrupted ? '同一要求的已记录累计' : '同一要求的累计消耗'}</h4>
          <dl className="compact-dl">
            <div>
              <dt>模型调用</dt>
              <dd>
                {totals.models} / {run.config.limits.max_model_turns}
              </dd>
            </div>
            <div>
              <dt>工具调用</dt>
              <dd>
                {totals.tools} / {run.config.limits.max_tool_calls}
              </dd>
            </div>
            <div>
              <dt>HTTP · 摘要</dt>
              <dd>
                {totals.http} · {totals.summaries}
              </dd>
            </div>
            <div>
              <dt>主动执行</dt>
              <dd>
                {duration(totals.seconds * 1000)} / {run.config.limits.run_timeout_seconds} 秒
              </dd>
            </div>
            <div>
              <dt>已知 token</dt>
              <dd>
                {totals.tokens.toLocaleString()}
                {!totals.complete && '（用量不完整）'}
              </dd>
            </div>
          </dl>
          <p className="muted">
            {run.interrupted
              ? '本次执行未封存，上述为已记录累计；恢复按检查点核验后的预算执行。'
              : '累计包含前序尝试与本次执行；停机时间不计入主动耗时。'}
          </p>
        </div>
      )}
      {recovery.submitError && (
        <Alert
          type="error"
          showIcon
          title={recovery.confirming ? '恢复请求尚未确认' : '恢复未完成'}
          description={recovery.submitError}
        />
      )}
      {recovery.confirming && (
        <p className="muted">
          上次恢复结果未知。点击“确认原恢复请求”核对同一请求；刷新不会自动恢复。
        </p>
      )}
      {(recovery.view?.supported || recovery.confirming) && (
        <div className="recovery-actions">
          <Button
            aria-label={recovery.confirming ? '确认原恢复请求' : '恢复任务'}
            icon={<ReloadOutlined aria-hidden="true" />}
            disabled={!recovery.canResume}
            loading={recovery.submitting}
            onClick={recovery.resume}
          >
            {recovery.confirming ? '确认原恢复请求' : '恢复任务'}
          </Button>
          <span className="muted">
            核验记录和代码后在新工作树继续，已完成工具不会由宿主自动重放。
          </span>
        </div>
      )}
    </section>
  );
}
