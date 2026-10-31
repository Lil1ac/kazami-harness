import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Input, Modal, Select } from 'antd';
import { Link } from 'react-router';
import { errorMessage, getTasks, mutateTask } from '../api';
import type { AgentTask } from '../types';
import './TasksPanel.css';
import { useWorkEvents } from '../hooks/useWorkEvents';

const labels = {
  pending: '待处理',
  in_progress: '进行中',
  completed: '已完成',
  cancelled: '已取消',
}; // 工作进度，独立验收另行显示。

/** 会话任务清单；historical 读取固定快照，active 时只允许取消，操作不自动提交运行。 */
export function TasksPanel({
  runId,
  active,
  historical = false,
}: {
  runId: string; // 查询及操作的可信运行编号。
  active: boolean; // 当前会话是否持有执行槽。
  historical?: boolean; // true 隐藏修改入口，只展示当轮记录。
}) {
  const cache = useQueryClient();
  useWorkEvents(!historical);
  const query = useQuery({
    queryKey: ['tasks', runId, historical],
    queryFn: /* 只读事实。 */ () => getTasks(runId, historical),
    refetchInterval: historical ? false : 1500,
  });
  const [editing, setEditing] = useState<AgentTask | 'new' | null>(null); // 编辑对象，null 关闭表单。
  const [subject, setSubject] = useState(''); // 用户修改的标题。
  const [description, setDescription] = useState(''); // 用户修改的范围。
  const [acceptance, setAcceptance] = useState(''); // 每行一个完成条件。
  const [dependencies, setDependencies] = useState<string[]>([]); // 同清单前置编号。
  const mutation = useMutation({
    mutationFn: /* 参数已在点击时冻结。 */ (value: { body: object; create?: boolean }) =>
      mutateTask(runId, value.body, value.create),
    onSuccess: /* 写入成功后读取宿主状态，不在页面猜测完成。 */ () => {
      setEditing(null);
      void cache.invalidateQueries({ queryKey: ['tasks', runId] });
    },
  });

  /** 打开 task 的编辑副本；new 创建空表单，返回 void，不写服务。 */
  function edit(task: AgentTask | 'new'): void {
    setEditing(task);
    setSubject(task === 'new' ? '' : task.subject);
    setDescription(task === 'new' ? '' : task.description);
    setAcceptance(task === 'new' ? '' : task.acceptance.join('\n'));
    setDependencies(task === 'new' ? [] : task.depends_on);
  }

  /** 提交当前表单；稳定版本来自编辑快照，成功前保留输入，返回 void。 */
  function save(): void {
    if (!editing || !subject.trim()) return;
    mutation.mutate({
      create: editing === 'new',
      body: {
        request_id: crypto.randomUUID(),
        subject: subject.trim(),
        description,
        acceptance: acceptance.split('\n').filter(/* 空行不成为验收条件。 */ (line) => line.trim()),
        depends_on: dependencies,
        ...(editing === 'new' ? {} : { task_id: editing.id, expected_revision: editing.revision }),
      },
    });
  }

  /** 将 task 转为 status；人工完成必须明确填写说明，保留人工确认来源，返回 void。 */
  function transition(task: AgentTask, status: AgentTask['status']): void {
    if (status === 'completed') {
      let summary = ''; // 本次弹窗说明，不持久存储未确认内容。
      Modal.confirm({
        title: '确认这项工作已完成',
        content: (
          <Input.TextArea
            aria-label="完成说明"
            onChange={
              /* 捕获用户完成说明。 */ (event) => {
                summary = event.target.value;
              }
            }
          />
        ),
        onOk: /* 明确人工确认，后端不会制造测试通过。 */ () =>
          mutation.mutateAsync({
            body: {
              request_id: crypto.randomUUID(),
              task_id: task.id,
              expected_revision: task.revision,
              status,
              completion_summary: summary,
              evidence_refs: [{ kind: 'manual_confirmation' }],
            },
          }),
      });
      return;
    }
    mutation.mutate({
      body: {
        request_id: crypto.randomUUID(),
        task_id: task.id,
        expected_revision: task.revision,
        status,
      },
    });
  }
  const tasks = query.data?.items || [];
  return (
    <details
      className="tasks-panel"
      open={tasks.some(
        /* 当前任务或阻塞优先可见。 */ (task) =>
          task.status === 'in_progress' || task.blocked_reason,
      )}
    >
      <summary>
        任务 ·{' '}
        {
          tasks.filter(
            /* 仅计入当前依赖仍有效的完成项，待复核证据保留但不计完成。 */ (task) =>
              !task.is_group && task.status === 'completed' && !task.blocked_reason,
          ).length
        }
        /{tasks.filter(/* 父任务不重复计数。 */ (task) => !task.is_group).length}
      </summary>
      {(query.error || mutation.error) && (
        <Alert type="error" title={errorMessage(query.error || mutation.error)} />
      )}
      {!tasks.length && <p>尚无工作项</p>}
      <ol>
        {tasks.map(
          /* 任务稳定编号跨 Run 保持。 */ (task) => (
            <li
              key={task.id}
              className="task-row"
              style={{ marginLeft: task.parent_task_id ? 16 : 0 }}
            >
              <div>
                <strong>
                  #{task.display_id} {task.subject}
                </strong>
                <span>
                  {task.blocked_reason && task.status !== 'cancelled'
                    ? '阻塞'
                    : labels[task.status]}
                </span>
              </div>
              {task.blocked_reason && <p role="status">{task.blocked_reason}</p>}
              <details>
                <summary>条件与证据</summary>
                <p>{task.description}</p>
                <ul>
                  {task.acceptance.map(
                    /* 条件为只读文字。 */ (value, index) => (
                      <li key={index}>{value}</li>
                    ),
                  )}
                </ul>
                {task.completion_summary && <p>{task.completion_summary}</p>}
                {task.run_ids.map(
                  /* 从任务定位真实运行证据。 */ (id) => (
                    <Link key={id} to={'/runs/' + id}>
                      查看运行 {id.slice(0, 8)}{' '}
                    </Link>
                  ),
                )}
                {task.evidence_refs.map(
                  /* 保留工具或人工证据来源，避免把自评当验收。 */ (reference, index) => (
                    <p key={index}>
                      {reference.kind === 'manual_confirmation'
                        ? '人工确认'
                        : '工具结果：' + reference.tool_call_id}
                    </p>
                  ),
                )}
              </details>
              {!historical && (
                <div className="task-actions">
                  {!active && !task.source && task.status === 'pending' && (
                    <Button size="small" onClick={/* 只编辑副本。 */ () => edit(task)}>
                      编辑
                    </Button>
                  )}
                  {!active && !task.is_group && task.status === 'pending' && (
                    <Button
                      size="small"
                      onClick={/* 打开人工完成确认。 */ () => transition(task, 'completed')}
                    >
                      人工完成
                    </Button>
                  )}
                  {!active &&
                    (task.status === 'completed' ||
                      task.blocked_reason ||
                      task.status === 'cancelled') && (
                      <Button
                        size="small"
                        onClick={/* 保留证据重新核对。 */ () => transition(task, 'pending')}
                      >
                        重新打开
                      </Button>
                    )}
                  {task.status !== 'cancelled' && task.status !== 'completed' && (
                    <Button
                      size="small"
                      disabled={mutation.isPending}
                      onClick={
                        /* 取消执行项会由宿主停止对应 Run。 */ () => transition(task, 'cancelled')
                      }
                    >
                      取消任务
                    </Button>
                  )}
                </div>
              )}
            </li>
          ),
        )}
      </ol>
      {!historical && (
        <Button disabled={active} onClick={/* 明确新增，不从文字自动创建。 */ () => edit('new')}>
          新增工作项
        </Button>
      )}
      <Modal
        title={editing === 'new' ? '新增工作项' : '编辑工作项'}
        open={editing !== null}
        onCancel={/* 关闭不保存。 */ () => setEditing(null)}
        onOk={save}
        confirmLoading={mutation.isPending}
      >
        <label>
          标题
          <Input
            value={subject}
            maxLength={200}
            onChange={/* 保存标题草稿。 */ (event) => setSubject(event.target.value)}
          />
        </label>
        <label>
          工作范围
          <Input.TextArea
            value={description}
            onChange={/* 保存范围草稿。 */ (event) => setDescription(event.target.value)}
          />
        </label>
        <label>
          完成条件
          <Input.TextArea
            value={acceptance}
            onChange={/* 一行一个条件。 */ (event) => setAcceptance(event.target.value)}
          />
        </label>
        <label>
          前置任务
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            value={dependencies}
            onChange={setDependencies}
            options={tasks
              .filter(
                /* 拒绝自身依赖的第一层提示；最终由宿主校验。 */ (task) =>
                  editing === 'new' || task.id !== editing?.id,
              )
              .map(
                /* 标签简洁可定位。 */ (task) => ({
                  value: task.id,
                  label: '#' + task.display_id + ' ' + task.subject,
                }),
              )}
          />
        </label>
        {mutation.error && <Alert type="error" title={errorMessage(mutation.error)} />}
      </Modal>
    </details>
  );
}
