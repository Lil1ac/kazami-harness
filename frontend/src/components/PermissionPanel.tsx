import { memo, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Select } from 'antd';
import { errorMessage, getPermissions } from '../api';
import type { ActionDecision, PermissionAction, PermissionMode, RunView } from '../types';
import './PermissionPanel.css';

const modes = [
  { value: 'research', label: '仅研究' },
  { value: 'workspace_edit', label: '工作区编辑' },
  { value: 'strict', label: '严格审批' },
]; // 模式选择不能修改宿主策略，实际有效范围以服务端快照为准。
const statusLabels: Record<string, string> = {
  pending: '待批准',
  approved: '已批准，等待执行',
  executing: '执行中',
  executed: '已执行',
  failed: '执行失败',
  rejected: '已拒绝',
  cancelled: '已取消',
  unknown: '结果不明，需核对',
  expired: '已过期',
  invalidated: '已失效',
}; // 授权与真实执行状态分别表达。
const toolLabels: Record<string, string> = {
  write_file: '写入文件',
  edit_file: '编辑文件',
  run_command: '运行本机命令',
  verification: '运行验收检查',
  integration_apply: '采纳成果写回',
  integration_check_candidate: '检查集成候选',
  integration_check_target: '复验采纳目标',
  integration_reconcile: '核对成果写回',
}; // 未知动作保留原名便于查阅。
const decisionLabels = { allow: '允许', ask: '需要审批', deny: '拒绝' }; // 权限判定使用后端结果，页面只翻译文案。

/** 选择下一次普通消息的权限模式；value 为明确选择，onChange 仅改草稿，无服务端副作用。 */
export const PermissionSelect = memo(function PermissionSelect({
  value,
  onChange,
  disabled = false,
}: {
  value: PermissionMode;
  onChange: (value: PermissionMode) => void;
  disabled?: boolean;
}) {
  return (
    <Select
      aria-label="权限模式"
      value={value}
      onChange={onChange}
      options={modes}
      disabled={disabled}
      virtual={false}
      style={{ minWidth: 140 }}
    />
  );
});

/** 展示不可编辑的 action 原参数；文件展示替换前后片段，命令保留argv逐项JSON边界。 */
function ActionContent({ action }: { action: PermissionAction }) {
  const args = action.arguments;
  const command = action.name === 'run_command' || action.name === 'verification';
  return (
    <>
      <strong>{toolLabels[action.name] || action.name}</strong>
      <code className="permission-path">
        {action.inputs.target || action.inputs.cwd || action.workspace}
      </code>
      {command ? (
        <>
          <pre aria-label="命令参数">{JSON.stringify(args.argv, null, 2)}</pre>
          <p>
            实际程序：<code>{action.inputs.executable}</code>
          </p>
          <p className="muted">本机执行；进程可按系统权限访问文件与网络。</p>
        </>
      ) : (
        <>
          {action.name === 'edit_file' && (
            <details>
              <summary>替换前</summary>
              <pre>{String(args.old_text ?? '')}</pre>
            </details>
          )}
          {action.name === 'write_file' && action.inputs.file?.exists && (
            <details>
              <summary>写入前</summary>
              {action.inputs.file.text === null ? (
                <p>原文件不是UTF-8文本，请核对原文件后决定。</p>
              ) : (
                <pre>{action.inputs.file.text}</pre>
              )}
            </details>
          )}
          <pre aria-label="待写入内容">{String(args.new_text ?? args.content ?? '')}</pre>
          {args.overwrite === true && <p>允许覆盖；执行前会重新核对目标版本。</p>}
        </>
      )}
    </>
  );
}

/** 展示run的固定权限与审批；historical只读，onDecision仅当前最新轮提供，不重新提交动作参数。 */
export function PermissionPanel({
  run,
  historical = false,
  disabled = false,
  onDecision,
}: {
  run: RunView; // 本轮固定状态与实际宿主策略。
  historical?: boolean; // 历史使用当轮快照，不回查后续决定覆盖旧事实。
  disabled?: boolean; // 忙碌、归档或请求结果不明时禁止新决定。
  onDecision?: (action: ActionDecision['action']) => void; // 明确按钮事件，缺省仅供阅读。
}) {
  const [historyOpen, setHistoryOpen] = useState(false); // 历史按需加载，打开不会批准或启动动作。
  const [historyOffset, setHistoryOffset] = useState(0); // 每页50项，分页只读取，不复制或修改动作。
  const pending = run.result.permissions.pending;
  const [now, setNow] = useState(Date.now()); // 只控制到期展示，宿主仍独立检查时间。
  useEffect(
    /* 到期时更新按钮，不发送决定或续跑请求。 */ () => {
      setNow(Date.now());
      if (!pending) return;
      const timer = setTimeout(
        /* 计时到期只改变本地阅读状态。 */ () => setNow(Date.now()),
        Math.max(0, pending.expires_at * 1000 - Date.now()) + 20,
      );
      return /* 卸载或切换动作时撤销旧计时。 */ () => clearTimeout(timer);
    },
    [pending?.id],
  );
  const history = useQuery({
    queryKey: ['permissions', run.run_id, historical, historyOffset],
    queryFn: /* 仅读取审批历史，保留历史轮次绑定。 */ () =>
      getPermissions(run.run_id, historical, historyOffset),
    enabled: historyOpen || Boolean(pending && !historical),
    refetchInterval: !historical && (run.active || pending) ? 2000 : false,
  });
  const current =
    !historical && pending
      ? history.data?.items.find(
          /* 原动作批准后仍未出队时读取当前决定，禁止再次批准。 */ (item) => item.id === pending.id,
        ) || pending
      : pending;
  const actionable =
    current?.status === 'pending' || (current?.status === 'expired' && !current.decision);
  const expired = Boolean(
    pending && (pending.expires_at * 1000 <= now || current?.status === 'expired'),
  );
  const policy = run.config.permission_policy;
  const state = run.result.permissions;
  return (
    <section className="permission-panel" aria-label="执行权限">
      <details>
        <summary>
          权限 ·{' '}
          {modes.find(/* 用固定模式生成中文名称。 */ (item) => item.value === state.mode)?.label}
        </summary>
        <dl>
          <dt>工作区</dt>
          <dd>{run.workspace}</dd>
          <dt>可读范围</dt>
          <dd>
            {state.effective.read_file === 'deny' ? '无' : policy.read_roots.join('、') || '无'}
          </dd>
          <dt>可写范围</dt>
          <dd>
            {state.effective.write_file === 'deny' ? '无' : policy.write_roots.join('、') || '无'}
          </dd>
          <dt>实际权限</dt>
          <dd>
            读取 {decisionLabels[state.effective.read_file]} · 写入{' '}
            {decisionLabels[state.effective.write_file]} · 命令{' '}
            {decisionLabels[state.effective.run_command]} · 验收{' '}
            {decisionLabels[state.effective.verification]}
          </dd>
        </dl>
        <p>
          本段之前已用：模型 {state.spent_model_turns} 轮 · 工具 {state.spent_tool_calls} 次 ·
          活动时间 {state.spent_seconds.toFixed(1)} 秒
        </p>
        <p>
          审批链已知用量：
          {(
            (state.spent_usage.total_tokens || 0) + run.result.usage.total_tokens
          ).toLocaleString()}{' '}
          Token{state.spent_usage_complete && run.result.usage_complete ? '' : '（不完整）'}
        </p>
      </details>
      {pending && (
        <article className="permission-request" aria-label="动作审批">
          <ActionContent action={pending} />
          <p>
            {historical
              ? '当轮快照：待批准'
              : expired && actionable
                ? '审批已过期；请取消等待后重新发起任务。'
                : actionable
                  ? '尚未执行 · 有效至 ' +
                    new Date(pending.expires_at * 1000).toLocaleTimeString('zh-CN', {
                      hour12: false,
                    })
                  : statusLabels[current!.status] || current!.status}
          </p>
          {onDecision && !historical && actionable && (
            <div className="permission-actions">
              <Button
                type="primary"
                disabled={disabled || run.active || expired}
                onClick={/* 只批准当前固定动作。 */ () => onDecision('approve')}
              >
                批准本次
              </Button>
              <Button
                disabled={disabled || run.active}
                onClick={/* 拒绝不调用模型寻找替代方案。 */ () => onDecision('reject')}
              >
                拒绝
              </Button>
              <Button
                disabled={disabled || run.active}
                onClick={/* 取消不会回滚已有修改。 */ () => onDecision('cancel')}
              >
                取消等待
              </Button>
            </div>
          )}
        </article>
      )}
      <details
        onToggle={
          /* 仅处理当前折叠块，忽略子项toggle冒泡，不触发执行。 */ (event) => {
            if (event.target === event.currentTarget) setHistoryOpen(event.currentTarget.open);
          }
        }
      >
        <summary>动作审批记录</summary>
        {history.error && <Alert type="error" title={errorMessage(history.error)} />}
        {history.data?.items.map(
          /* 每项展开显示原参数，旧动作不提供批准按钮。 */ (action) => (
            <details key={action.id}>
              <summary>
                {toolLabels[action.name] || action.name} ·{' '}
                {statusLabels[action.status] || action.status}
              </summary>
              <ActionContent action={action} />
            </details>
          ),
        )}
        {history.data?.total === 0 && <p>暂无动作审批。</p>}
        {history.data && history.data.total > 50 && (
          <div className="permission-actions">
            <Button
              disabled={historyOffset === 0}
              onClick={/* 上页只读查询。 */ () => setHistoryOffset(Math.max(0, historyOffset - 50))}
            >
              上一页
            </Button>
            <Button
              disabled={historyOffset + 50 >= history.data.total}
              onClick={/* 下页只读查询。 */ () => setHistoryOffset(historyOffset + 50)}
            >
              下一页
            </Button>
          </div>
        )}
      </details>
    </section>
  );
}
