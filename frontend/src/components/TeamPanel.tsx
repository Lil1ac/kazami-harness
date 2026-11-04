import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Input, Modal, Select } from 'antd';
import { errorMessage, getTeam, patchUrl, teamAction } from '../api';
import { IntegrationPanel } from './IntegrationPanel';
import { SubagentsPanel } from './SubagentsPanel';
import { PermissionPanel } from './PermissionPanel';
import type { RunView, TeamState } from '../types';
import './TeamPanel.css';

const labels: Record<string, string> = {
  idle: '空闲', running: '执行中', pending: '待认领', ready: '成果已固定', stale: '待重新分配',
  waiting_input: '等待批准或接续', failed: '未完成', needs_review: '待核对', stopped: '已停止', stopping: '正在停止', active: '协作中',
}; // 成果就绪不等于工作项完成或检查通过。

/** 展示run的持久团队、任务、消息和整体成果；historical/readOnly只读，所有写回沿用C13人工审阅。 */
export function TeamPanel({ run, historical = false, readOnly = false }: { run: RunView; historical?: boolean; readOnly?: boolean }) {
  const cache = useQueryClient();
  const [recipient, setRecipient] = useState('lead'); // 用户明确选择的消息收件人。
  const [content, setContent] = useState(''); // 本地草稿；成功落盘后才清空。
  const [confirm, setConfirm] = useState<'stop' | 'discard' | 'confirm_stopped' | null>(null); // 破坏性管理决定须明确确认。
  const requests = useRef(new Map<string, string>()); // 同内容失败重试沿用编号。
  const query = useQuery({
    queryKey: ['team', run.run_id, historical, run.active],
    queryFn: /* 只读持久事实，不重投任务或请求模型。 */ () => getTeam(run.run_id, historical),
    refetchInterval: !historical && run.active ? 1000 : false,
    retry: false,
  });
  const team: TeamState | null | undefined = query.data?.team ?? run.result.team;
  const locked = historical || readOnly || query.data?.historical;
  const action = useMutation({
    mutationFn: /* 绑定当前审阅版本，只有明确wake才排队继续主Agent。 */ (value: { action: 'message' | 'stop' | 'discard' | 'confirm_stopped'; wake?: boolean }) => {
      const body = { ...value, expected_revision: team!.revision, ...(value.action === 'message' ? { recipient, content } : {}) };
      const key = JSON.stringify(value.action === 'message' ? { ...value, recipient, content } : body);
      if (!requests.current.has(key)) requests.current.set(key, crypto.randomUUID());
      return teamAction(run.run_id, { ...body, request_id: requests.current.get(key)! });
    },
    onSuccess: /* 仅落盘成功后清空草稿，刷新同一事实源及当前会话。 */ (_value, variables) => {
      requests.current.clear();
      if (variables.action === 'message') setContent('');
      setConfirm(null);
      void cache.invalidateQueries({ queryKey: ['team', run.run_id] });
      void cache.invalidateQueries({ queryKey: ['run', run.run_id] });
      void cache.invalidateQueries({ queryKey: ['jobs'] });
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
  });
  /** 将宿主身份id翻译成稳定显示名；未知历史身份保留编号供核对。 */
  function memberName(id: string | null): string {
    return id === 'lead' ? '主 Agent' : id === 'user' ? '你' : team?.members.find(/* 稳定编号匹配。 */ (member) => member.id === id)?.name || id || '待认领';
  }
  /** 使用原清单标题定位id，缺失时显示固定编号，不创造第二份任务名称。 */
  function taskName(id: string): string {
    return query.data?.tasks.items.find(/* 同源C04编号匹配。 */ (task) => task.id === id)?.subject || id;
  }
  if (!team && query.error) return <Alert type="error" title={errorMessage(query.error)} />;
  if (!team) return <p className="panel-empty">尚未创建团队。可让主 Agent 按批准方案组织协作。</p>;
  const teamRecords = run.result.subagents?.filter(/* 团队成员的本轮真实执行记录。 */ (record) => record.team_id === team.id) || [];
  return <section className="team-panel" aria-label="团队协作">
    <header><div><h3>{team.title}</h3><span>{labels[team.status] || team.status} · 第 {team.generation} 代成果</span></div>
      {!locked && team.status !== 'stopped' && <Button danger loading={action.isPending} onClick={/* 打开停止确认，尚未收回授权。 */ () => setConfirm('stop')}>停止团队</Button>}
    </header>
    {historical && <p className="muted">本轮固定快照</p>}
    {team.error && <Alert type="warning" title={team.error} />}
    {query.error && <Alert type="error" title={errorMessage(query.error)} />}
    {action.error && <Alert type="error" title={errorMessage(action.error)} />}
    <div className="team-members">{team.members.map(/* 每个稳定成员保留自己的摘要和累计事实。 */ (member) => <article key={member.id}>
      <div><strong>{member.name}</strong><span>{labels[member.status] || member.status}</span></div>
      <code>{member.write_scope.join('、')}</code>
      <small>{member.model_turns} 次模型 · {member.tool_calls} 次工具 · {member.total_tokens.toLocaleString()} token{member.usage_complete ? '' : '（已知部分）'}</small>
      {member.summary && <p>{member.summary}</p>}
      {member.runs.length > 0 && <details><summary>{member.runs.length} 段执行</summary>{member.runs.map(/* 真实日志路径保持原父归属。 */ (segment) => <a key={segment.run_id} href={`/runs/${segment.parent_run_id}?inspect=${segment.parent_run_id}&tab=agents`}>{taskName(segment.task_id)} · {segment.run_id.slice(0, 8)}</a>)}</details>}
    </article>)}</div>
    <div className="team-tasks">{team.tasks.map(/* 展示C04工作项的当前成员、依赖及固定成果版本。 */ (task) => <article key={task.task_id}>
      <strong>{taskName(task.task_id)}</strong><span>{memberName(task.member_id)} · {labels[task.state] || task.state}</span>
      {task.depends_on.length > 0 && <small>依赖：{task.depends_on.map(taskName).join('、')}</small>}
      {Object.keys(task.dependency_versions).length > 0 && <small>固定版本：{Object.entries(task.dependency_versions).map(/* 仅缩短展示，编号保持完整保存。 */ ([id, version]) => `${taskName(id)} @ ${version.slice(0, 8)}`).join('、')}</small>}
      {task.error && <p role="status">{task.error}</p>}
    </article>)}</div>
    {teamRecords.some(/* 原动作审批仍由父会话统一处理。 */ (record) => record.pending_action) && <PermissionPanel run={run} historical={historical} />}
    <details className="team-mailbox" open><summary>消息</summary>
      {team.messages.slice(-30).map(/* 显示明确来源和收件人，消息不冒充用户批准。 */ (message) => <article key={message.seq}><small>{memberName(message.sender)} → {memberName(message.recipient)}</small><p>{message.content}</p></article>)}
    </details>
    {!locked && team.status === 'active' && <div className="team-message-form">
      <Select aria-label="指示收件人" value={recipient} onChange={setRecipient} options={[{ value: 'lead', label: '主 Agent' }, ...team.members.map(/* 成员身份不能由正文伪造。 */ (member) => ({ value: member.id, label: member.name }))]} />
      <Input.TextArea aria-label="团队指示" value={content} maxLength={4000} onChange={/* 编辑草稿，不自动发送。 */ (event) => setContent(event.target.value)} placeholder="发送范围内的指示或反馈" />
      <div><Button disabled={!content.trim()} loading={action.isPending} onClick={/* 活动成员在下一安全边界接收，空闲消息只落盘。 */ () => action.mutate({ action: 'message' })}>发送指示</Button>
      {!run.active && <Button disabled={!content.trim()} loading={action.isPending} onClick={/* 用户明确要求通过现有队列继续主Agent，不轮询模型。 */ () => action.mutate({ action: 'message', wake: true })}>发送并继续</Button>}</div>
    </div>}
    {action.data?.job && <a href={`/jobs/${action.data.job.id}`}>查看继续执行</a>}
    {teamRecords.length > 0 && <SubagentsPanel parentId={run.run_id} records={teamRecords} active={run.active && !historical} />}
    {(team.stage_ready || query.data?.stage?.snapshot_status === 'ready') && <><h4>团队汇集成果</h4><a href={patchUrl(team.stage_run_id, run.run_id)}>下载固定补丁</a>
      {locked && query.data?.stage?.files.map(/* 历史只读实际固定清单，不提供写回操作。 */ (file) => <code key={file.path}>{file.path}</code>)}
      {team.stage_ready && !run.active && !locked && <IntegrationPanel runId={team.stage_run_id} parentRunId={run.run_id} sourceTest="unverified" />}</>}
    {!locked && !run.active && team.members.some(/* 未知执行不能自行重放，由用户先核对进程。 */ (member) => member.status === 'needs_review') && <Button onClick={/* 明确提示进程核对责任后才释放未知认领。 */ () => setConfirm('confirm_stopped')}>确认旧执行已停止</Button>}
    {!locked && !run.active && team.status === 'stopped' && team.cleanup_status !== 'complete' && <Button danger onClick={/* 丢弃前展示明确确认。 */ () => setConfirm('discard')}>丢弃团队工作树</Button>}
    <Modal open={Boolean(confirm)} title={confirm === 'discard' ? '丢弃团队工作树？' : confirm === 'confirm_stopped' ? '确认旧执行及进程已经停止？' : '停止团队协作？'} okText="确认" cancelText="取消" confirmLoading={action.isPending}
      onCancel={/* 关闭确认不产生任何决定。 */ () => setConfirm(null)} onOk={/* 仅明确确认后提交当前操作及版本。 */ () => confirm && action.mutate({ action: confirm })}>
      <p>{confirm === 'discard' ? '删除宿主创建的成员和汇集工作树，保留固定补丁、执行和消息历史。' : confirm === 'confirm_stopped' ? '请先核对原执行和本机进程均已停止。确认只释放旧认领，不会重放旧操作或自动转派。' : '收回成员执行授权并等待清理，保留已有成果与历史。'}</p>
    </Modal>
  </section>;
}
