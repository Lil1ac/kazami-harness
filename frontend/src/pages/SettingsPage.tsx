import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Form,
  Input,
  InputNumber,
  Select,
  Skeleton,
  Tabs,
} from 'antd';
import { CheckOutlined, UndoOutlined } from '@ant-design/icons';
import { useEffect, useState } from 'react';
import { useLocation } from 'react-router';
import { McpSettings } from '../components/McpSettings';
import { SkillSettings } from '../components/SkillSettings';
import { errorMessage, getOptions, saveDesktopSettings } from '../api';
import { ModelServices } from '../components/ModelServices';
import { HookSettings } from '../components/HooksPanel';
import { effectiveConfig, useConfigDraft } from '../config';
import { modelChoices, modelChoiceValue } from '../modelSelection';
import './SettingsPage.css';
import type { OptionsResponse, RoleModels, RunConfig, RunLimits, ToolName } from '../types';

/** 用户可调整的总体预算；重试、清理和输出等内部限制由宿主配置维护。 */
type TaskBudget = Pick<RunLimits, 'max_model_turns' | 'max_tool_calls' | 'run_timeout_seconds'>;

/** 设置页仅收集直接影响下一次任务的模型、权限和总体预算。 */
interface SettingsValues {
  downloads_directory?: string; // 桌面专用绝对下载目录，浏览器模式不提交。
  model?: string; // 已登记的默认模型选项，空值表示交给宿主。
  role_choices?: Partial<Record<keyof RoleModels, string>>; // 空值表示该角色继承会话。
  tools: ToolName[]; // 用户明确允许的基础工具，至少选择一项。
  limits: TaskBudget; // 三项总体预算，不包含隐藏的运行器阈值。
}

/** 可编辑预算的展示元数据；单位同时决定允许的最小值与精度。 */
interface LimitField {
  key: keyof TaskBudget; // 与后端预算字段一一对应。
  label: string; // 显示名称。
  unit: '轮' | '次' | '秒'; // 秒允许小数，其余只接受正整数。
}
const limitFields: LimitField[] = [
  { key: 'max_model_turns', label: '模型轮次', unit: '轮' },
  { key: 'max_tool_calls', label: '工具调用', unit: '次' },
  { key: 'run_timeout_seconds', label: '整个任务超时', unit: '秒' },
];

/** 五类基础工具的权限选项；与后端允许提交的 ToolName 保持一致。 */
const toolOptions: { value: ToolName; label: string }[] = [
  { value: 'read_file', label: 'read_file · 读取文件' },
  { value: 'search_files', label: 'search_files · 搜索内容' },
  { value: 'write_file', label: 'write_file · 写入文件' },
  { value: 'edit_file', label: 'edit_file · 局部编辑文件' },
  { value: 'run_command', label: 'run_command · 执行命令' },
];

/** 当前宿主可配置的角色及中文名称。 */
const modelRoles = { explore: '探索', review: '审阅' } as const;

/** 从完整 config 提取可编辑字段并返回独立表单值，不修改宿主配置或配置草稿。 */
function settingsValues(config: RunConfig): SettingsValues {
  return {
    model: modelChoiceValue(config),
    role_choices: {
      explore: modelChoiceValue(config.role_models.explore),
      review: modelChoiceValue(config.role_models.review),
    },
    tools: [...config.tools],
    limits: {
      max_model_turns: config.limits.max_model_turns,
      max_tool_calls: config.limits.max_tool_calls,
      run_timeout_seconds: config.limits.run_timeout_seconds,
    },
  };
}

/** 读取宿主能力后展示设置；查询失败显示受控错误，不创建或执行任务。 */
export function SettingsPage() {
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 只读取公开配置，不获取密钥值。 */ ({ signal }) => getOptions(signal),
  });
  if (options.isPending) return <Skeleton active paragraph={{ rows: 6 }} />;
  if (options.error)
    return <Alert type="error" title="无法读取配置" description={errorMessage(options.error)} />;
  return <SettingsForm options={options.data} />;
}

/** 编辑后续任务配置；options 为宿主能力，桌面跨启动保存，浏览器使用临时覆盖。 */
function SettingsForm({ options }: { options: OptionsResponse }) {
  const cache = useQueryClient();
  const { hash } = useLocation(); // 新建任务的配置链接直接打开模型分类。
  const { draft, setDraft } = useConfigDraft();
  const [form] = Form.useForm<SettingsValues>();
  const [applied, setApplied] = useState(false); // 仅确认浏览器草稿已更新，不代表任务启动。
  const [saving, setSaving] = useState(false); // 桌面设置保存期间防止重复提交。
  const [failure, setFailure] = useState<string | null>(null); // 保存失败的受控提示。
  const [section, setSection] = useState(
    hash === '#model-services' || !Object.keys(options.model_providers).length
      ? 'models'
      : 'defaults',
  ); // 当前设置分类；切换时保留未保存表单。
  const [advanced, setAdvanced] = useState<string[]>([]); // 角色模型和预算默认收起，校验失败时展开对应区。
  const [invalidField, setInvalidField] = useState<Parameters<typeof form.scrollToField>[0]>(); // 待分类与展开状态生效后定位。
  const choices = modelChoices(options.model_providers);
  useEffect(
    /* 从配置直达链接进入时打开模型分类。 */ () => {
      if (hash === '#model-services') setSection('models');
    },
    [hash],
  );
  useEffect(
    /* 校验失败先显示字段所在分类，再把焦点交给首个无效控件。 */ () => {
      if (!invalidField) return;
      const frame = requestAnimationFrame(() => {
        form.scrollToField(invalidField, { focus: true, block: 'center' });
        setInvalidField(undefined);
      });
      return /* 分类变化或卸载时取消旧定位。 */ () => cancelAnimationFrame(frame);
    },
    [advanced, form, invalidField, section],
  );
  useEffect(
    /* 新增、停用或调整服务商后同步模型字段，保留正在编辑的预算和工具。 */ () => {
      const valid = new Set(modelChoices(options.model_providers).map(({ value }) => value));
      const model = form.getFieldValue('model');
      if (!model || !valid.has(model))
        form.setFieldValue('model', modelChoiceValue(options.config));
      for (const role of Object.keys(modelRoles) as (keyof RoleModels)[]) {
        const value = form.getFieldValue(['role_choices', role]);
        if (value && !valid.has(value)) form.setFieldValue(['role_choices', role], undefined);
      }
    },
    [form, options.config, options.model_providers],
  );

  /** 保存校验后的 values；桌面持久化公开设置，浏览器合入草稿，失败时保留输入供重试。 */
  async function apply(values: SettingsValues): Promise<void> {
    setFailure(null);
    const roleModels: RoleModels = {};
    for (const role of Object.keys(modelRoles) as (keyof RoleModels)[]) {
      const choice = choices.find(({ value }) => value === values.role_choices?.[role]);
      if (choice) roleModels[role] = choice.selection;
    }
    const main = choices.find(({ value }) => value === values.model)?.selection;
    const selection = { ...main, role_models: roleModels };
    if (options.desktop) {
      setSaving(true);
      try {
        const updated = await saveDesktopSettings(
          {
            ...selection,
            provider: main?.provider ?? null,
            requested_model: main?.requested_model ?? null,
            tools: values.tools,
            limits: values.limits,
          },
          values.downloads_directory!.trim(),
        );
        cache.setQueryData(['options'], updated);
        const next = { ...draft };
        delete next.provider;
        delete next.requested_model;
        delete next.role_models;
        delete next.tools;
        delete next.limits;
        setDraft(next);
        setApplied(true);
      } catch (error) {
        setFailure(errorMessage(error));
      } finally {
        setSaving(false);
      }
      return;
    }
    setDraft({
      ...draft,
      ...selection,
      tools: values.tools,
      limits: { ...draft.limits, ...values.limits },
    });
    setApplied(true);
  }

  /** 清空浏览器配置覆盖并恢复宿主表单值，返回 void；不更改活动任务的配置快照。 */
  function reset(): void {
    setDraft({});
    const values = settingsValues(options.config);
    form.setFieldsValue({ ...values, downloads_directory: options.desktop?.downloads_directory });
    // 显式清理宿主未配置的角色，避免表单合并保留原草稿。
    form.setFieldValue('role_choices', values.role_choices);
    setApplied(false);
    setFailure(null);
  }

  /** 提交可见分类的原生表单；两组字段合并校验，保存中忽略重复的 Enter。 */
  function submitSettings(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!saving) form.submit();
  }

  const editingDefaults = section === 'defaults' || section === 'desktop';
  return (
    <div className="settings-workspace">
      <header className="page-heading settings-heading">
        <h1>运行配置</h1>
      </header>
      <Form
        component={false}
        form={form}
        layout="vertical"
        initialValues={{
          ...settingsValues(effectiveConfig(options.config, draft)),
          downloads_directory: options.desktop?.downloads_directory,
        }}
        onFinish={apply}
        onFinishFailed={
          /* 下载与默认项共用宿主保存接口，校验可能需要切到另一分类。 */ ({ errorFields }) => {
            const field = errorFields[0]?.name;
            if (!field) return;
            setSection(field[0] === 'downloads_directory' ? 'desktop' : 'defaults');
            if (field[0] === 'limits' || field[0] === 'role_choices') {
              const key = field[0] === 'limits' ? 'budget' : 'roles';
              setAdvanced((open) => (open.includes(key) ? open : [...open, key]));
            }
            setInvalidField(field);
          }
        }
        onValuesChange={/* 新编辑撤去旧保存结果，错误保留到下一次提交。 */ () => setApplied(false)}
        requiredMark={false}
      >
        <Tabs
          className="settings-tabs"
          activeKey={section}
          onChange={setSection}
          items={[
            {
              key: 'defaults',
              label: '运行默认值',
              forceRender: true,
              children: (
                <form
                  id="settings-defaults-form"
                  className="settings-tab-body"
                  onSubmit={submitSettings}
                >
                  <section className="settings-primary settings-inline-field">
                    <h2>默认模型</h2>
                    {choices.length > 0 ? (
                      <Form.Item name="model" label="默认模型">
                        <Select virtual={false} options={choices} />
                      </Form.Item>
                    ) : (
                      <p className="settings-inline-notice">
                        尚无可用模型。{' '}
                        <button
                          type="button"
                          className="text-button"
                          onClick={
                            /* 模型管理独立保存，不覆盖当前运行草稿。 */ () => setSection('models')
                          }
                        >
                          前往模型服务
                        </button>
                      </p>
                    )}
                  </section>
                  <section className="settings-primary settings-inline-field">
                    <h2>允许的工具</h2>
                    <Form.Item
                      name="tools"
                      label="允许的工具"
                      rules={[
                        { required: true, type: 'array', min: 1, message: '至少选择一类工具' },
                      ]}
                    >
                      <Checkbox.Group options={toolOptions} className="tools-options" />
                    </Form.Item>
                  </section>
                  <Collapse
                    ghost
                    className="settings-advanced"
                    activeKey={advanced}
                    onChange={setAdvanced}
                    items={[
                      ...(choices.length > 0
                        ? [
                            {
                              key: 'roles',
                              label: '角色模型',
                              forceRender: true,
                              children: (
                                <div className="settings-role-grid">
                                  {Object.entries(modelRoles).map(([role, label]) => (
                                    <Form.Item
                                      key={role}
                                      name={['role_choices', role]}
                                      label={label}
                                    >
                                      <Select
                                        allowClear
                                        virtual={false}
                                        placeholder="继承会话模型"
                                        options={choices}
                                      />
                                    </Form.Item>
                                  ))}
                                </div>
                              ),
                            },
                          ]
                        : []),
                      {
                        key: 'budget',
                        label: '执行预算',
                        forceRender: true,
                        children: (
                          <div className="limits-grid">
                            {limitFields.map((field) => (
                              <Form.Item
                                key={field.key}
                                name={['limits', field.key]}
                                label={field.label + '（' + field.unit + '）'}
                                rules={[
                                  { required: true, message: '请填写大于零的预算' },
                                  {
                                    type: 'number',
                                    min: field.unit === '秒' ? 0.1 : 1,
                                    message: '预算必须大于零',
                                  },
                                ]}
                              >
                                <InputNumber
                                  min={field.unit === '秒' ? 0.1 : 1}
                                  precision={field.unit === '秒' ? 1 : 0}
                                />
                              </Form.Item>
                            ))}
                          </div>
                        ),
                      },
                    ]}
                  />
                </form>
              ),
            },
            {
              key: 'models',
              label: '模型服务',
              children: (
                <div className="settings-tab-body">
                  <ModelServices options={options} />
                </div>
              ),
            },
            {
              key: 'integrations',
              label: '技能与集成',
              children: (
                <div className="settings-tab-body">
                  <Tabs
                    className="integration-tabs"
                    defaultActiveKey="skills"
                    animated={false}
                    items={[
                      {
                        key: 'skills',
                        label: '技能',
                        children: <SkillSettings skills={options.skills || []} />,
                      },
                      { key: 'mcp', label: 'MCP', children: <McpSettings /> },
                      {
                        key: 'hooks',
                        label: 'Hooks',
                        children: <HookSettings projects={options.recent_projects} />,
                      },
                    ]}
                  />
                </div>
              ),
            },
            ...(options.desktop
              ? [
                  {
                    key: 'desktop',
                    label: '桌面',
                    forceRender: true,
                    children: (
                      <form
                        id="settings-desktop-form"
                        className="settings-tab-body"
                        onSubmit={submitSettings}
                      >
                        <section className="settings-primary">
                          <h2>下载</h2>
                          <Form.Item
                            name="downloads_directory"
                            label="保存目录"
                            rules={[
                              { required: true, whitespace: true, message: '请填写绝对下载路径' },
                            ]}
                          >
                            <Input maxLength={4096} />
                          </Form.Item>
                        </section>
                        <section className="settings-primary settings-data">
                          <h2>应用数据</h2>
                          <code>{options.desktop.data_directory}</code>
                        </section>
                      </form>
                    ),
                  },
                ]
              : []),
          ]}
        />
        {editingDefaults && (
          <footer className="settings-save-bar">
            {failure && <Alert type="error" title={failure} />}
            <div className="settings-save-row">
              <span className="settings-save-scope">
                {applied ? (
                  <span role="status" className="applied-note">
                    {options.desktop ? '已保存' : '已应用到本浏览器'}
                  </span>
                ) : options.desktop ? (
                  '用于后续任务，退出后保留。'
                ) : (
                  '应用于当前浏览器，刷新后恢复。'
                )}
              </span>
              <div className="settings-actions">
                <Button onClick={reset} disabled={saving} icon={<UndoOutlined />}>
                  {options.desktop ? '撤销更改' : '恢复宿主默认'}
                </Button>
                <Button
                  type="primary"
                  htmlType="submit"
                  form={section === 'desktop' ? 'settings-desktop-form' : 'settings-defaults-form'}
                  icon={<CheckOutlined />}
                  loading={saving}
                >
                  {options.desktop ? '保存设置' : '应用到后续任务'}
                </Button>
              </div>
            </div>
          </footer>
        )}
      </Form>
    </div>
  );
}
