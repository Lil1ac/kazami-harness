import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Drawer, Input, Modal, Select } from 'antd';
import {
  PlusOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  LoadingOutlined,
  MinusCircleOutlined,
  ArrowRightOutlined,
} from '@ant-design/icons';
import { Link } from 'react-router';
import { errorMessage, getTasks, mutateTask } from '../api';
import type { AgentTask } from '../types';
import './TasksPanel.css';

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
  readOnly = false,
}: {
  runId: string; // 查询及操作的可信运行编号。
  active: boolean; // 当前会话是否持有执行槽。
  historical?: boolean; // true 隐藏修改入口，只展示当轮记录。
  readOnly?: boolean; // 归档会话保留当前清单读取，但不提供修改入口。
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null); // 当前阅读的任务，详情关闭不改变任务状态。
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ['tasks', runId, historical],
    queryFn: /* 只读事实。 */ () => getTasks(runId, historical),
    refetchInterval: !historical && active ? 1500 : false,
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
  const leafTasks = tasks.filter(/* 分组不重复计算工作进度。 */ (task) => !task.is_group);
  const completed = leafTasks.filter(
    /* 前置变化后的待复核项仍保留证据，但不计为完成。 */ (task) =>
      task.status === 'completed' && !task.blocked_reason,
  ).length;
  const selected = tasks.find(/* 详情始终显示服务端最新版本。 */ (task) => task.id === selectedId);
  return (
    <section className="tasks-panel" aria-label="任务清单">
      <header className="work-panel-heading">
        <div>
          <h3>
            任务 · {completed}/{leafTasks.length}
          </h3>
          <span className="muted">
            {active ? '执行进度实时更新' : historical ? '本轮固定记录' : '会话工作项'}
          </span>
        </div>
        {!historical && !readOnly && (
          <Button
            type="text"
            icon={<PlusOutlined />}
            aria-label="新增工作项"
            disabled={active}
            onClick={/* 创建编辑副本，不直接启动运行。 */ () => edit('new')}
          />
        )}
      </header>
      {leafTasks.length > 0 && (
        <progress
          className="task-progress"
          value={completed}
          max={leafTasks.length}
          aria-label="任务完成进度"
        />
      )}
      {(query.error || mutation.error) && (
        <Alert type="error" title={errorMessage(query.error || mutation.error)} />
      )}
      {!tasks.length && (
        <div className="panel-empty">
          {query.isPending ? '正在读取任务…' : '尚无工作项'}
          {!historical && !readOnly && (
            <Button
              type="link"
              disabled={active}
              onClick={/* 空状态与标题入口复用同一编辑流程。 */ () => edit('new')}
            >
              创建任务
            </Button>
          )}
        </div>
      )}
      <ol className="task-list">
        {tasks.map(
          /* 稳定任务编号与父子关系决定行身份，点击只打开详情。 */ (task) => (
            <li
              key={task.id}
              className={`task-row task-${task.blocked_reason ? 'blocked' : task.status} ${task.parent_task_id ? 'task-child' : ''}`}
            >
              <button
                className="task-open"
                type="button"
                onClick={/* 详情使用最新缓存，不发起执行。 */ () => setSelectedId(task.id)}
              >
                <span className="task-status-icon">
                  {task.status === 'completed' && !task.blocked_reason ? (
                    <CheckCircleOutlined />
                  ) : task.status === 'cancelled' ? (
                    <MinusCircleOutlined />
                  ) : task.status === 'in_progress' ? (
                    <LoadingOutlined spin />
                  ) : (
                    <ClockCircleOutlined />
                  )}
                </span>
                <span className="task-row-copy">
                  <strong>
                    #{task.display_id} {task.subject}
                  </strong>
                  {task.owner?.startsWith('subagent:') && <small>子 Agent 开发中</small>}
                  {task.blocked_reason ? (
                    <small>{task.blocked_reason}</small>
                  ) : task.depends_on.length > 0 ? (
                    <small>{task.depends_on.length} 项前置任务</small>
                  ) : null}
                </span>
                <span className="task-state-label">
                  {task.blocked_reason && task.status !== 'cancelled'
                    ? '阻塞'
                    : labels[task.status]}
                </span>
                <ArrowRightOutlined className="task-row-arrow" />
              </button>
            </li>
          ),
        )}
      </ol>
      <Drawer
        title={selected ? `工作项 #${selected.display_id}` : '任务详情'}
        size={440}
        styles={{ wrapper: { maxWidth: '100vw' } }}
        open={Boolean(selected)}
        onClose={/* 关闭阅读层，不修改任务。 */ () => setSelectedId(null)}
        className="task-detail-drawer"
      >
        {selected && (
          <div className="task-detail">
            <div className="task-detail-status">
              {selected.blocked_reason ? '阻塞' : labels[selected.status]}
            </div>
            <h2>{selected.subject}</h2>
            {selected.delegation_run_id && <p>{selected.integration_status === 'applied' ? '子成果已采纳，完成情况仍需核对。' : '开发子成果尚待采纳或核对。'}</p>}
            {selected.blocked_reason && <Alert type="warning" title={selected.blocked_reason} />}
            <section>
              <h3>工作范围</h3>
              <p>{selected.description || '未填写工作范围'}</p>
            </section>
            <section>
              <h3>完成条件</h3>
              <ul>
                {selected.acceptance.map(
                  /* 保留后端原始条件。 */ (value, index) => (
                    <li key={index}>{value}</li>
                  ),
                )}
              </ul>
            </section>
            {selected.depends_on.length > 0 && (
              <section>
                <h3>前置任务</h3>
                {selected.depends_on.map(
                  /* 从同清单解析标题，缺失时保留编号。 */ (id) => (
                    <p key={id}>{tasks.find((task) => task.id === id)?.subject || id}</p>
                  ),
                )}
              </section>
            )}
            {(selected.completion_summary ||
              selected.run_ids.length > 0 ||
              selected.evidence_refs.length > 0) && (
              <section>
                <h3>完成与证据</h3>
                {selected.completion_summary && <p>{selected.completion_summary}</p>}
                {selected.run_ids.map(
                  /* 跳转对应真实运行。 */ (id) => (
                    <Link className="task-evidence-link" key={id} to={'/runs/' + id}>
                      查看运行 {id.slice(0, 8)} <ArrowRightOutlined />
                    </Link>
                  ),
                )}
                {selected.evidence_refs.map(
                  /* 不把人工确认等同于自动测试通过。 */ (reference, index) => (
                    <p className="muted" key={index}>
                      {reference.kind === 'manual_confirmation'
                        ? '人工确认'
                        : '工具结果：' + reference.tool_call_id}
                    </p>
                  ),
                )}
              </section>
            )}
            {!historical && !readOnly && (
              <div className="task-actions">
                {!active && !selected.source && selected.status === 'pending' && (
                  <Button onClick={/* 创建独立编辑副本。 */ () => edit(selected)}>编辑</Button>
                )}
                {!active && !selected.is_group && selected.status === 'pending' && (
                  <Button
                    onClick={/* 完成必须经过说明确认。 */ () => transition(selected, 'completed')}
                  >
                    人工完成
                  </Button>
                )}
                {!active &&
                  (selected.status === 'completed' ||
                    selected.blocked_reason ||
                    selected.status === 'cancelled') && (
                    <Button
                      onClick={/* 重新核对时保留原有证据。 */ () => transition(selected, 'pending')}
                    >
                      重新打开
                    </Button>
                  )}
                {!['cancelled', 'completed'].includes(selected.status) && (
                  <Button
                    danger
                    disabled={mutation.isPending}
                    onClick={/* 后端负责停止关联执行。 */ () => transition(selected, 'cancelled')}
                  >
                    取消任务
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </Drawer>
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
    </section>
  );
}
