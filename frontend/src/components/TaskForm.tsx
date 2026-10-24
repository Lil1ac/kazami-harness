import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, InputNumber, Select } from 'antd';
import {
  ArrowUpOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  BranchesOutlined,
  ClockCircleOutlined,
  PlusOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { createRun, errorMessage, openProject, pickProject, saveProjectSettings } from '../api';
import { useConfigDraft, effectiveConfig } from '../config';
import { formatContextBytes } from '../context';
import { localProjects } from '../projects';
import type { LocalProjectOption, OptionsResponse } from '../types';
import { SkillSelector } from './SkillSelector';

/** 新任务的可编辑内容；项目来源由用户明确选择，不从示例默认推断。 */
interface TaskValues {
  prompt: string; // 本轮要求，提交时去首尾空白。
  base_ref?: string; // 仅隔离Git执行使用，空白沿用HEAD。
  execution_mode?: 'local' | 'isolated'; // 新会话默认直接修改所选目录。
  verifier_id?: string; // 普通目录可选宿主验收器编号。
  outputs?: string; // 每行一个预期产物相对路径。
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
    for (const key of ['prompt', 'base_ref', 'verifier_id', 'outputs'] as const) {
      if (typeof values[key] === 'string') restored[key] = values[key].slice(0, 20000);
    }
    if (values.execution_mode === 'local' || values.execution_mode === 'isolated')
      restored.execution_mode = values.execution_mode;
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

/** 首页主输入区；打开本机目录、选择最近项目与发送任务分别产生明确请求。 */
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
  const executionMode = Form.useWatch('execution_mode', form) || 'local'; // 页面说明与实际请求使用同一字段。
  const [initialDraft] = useState(() => taskDraft(draftKey)); // 调整配置后返回或刷新时恢复本标签页文字。
  const [selectedId, setSelectedId] = useState(() => initialProjectId || lastProject()); // 持久化的编号只用于定位当前可访问登记。
  const [opened, setOpened] = useState<LocalProjectOption | null>(null); // 目录POST返回后立即可用，等待选项刷新。
  const [demoNotice, setDemoNotice] = useState(false); // 仅明确填充样例时展示适用范围。
  const [command, setCommand] = useState(''); // 未保存的测试命令。
  const [testSeconds, setTestSeconds] = useState(30); // 项目测试秒级超时。
  const [settingsSaved, setSettingsSaved] = useState(false); // 保存确认，不等于测试执行通过。
  const { draft, setDraft } = useConfigDraft();
  const config = effectiveConfig(options.config, draft);
  const navigate = useNavigate();
  const cache = useQueryClient();
  const choices = localProjects(options);
  const selected =
    choices.find(/* 以当前宿主记录核对本机目录。 */ (item) => item.id === selectedId) ||
    (opened?.id === selectedId ? opened : undefined);
  const logWorkspace = options.workspaces.find(
    /* 仅辅助示例入口使用固定日志输入。 */ (item) => item.id.includes('logs'),
  );
  const logVerifier = options.verifiers.find(
    /* 不把固定答案验收器用于个人项目。 */ (item) => item.id.includes('logs'),
  );
  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: /* 真实接纳后进入已有持续会话。 */ (run) => {
      try {
        sessionStorage.removeItem(draftKey);
      } catch {
        /* 成功运行不依赖浏览器存储。 */
      }
      cache.setQueryData(['run', run.run_id], run);
      void cache.invalidateQueries({ queryKey: ['runs'] });
      void cache.invalidateQueries({ queryKey: ['conversations'] });
      void cache.invalidateQueries({ queryKey: ['options'] });
      navigate('/runs/' + run.run_id);
    },
  });

  /** 更新当前已明确打开的目录；返回void，只保存编号，任务文字保持。 */
  function acceptProject(project: LocalProjectOption, resetMode = false): void {
    setOpened(project);
    setSelectedId(project.id);
    if (resetMode) form.setFieldsValue({ execution_mode: 'local' }); // 仅打开新目录重置；保存测试设置保留用户的执行方式。
    saveTaskDraft(form.getFieldsValue(), draftKey);
    setDemoNotice(false);
    setSettingsSaved(false);
    try {
      localStorage.setItem('harness-selected-project', project.id);
    } catch {
      /* 内存选择仍可使用。 */
    }
    void cache.invalidateQueries({ queryKey: ['options'] });
  }
  const picker = useMutation({
    mutationFn: pickProject,
    onSuccess: /* 用户取消原生窗口时保持原项目和草稿。 */ (value) => {
      if (value.project) acceptProject(value.project, true);
    },
  });
  const opening = useMutation({
    mutationFn: openProject,
    onSuccess: /* 打开最近项目后进入该项目的会话列表，不自动创建任务。 */ (project) => {
      // 查看已有项目时保留新建草稿的原目录选择；只有明确选目录或新建入口才改变来源。
      void cache.invalidateQueries({ queryKey: ['options'] });
      navigate('/projects/' + encodeURIComponent(project.id));
    },
  });
  const settings = useMutation({
    mutationFn: /* 仅保存公开设置；执行由任务结束后的独立验收承担。 */ () =>
      saveProjectSettings(selected!.id, command, testSeconds),
    onSuccess: /* 回写真实服务器配置并提示已保存。 */ (value) => {
      acceptProject(value);
      cache.setQueryData<OptionsResponse>(['options'], (current) =>
        current
          ? {
              ...current,
              recent_projects: [
                value,
                ...(current.recent_projects || []).filter((item) => item.id !== value.id),
              ],
            }
          : current,
      );
      setSettingsSaved(true);
    },
  });
  useEffect(
    /* 切换项目时加载该项目测试草稿，不执行命令。 */ () => {
      setCommand(selected?.test_command || selected?.test_argv?.join(' ') || '');
      setTestSeconds(selected?.test_timeout_seconds || 30);
    },
    [selected?.id, selected?.test_command],
  );

  /** 明确填充示例后才使用示例目录，不发起任务；保留可修改的普通提交链路。 */
  function fillDemo(): void {
    if (!logWorkspace) return;
    setSelectedId(logWorkspace.id);
    setOpened({ ...logWorkspace, kind: 'workspace' });
    form.setFieldsValue({
      execution_mode: 'isolated',
      prompt:
        '读取工作区 app.log，统计 ERROR 错误类型。编写 analyze_logs.py，运行生成包含 counts 和 total_errors 的 report.json，并核对结果。',
      outputs: 'analyze_logs.py\nreport.json',
      verifier_id: logVerifier?.id,
    });
    saveTaskDraft(form.getFieldsValue(), draftKey);
    setDraft({ ...draft, tools: ['read_file', 'search_files', 'write_file', 'run_command'] });
    mutation.reset();
    setDemoNotice(true);
  }

  /** 提交当前选择的编号与可编辑任务；无目录时不请求模型，失败不清空输入。 */
  function submit(values: TaskValues): void {
    if (!selected) return;
    mutation.mutate({
      prompt: values.prompt.trim(),
      execution_mode: values.execution_mode || 'local',
      ...(selected.kind === 'project'
        ? {
            project_id: selected.id,
            base_ref:
              values.execution_mode === 'isolated' ? values.base_ref?.trim() || 'HEAD' : 'HEAD',
          }
        : { workspace_id: selected.id, verifier_id: values.verifier_id || null }),
      expected_outputs: [
        ...new Set(
          (values.outputs || '')
            .split(/\r?\n/)
            .map(/* 去除空白而保留路径文字。 */ (line) => line.trim())
            .filter(Boolean),
        ),
      ],
      config: draft,
    });
  }
  const projectBusy = picker.isPending || opening.isPending || settings.isPending; // 打开目录期间防止误发给前一个项目。
  const error = picker.error || opening.error || settings.error || mutation.error;
  return (
    <section className="task-start" aria-labelledby="new-task-heading">
      <div className="task-start-heading">
        <span className="start-symbol" aria-hidden="true">
          <BranchesOutlined />
        </span>
        <h1 id="new-task-heading">开始一个新对话</h1>
        <p>打开项目，描述你想完成的工作。</p>
      </div>
      {!options.key_configured && (
        <Alert
          type="warning"
          showIcon
          title={'请先配置 ' + options.config.api_key_env}
          description="在启动服务的终端设置环境变量，然后重启服务。"
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
      {error && (
        <Alert type="error" showIcon title="操作未完成" description={errorMessage(error)} />
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={{ base_ref: 'HEAD', execution_mode: 'local', ...initialDraft }}
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
            <Button
              className="project-open-button"
              aria-label={selected?.label || '选择项目文件夹'}
              icon={<FolderOpenOutlined />}
              onClick={/* 只有明确点击才显示系统选择框。 */ () => picker.mutate()}
              loading={picker.isPending}
              disabled={opening.isPending || settings.isPending}
            >
              {selected?.label || '选择项目文件夹'}
            </Button>
            <div className="start-send-area">
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
                  Boolean(busyId) ||
                  !options.key_configured ||
                  projectBusy
                }
              />
            </div>
          </div>
        </div>
        {picker.isPending && (
          <p className="project-open-status" role="status">
            请在系统窗口中选择文件夹；取消后会保留当前输入。
          </p>
        )}
        <div className="selected-project-context">
          {selected ? (
            <>
              <span className="project-type">
                {selected.kind === 'project' ? <BranchesOutlined /> : <FolderOutlined />}
                {selected.kind === 'project' ? selected.branch || 'Git 项目' : '文件夹'}
              </span>
              {selected.kind === 'project' && selected.head_commit && (
                <code title={'当前提交 ' + selected.head_commit}>
                  {selected.head_commit.slice(0, 8)}
                </code>
              )}
              <span className="selected-project-path" title={selected.path}>
                {selected.path}
              </span>
            </>
          ) : (
            <span>选择电脑中的项目文件夹，或从最近项目中继续。</span>
          )}
        </div>
        {selected?.available === false && (
          <Alert type="error" title={selected.error || '项目暂不可用，请检查目录与 Git 状态。'} />
        )}
        {selected && (
          <p className="project-mode-note">
            {executionMode === 'local'
              ? '直接修改上方所选目录，使用当前文件与未提交改动。'
              : selected.kind === 'project'
                ? '在独立 Git 工作树中执行，原目录的未提交改动不会带入，修改不会写回原目录。'
                : '在独立副本中执行，修改不会写回所选目录。'}
          </p>
        )}
        <details className="task-advanced">
          <summary>
            <SettingOutlined /> 高级设置
            {config.skills?.length ? ' · ' + config.skills.length + ' 个技能' : ''}
          </summary>
          <div className="task-advanced-body">
            <Form.Item
              label="执行方式"
              name="execution_mode"
              extra="选择仅影响新会话；开始后，多轮对话沿用同一执行方式。"
            >
              <Select
                virtual={false}
                options={[
                  { value: 'local', label: '原目录执行（默认）' },
                  { value: 'isolated', label: '隔离执行' },
                ]}
              />
            </Form.Item>
            <div className="composer-config">
              <span>{config.tools.length} 类工具</span>
              <span>输入上限 {formatContextBytes(config.context.max_input_bytes)}</span>
              <Button
                type="link"
                size="small"
                onClick={/* 设置只影响后续任务。 */ () => navigate('/settings')}
              >
                调整配置
              </Button>
            </div>
            <SkillSelector
              options={options.skills || []}
              selected={config.skills || []}
              onChange={/* 选名仍由宿主映射正文。 */ (skills) => setDraft({ ...draft, skills })}
            />
            {selected?.kind === 'project' ? (
              <>
                {executionMode === 'isolated' && (
                  <Form.Item
                    label="提交基线"
                    name="base_ref"
                    extra="默认 HEAD；原目录和已有改动保持。"
                  >
                    <Input placeholder="HEAD" maxLength={200} />
                  </Form.Item>
                )}
                <div className="project-test-settings">
                  <label htmlFor="project-test-command">项目测试命令</label>
                  <Input
                    id="project-test-command"
                    value={command}
                    placeholder="例如 python -m pytest -q，可留空"
                    onChange={
                      /* 编辑不自动保存或执行。 */ (event) => {
                        setCommand(event.target.value);
                        setSettingsSaved(false);
                      }
                    }
                  />
                  <div className="project-test-actions">
                    <label htmlFor="project-test-timeout">超时（秒）</label>
                    <InputNumber
                      id="project-test-timeout"
                      min={1}
                      max={3600}
                      value={testSeconds}
                      onChange={
                        /* 任何未保存的超时修改都撤去旧保存提示。 */ (value) => {
                          setTestSeconds(value || 30);
                          setSettingsSaved(false);
                        }
                      }
                    />
                    <Button
                      size="small"
                      loading={settings.isPending}
                      onClick={/* 用户明确保存后才更新该项目。 */ () => settings.mutate()}
                    >
                      保存项目设置
                    </Button>
                  </div>
                  <p className="muted">
                    保存不会执行命令；后续任务完成后运行这项检查。留空并保存可取消测试。
                  </p>
                  {settingsSaved && <p role="status">项目设置已保存。</p>}
                </div>
              </>
            ) : (
              <Form.Item label="独立验收" name="verifier_id" extra="不选择时，结果保持未验收。">
                <Select
                  allowClear
                  placeholder="不进行独立验收"
                  options={options.verifiers.map(
                    /* 仅接受宿主已登记检查。 */ (item) => ({ value: item.id, label: item.label }),
                  )}
                />
              </Form.Item>
            )}
            <Form.Item
              label="预期产物"
              name="outputs"
              extra="每行一个工作区相对路径；声明路径不代表内容已验收。"
            >
              <Input.TextArea rows={2} placeholder={'report.md\nresult.csv'} />
            </Form.Item>
          </div>
        </details>
      </Form>
      <section className="recent-projects" aria-labelledby="recent-projects-heading">
        <div className="section-heading">
          <h2 id="recent-projects-heading">
            <ClockCircleOutlined /> 最近项目
          </h2>
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            onClick={/* 打开另一个本机目录，保留要求。 */ () => picker.mutate()}
            disabled={projectBusy}
          >
            打开文件夹
          </Button>
        </div>
        {!choices.length ? (
          <p className="recent-projects-empty">打开的项目会保存在这里，方便下次继续。</p>
        ) : (
          <ul>
            {choices.slice(0, 8).map(
              /* 可点击列表不自动发送任务。 */ (project) => (
                <li key={project.kind + project.id}>
                  <button
                    type="button"
                    className={
                      'recent-project-row ' + (selected?.id === project.id ? 'is-selected' : '')
                    }
                    onClick={/* 重新核验当前路径后才采用目录。 */ () => opening.mutate(project.id)}
                    disabled={projectBusy}
                  >
                    <FolderOutlined />
                    <span>
                      <strong>{project.label}</strong>
                      <small>{project.path}</small>
                    </span>
                    <span className="recent-project-kind" title={project.error || undefined}>
                      {project.available === false
                        ? '不可用'
                        : project.kind === 'project'
                          ? 'Git'
                          : '文件夹'}
                    </span>
                  </button>
                </li>
              ),
            )}
          </ul>
        )}
      </section>
      {logWorkspace && (
        <details className="task-examples">
          <summary>试用示例</summary>
          <p className="muted">没有准备好自己的项目时，可使用仓库附带的独立日志样例。</p>
          <Button size="small" onClick={fillDemo}>
            填充日志样例
          </Button>
          {demoNotice && (
            <p className="inline-note">
              样例已启用读取、搜索、写入、命令四类工具。固定日志验收仅适用于这份输入。
            </p>
          )}
        </details>
      )}
    </section>
  );
}
