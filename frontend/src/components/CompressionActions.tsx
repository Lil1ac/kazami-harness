import { Tag } from 'antd';
import { formatContextBytes } from '../context';
import type { ContextView, WorkSummary } from '../types';

/** 压缩事件种类的可读名称；失败与成功替换分别显示。 */
const actionLabels = {
  offload: '输出外置',
  mask: '旧结果遮蔽',
  summary: '结构化摘要',
  summary_failed: '摘要未采用',
};

/** 摘要字段只作为历史工作记录展示，宿主验收状态继续单独显示。 */
const summaryLabels: Record<keyof WorkSummary, string> = {
  goal: '历史目标',
  decisions: '已有决策',
  changes: '已有修改',
  evidence: '历史证据',
  open_issues: '未解决问题',
  next_steps: '待办',
};

/** 展示持久化输入变换与证据引用；不推断摘要代表本轮测试通过。 */
export function CompressionActions({ context }: { context: ContextView }) {
  const actions = context.actions ?? []; // 旧记录不从配置补造事件。
  return (
    <section className="context-compression" aria-labelledby="compression-heading">
      <h3 id="compression-heading">上下文压缩记录</h3>
      <p className="muted">
        原始对话和执行记录继续保留。这里展示模型输入的变换；摘要调用计入模型轮次和用量。
      </p>
      {context.action_count !== undefined && (
        <p className="muted">
          已记录 {context.action_count} 次变换 · 摘要调用 {context.summary_calls ?? 0} 次
          {context.action_count > actions.length ? ' · 当前展示最近 ' + actions.length + ' 条' : ''}
        </p>
      )}
      {!actions.length && <p className="muted">此记录没有可展示的压缩事件。</p>}
      {actions.map(
        /* 固定范围和来源摘要使刷新仍指向同一原始证据。 */ (action, index) => (
          <details
            className="context-action"
            key={action.kind + ':' + action.reference + ':' + index}
          >
            <summary>
              <Tag color={action.kind === 'summary_failed' ? 'warning' : undefined}>
                {actionLabels[action.kind] ?? action.kind}
              </Tag>
              {formatContextBytes(action.before_bytes)} → {formatContextBytes(action.after_bytes)}
            </summary>
            <p className="muted">
              原始消息范围 [{action.source_start}, {action.source_end}) · 拟请求第{' '}
              {action.model_turn} 轮
            </p>
            <p className="muted">
              证据引用：<code>{action.reference}</code>
            </p>
            {action.reason && (
              <p className="stop-reason">
                {action.reason === 'inherited'
                  ? '来源已核验，复用前轮摘要，未额外调用模型。'
                  : '原因：' + action.reason}
              </p>
            )}
            {action.summary && (
              <dl className="context-summary">
                {(Object.keys(summaryLabels) as (keyof WorkSummary)[]).map(
                  /* 整理内容只按纯文本渲染，保留与宿主事实的边界。 */ (key) => (
                    <div key={key}>
                      <dt>{summaryLabels[key]}</dt>
                      <dd>
                        {typeof action.summary![key] === 'string' ? (
                          action.summary![key]
                        ) : (
                          <ul>
                            {(action.summary![key] as string[]).map(
                              /* 同一字段允许重复条目，索引只用于展示。 */ (text, item) => (
                                <li key={item}>{text}</li>
                              ),
                            )}
                          </ul>
                        )}
                      </dd>
                    </div>
                  ),
                )}
              </dl>
            )}
          </details>
        ),
      )}
    </section>
  );
}
