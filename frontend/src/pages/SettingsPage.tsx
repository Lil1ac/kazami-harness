import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Skeleton } from 'antd';
import { CheckOutlined, UndoOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { errorMessage, getOptions, saveDesktopSettings } from '../api';
import { DesktopCredentials } from '../components/DesktopCredentials';
import { effectiveConfig, useConfigDraft } from '../config';
import type { OptionsResponse, RunConfig, RunLimits, ToolName } from '../types';

/** 用户可调整的总体预算；重试、清理和输出等内部限制由宿主配置维护。 */
type TaskBudget = Pick<RunLimits, 'max_model_turns' | 'max_tool_calls' | 'run_timeout_seconds'>;

/** 设置页仅收集直接影响下一次任务的模型、权限和总体预算。 */
interface SettingsValues {
  downloads_directory?: string; // 桌面专用绝对下载目录，浏览器模式不提交。
  requested_model: string; // 提交前去除首尾空白，不能为空。
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

/** 从完整 config 提取可编辑字段并返回独立表单值，不修改宿主配置或配置草稿。 */
function settingsValues(config: RunConfig): SettingsValues {
  return {
    requested_model: config.requested_model,
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
  const { draft, setDraft } = useConfigDraft();
  const [form] = Form.useForm<SettingsValues>();
  const [applied, setApplied] = useState(false); // 仅确认浏览器草稿已更新，不代表任务启动。
  const [saving, setSaving] = useState(false); // 桌面设置保存期间防止重复提交。
  const [failure, setFailure] = useState<string | null>(null); // 保存失败的受控提示。

  /** 保存校验后的 values；桌面持久化公开设置，浏览器合入草稿，失败时保留输入供重试。 */
  async function apply(values: SettingsValues): Promise<void> {
    setFailure(null);
    if (options.desktop) {
      setSaving(true);
      try {
        const updated = await saveDesktopSettings(
          {
            requested_model: values.requested_model.trim(),
            tools: values.tools,
            limits: values.limits,
          },
          values.downloads_directory!.trim(),
        );
        cache.setQueryData(['options'], updated);
        const next = { ...draft };
        delete next.requested_model;
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
      requested_model: values.requested_model.trim(),
      tools: values.tools,
      limits: { ...draft.limits, ...values.limits },
    });
    setApplied(true);
  }

  /** 清空浏览器配置覆盖并恢复宿主表单值，返回 void；不更改活动任务的配置快照。 */
  function reset(): void {
    setDraft({});
    form.setFieldsValue({
      ...settingsValues(options.config),
      downloads_directory: options.desktop?.downloads_directory,
    });
    setApplied(false);
    setFailure(null);
  }

  return (
    <div className="settings-workspace">
      <header className="page-heading">
        <div>
          <h1>运行配置</h1>
          <p className="muted">
            {options.desktop
              ? '用于后续任务，退出后继续保留。'
              : '用于本浏览器后续任务；刷新后恢复宿主默认。'}
          </p>
        </div>
      </header>
      <div className="settings-layout">
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            ...settingsValues(effectiveConfig(options.config, draft)),
            downloads_directory: options.desktop?.downloads_directory,
          }}
          onFinish={apply}
          onValuesChange={/* 继续编辑时撤去过期的应用确认。 */ () => setApplied(false)}
          requiredMark={false}
        >
          <section className="settings-section">
            <h2>模型与工具</h2>
            <Form.Item
              name="requested_model"
              label="模型名称"
              rules={[{ required: true, whitespace: true, message: '请填写模型名称' }]}
            >
              <Input maxLength={200} />
            </Form.Item>
            <Form.Item
              name="tools"
              label="允许的工具"
              rules={[{ required: true, type: 'array', min: 1, message: '至少选择一类工具' }]}
            >
              <Checkbox.Group options={toolOptions} className="tools-options" />
            </Form.Item>
          </section>
          <section className="settings-section">
            <h2>执行预算</h2>
            <div className="limits-grid">
              {limitFields.map(
                /* 只开放总体预算；进程清理等内部限制继续按宿主快照执行。 */ (field) => (
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
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                ),
              )}
            </div>
          </section>
          {options.desktop && (
            <section className="settings-section">
              <h2>下载</h2>
              <Form.Item
                name="downloads_directory"
                label="保存目录"
                rules={[{ required: true, whitespace: true, message: '请填写绝对下载路径' }]}
              >
                <Input maxLength={4096} />
              </Form.Item>
            </section>
          )}
          {failure && <Alert type="error" title={failure} />}
          <div className="settings-actions">
            <Button type="primary" htmlType="submit" icon={<CheckOutlined />} loading={saving}>
              {options.desktop ? '保存设置' : '应用到后续任务'}
            </Button>
            <Button onClick={reset} icon={<UndoOutlined />}>
              {options.desktop ? '撤销更改' : '恢复宿主默认'}
            </Button>
            {applied && (
              <span role="status" className="applied-note">
                {options.desktop ? '已保存' : '已应用到本浏览器'}
              </span>
            )}
          </div>
        </Form>
        <aside className="settings-aside">
          {options.desktop ? (
            <DesktopCredentials configured={options.key_configured} />
          ) : (
            <section>
              <h2>模型凭据</h2>
              <p>
                <code>{options.config.api_key_env}</code>
              </p>
              <Alert
                type={options.key_configured ? 'success' : 'warning'}
                showIcon
                title={options.key_configured ? '宿主环境已配置' : '宿主环境未配置'}
              />
            </section>
          )}
          {options.desktop && (
            <section>
              <h2>应用数据</h2>
              <p className="muted" style={{ overflowWrap: 'anywhere' }}>
                {options.desktop.data_directory}
              </p>
            </section>
          )}
          <p className="muted">上下文、重试与输出限制由宿主 TOML 配置维护。</p>
        </aside>
      </div>
    </div>
  );
}
