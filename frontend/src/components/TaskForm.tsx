import { useEffect, useState, useRef } from 'react';
import { Alert, Button, Checkbox, Form, Input, Select } from 'antd';
import { ArrowUpOutlined, BranchesOutlined, SettingOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router';
import { createRun, errorMessage } from '../api';
import { useConfigDraft, effectiveConfig } from '../config';
import type { LocalProjectOption, OptionsResponse, CreateRunRequest } from '../types';
import { SkillSelector } from './SkillSelector';
import { ProjectSelector } from './ProjectSelector';
import { ModeSelect } from './ModeSelect';

/** 新任务的可编辑内容；项目来源由用户明确选择，不从示例默认推断。 */
interface TaskValues {
  prompt: string; // 本轮要求，提交时去首尾空白。
  base_ref?: string; // 仅隔离Git执行使用，空白沿用HEAD。
  verifier_id?: string; // 普通目录可选宿主验收器编号。
}

/** 网络结果未知的新建请求；正文和编号一起冻结，刷新后只核对该请求。 */
interface PendingNewSubmission {
  id: string; // 幂等请求 UUID。
  body: CreateRunRequest; // 已发送的受限参数，不含凭据。
}

/** 读取 key 下待确认请求；损坏或非对象返回 null，不触发任何网络请求。 */
function readPendingSubmission(key: string): PendingNewSubmission | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key + ':submission') || 'null');
    return value && typeof value.id === 'string' && typeof value.body?.prompt === 'string'
      ? value
      : null;
  } catch {
    return null;
  }
}

/** 当前标签页的新任务草稿键；不保存密钥、宿主路径或模型执行状态。 */
const TASK_DRAFT_KEY = 'harness-new-task-draft';

/** 恢复可编辑文字；损坏或非对象存储退回默认值，限制字段类型与长度。 */
function taskDraft(key = TASK_DRAFT_KEY): Partial<TaskValues> {
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(key) || '{}');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    const values = saved as Record<string, unknown>;
    const restored: Partial<TaskValues> = {};
    for (const key of ['prompt', 'verifier_id'] as const) {
      if (typeof values[key] === 'string') restored[key] = values[key].slice(0, 20000);
    }
    return restored;
  } catch {
    return {};
  }
}

/** 保存当前标签页文字；存储被禁用时保留表单内存值，不影响目录选择与提交。 */
function saveTaskDraft(values: Partial<TaskValues>, key = TASK_DRAFT_KEY): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(values));
  } catch {
    /* 草稿持久化失败不阻塞用户操作。 */
  }
}

/** 读取最近选中的项目编号；存储不可用时返回空值，不触发后端操作。 */
function lastProject(): string {
  try {
    return localStorage.getItem('harness-selected-project') || '';
  } catch {
    return '';
  }
}

/** 首页主输入区；项目选择、确认绑定与发送各自独立，切换保留任务文字。 */
export function TaskForm({
  options,
  busyId,
  initialProjectId,
}: {
  options: OptionsResponse; // 当前宿主目录与能力。
  busyId: string | null; // 全局单槽的实际活动任务。
  initialProjectId?: string; // 项目新建入口明确指定来源，优先于浏览器上次选择。
}) {
  const draftKey = initialProjectId ? TASK_DRAFT_KEY + ':' + initialProjectId : TASK_DRAFT_KEY; // 不同项目的新会话草稿分别保存。
  const [form] = Form.useForm<TaskValues>(); // 失败时保留要求与高级字段。
  const [createWorktree, setCreateWorktree] = useState(false); // 只由本次明确选择开启，不从草稿恢复。
  const [initialDraft] = useState(() => taskDraft(draftKey)); // 调整配置后返回或刷新时恢复本标签页文字。
  const [selectedId, setSelectedId] = useState(() => initialProjectId || lastProject()); // 持久化的编号只用于定位当前可访问登记。
  const [opened, setOpened] = useState<LocalProjectOption | null>(null); // 目录POST返回后立即可用，等待选项刷新。
  const [projectBusy, setProjectBusy] = useState(false); // 项目切换或创建弹窗期间防止向旧目录发送。
  const [autoRepair, setAutoRepair] = useState(false); // 本次明确开启，不从其他项目或文字草稿恢复。
  const [mode, setMode] = useState<'auto' | 'plan'>('auto'); // 自动也允许Agent主动进入规划。
  const { draft, setDraft } = useConfigDraft();
  const config = effectiveConfig(options.config, draft);
  const navigate = useNavigate();
  const cache = useQueryClient();
  const pendingSubmission = useRef<PendingNewSubmission | null>(readPendingSubmission(draftKey)); // 网络未知时固定原载荷及编号。
  const [confirmingSubmission, setConfirmingSubmission] = useState(
    Boolean(pendingSubmission.current),
  ); // 刷新后保留确认提示。
  const choices = options.recent_projects;
  const selected =
    choices.find(/* 以当前宿主记录核对本机目录。 */ (item) => item.id === selectedId) ||
    (opened?.id === selectedId ? opened : undefined);
  const repairAvailable = selected?.kind === 'project' && Boolean(selected.test_argv?.length); // 只使用已保存的公开测试，不取未保存的命令草稿。
  useEffect(
    /* 换项目必须重新选择，避免把旧项目的自动修复授权带到其他目录。 */ () => {
      setAutoRepair(false);
      setCreateWorktree(false);
      form.setFieldValue('base_ref', 'HEAD');
    },
    [selected?.id],
  );
  const mutation = useMutation({
    mutationFn: /* 首次投递前落浏览器草稿，网络未知时重试只确认原请求。 */ (
      body: CreateRunRequest,
    ) => {
      if (!pendingSubmission.current) {
        try {
          pendingSubmission.current = JSON.parse(
            sessionStorage.getItem(draftKey + ':submission') || 'null',
          );
        } catch {
          /* 存储损坏时使用本次输入。 */
        }
      }
      const pending = pendingSubmission.current || {
        id: crypto.randomUUID(),
        body: structuredClone(body),
      };
      pendingSubmission.current = pending;
      try {
        sessionStorage.setItem(draftKey + ':submission', JSON.stringify(pending));
      } catch {
        /* 内存仍保持幂等编号。 */
      }
      return createRun(pending.body, pending.id);
    },
    onSuccess: /* 真实接纳后进入已有持续会话。 */ (run) => {
      try {
        if (form.getFieldValue('prompt')?.trim() === pendingSubmission.current?.body.prompt)
          sessionStorage.removeItem(draftKey);
        sessionStorage.removeItem(draftKey + ':submission');
      } catch {
        /* 成功运行不依赖浏览器存储。 */
      }
      void cache.invalidateQueries({ queryKey: ['conversations'] });
      void cache.invalidateQueries({ queryKey: ['options'] });
      navigate('/jobs/' + run.id);
      pendingSubmission.current = null;
      setConfirmingSubmission(false);
    },
    onError: /* 确定未接纳的4xx允许更正；网络未知只确认原请求。 */ (error: unknown) => {
      const status = (error as { status?: number }).status;
      if (status && status >= 400 && status < 500) {
        pendingSubmission.current = null;
        try {
          sessionStorage.removeItem(draftKey + ':submission');
        } catch {
          /* 不影响表单。 */
        }
      } else setConfirmingSubmission(true);
    },
  });

  /** 更新当前已明确打开的目录；返回void，只保存编号，任务文字保持。 */
  function acceptProject(project: LocalProjectOption): void {
    setOpened(project);
    setSelectedId(project.id);
    setCreateWorktree(false); // 切换或新建项目都回到原目录执行，隔离须再次明确选择。
    saveTaskDraft(form.getFieldsValue(), draftKey);
    try {
      localStorage.setItem('harness-selected-project', project.id);
    } catch {
      /* 内存选择仍可使用。 */
    }
    void cache.invalidateQueries({ queryKey: ['options'] });
  }
  /** 提交当前选择的编号与可编辑任务；无目录时不请求模型，失败不清空输入。 */
  function submit(values: TaskValues): void {
    if (!selected) return;
    mutation.mutate({
      prompt: values.prompt.trim(),
      ...(mode === 'plan' ? { mode } : {}),
      execution_mode: createWorktree ? 'isolated' : 'local',
      ...(selected.kind === 'project'
        ? {
            project_id: selected.id,
            base_ref: createWorktree ? values.base_ref?.trim() || 'HEAD' : 'HEAD',
          }
        : { workspace_id: selected.id, verifier_id: values.verifier_id || null }),
      config: { ...draft, auto_repair: repairAvailable && autoRepair },
    });
  }
  const error = mutation.error;
  return (
    <section className="task-start" aria-labelledby="new-task-heading">
      <div className="task-start-heading">
        <h1 id="new-task-heading">新建对话</h1>
      </div>
      {!options.key_configured && (
        <Alert
          type="warning"
          showIcon
          title={options.desktop ? '请先配置模型密钥' : '请先配置 ' + options.config.api_key_env}
          description={
            options.desktop ? (
              <Link to="/settings">前往运行配置保存 DeepSeek API Key</Link>
            ) : (
              '在启动服务的终端设置环境变量，然后重启服务。'
            )
          }
        />
      )}
      {busyId && (
        <Alert
          type="info"
          title="已有任务正在执行"
          description={
            <Button
              type="link"
              onClick={/* 打开现有任务，不重复发送。 */ () => navigate('/runs/' + busyId)}
            >
              查看正在执行的任务
            </Button>
          }
        />
      )}
      {Boolean(error) && (
        <Alert type="error" showIcon title="操作未完成" description={errorMessage(error)} />
      )}
      {confirmingSubmission && (
        <p role="status">上次提交尚未确认。再次点击开始执行会核对原请求，保留原编号与内容。</p>
      )}
      <ProjectSelector
        projects={choices}
        value={selected}
        pickerAvailable={options.folder_picker_available}
        onChange={acceptProject}
        onBusyChange={setProjectBusy}
      />
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={{ base_ref: 'HEAD', ...initialDraft }}
        onValuesChange={
          /* 跨配置页与刷新保留用户文字，接纳运行后清空。 */ (_, values) =>
            saveTaskDraft(values, draftKey)
        }
        requiredMark={false}
        className="start-form"
      >
        <div className="start-input-surface">
          <Form.Item
            label="任务描述"
            name="prompt"
            className="start-prompt"
            rules={[{ required: true, whitespace: true, message: '请描述要完成的任务' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 4, maxRows: 10 }}
              maxLength={20000}
              placeholder="修复一个问题、实现一个功能，或分析项目中的文件…"
            />
          </Form.Item>
          <div className="start-input-toolbar">
            <div className="start-send-area">
              <ModeSelect value={mode} onChange={setMode} />
              <span className="start-model" title={config.requested_model}>
                {config.requested_model}
              </span>
              <Button
                type="primary"
                htmlType="submit"
                aria-label="开始执行"
                className="start-send"
                icon={<ArrowUpOutlined />}
                loading={mutation.isPending}
                disabled={
                  !selected ||
                  selected.available === false ||
                  !options.key_configured ||
                  projectBusy
                }
              />
            </div>
          </div>
        </div>
        {selected?.available === false && (
          <Alert type="error" title={selected.error || '项目暂不可用，请检查目录与 Git 状态。'} />
        )}
        {selected && (
          <p className="project-mode-note">
            {config.planning_policy === 'required' && '宿主要求先批准计划，批准前只能读取项目。'}
            {createWorktree
              ? '在独立 Git 工作树中执行，原目录的未提交改动不会带入，修改不会写回原目录。'
              : '在所选目录执行，文件直接保存到该目录。'}
          </p>
        )}
        <details className="task-advanced">
          <summary>
            <SettingOutlined /> 高级设置
            {config.skills?.length ? ' · ' + config.skills.length + ' 个技能' : ''}
          </summary>
          <div className="task-advanced-body">
            {selected?.kind === 'project' && (
              <p className="project-revision">
                <BranchesOutlined /> {selected.branch || 'Git 项目'}
                {selected.head_commit && (
                  <code title={'当前提交 ' + selected.head_commit}>
                    {selected.head_commit.slice(0, 8)}
                  </code>
                )}
              </p>
            )}
            {selected?.kind === 'project' && (
              <Form.Item label="执行目录" htmlFor="execution-directory">
                <Select
                  id="execution-directory"
                  value={createWorktree ? 'worktree' : 'local'}
                  virtual={false}
                  onChange={
                    /* 创建工作树会更换实际执行目录，必须本次明确选择。 */ (value) =>
                      setCreateWorktree(value === 'worktree')
                  }
                  options={[
                    { value: 'local', label: '使用所选目录' },
                    { value: 'worktree', label: '创建独立 Git 工作树（更换目录）' },
                  ]}
                />
              </Form.Item>
            )}
            <div className="composer-config">
              <span>{config.requested_model}</span>
              <Link to="/settings">调整模型与预算</Link>
            </div>
            <Checkbox
              checked={config.memory.use}
              onChange={
                /* 只覆盖公开选择，内部预算由宿主维护。 */ (event) =>
                  setDraft({
                    ...draft,
                    memory: { use: event.target.checked, capture: config.memory.capture },
                  })
              }
            >
              使用项目记忆
            </Checkbox>
            <Checkbox
              checked={config.memory.capture}
              onChange={
                /* 新增候选不自动生效。 */ (event) =>
                  setDraft({
                    ...draft,
                    memory: { use: config.memory.use, capture: event.target.checked },
                  })
              }
            >
              允许提出记忆候选
            </Checkbox>
            {((options.skills?.length || 0) > 0 || (config.skills?.length || 0) > 0) && (
              <SkillSelector
                options={options.skills || []}
                selected={config.skills || []}
                onChange={/* 选名仍由宿主映射正文。 */ (skills) => setDraft({ ...draft, skills })}
              />
            )}
            {selected?.kind === 'project' && createWorktree && (
              <Form.Item
                label="提交基线"
                name="base_ref"
                extra="默认 HEAD；原目录的未提交改动不会带入。"
              >
                <Input placeholder="HEAD" maxLength={200} />
              </Form.Item>
            )}
            {selected?.kind === 'project' && (
              <Checkbox
                checked={repairAvailable && autoRepair}
                disabled={!repairAvailable}
                onChange={
                  /* 只对本次发送授权，修复共用任务预算。 */ (event) =>
                    setAutoRepair(event.target.checked)
                }
              >
                测试失败自动修复（最多2次）
              </Checkbox>
            )}
            {selected?.kind === 'workspace' && options.verifiers.length > 0 && (
              <Form.Item label="独立验收" name="verifier_id" extra="不选择时，结果保持未验收。">
                <Select
                  allowClear
                  placeholder="不进行独立验收"
                  options={options.verifiers.map(
                    /* 只提交宿主已登记的受控编号。 */ (item) => ({
                      value: item.id,
                      label: item.label,
                    }),
                  )}
                />
              </Form.Item>
            )}
          </div>
        </details>
      </Form>
    </section>
  );
}
