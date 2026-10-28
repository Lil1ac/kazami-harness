import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Skeleton } from 'antd';
import { CheckOutlined, UndoOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { errorMessage, getOptions } from '../api';
import { effectiveConfig, useConfigDraft } from '../config';
import type { OptionsResponse, RunConfig, RunLimits, ToolName } from '../types';

/** 用户可调整的总体预算；重试、清理和输出等内部限制由宿主配置维护。 */
type TaskBudget = Pick<RunLimits, 'max_model_turns' | 'max_tool_calls' | 'run_timeout_seconds'>;

/** 设置页仅收集直接影响下一次任务的模型、权限和总体预算。 */
interface SettingsValues {
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

/** 编辑本浏览器后续任务的覆盖值；options 为宿主完整配置，刷新后恢复宿主默认。 */
function SettingsForm({ options }: { options: OptionsResponse }) {
  const { draft, setDraft } = useConfigDraft();
  const [form] = Form.useForm<SettingsValues>();
  const [applied, setApplied] = useState(false); // 仅确认浏览器草稿已更新，不代表任务启动。

  /** 把校验后的 values 合入草稿，返回 void；仅覆盖可编辑字段，保留技能和宿主内部预算。 */
  function apply(values: SettingsValues): void {
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
    form.setFieldsValue(settingsValues(options.config));
    setApplied(false);
  }

  return (
    <div className="settings-workspace">
      <header className="page-heading">
        <div>
          <h1>运行配置</h1>
          <p className="muted">用于本浏览器后续任务；刷新后恢复宿主默认。</p>
        </div>
      </header>
      <div className="settings-layout">
        <Form
          form={form}
          layout="vertical"
          initialValues={settingsValues(effectiveConfig(options.config, draft))}
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
          <div className="settings-actions">
            <Button type="primary" htmlType="submit" icon={<CheckOutlined />}>
              应用到后续任务
            </Button>
            <Button onClick={reset} icon={<UndoOutlined />}>
              恢复宿主默认
            </Button>
            {applied && (
              <span role="status" className="applied-note">
                已应用到本浏览器
              </span>
            )}
          </div>
        </Form>
        <aside className="settings-aside">
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
          <p className="muted">上下文、重试与输出限制由宿主 TOML 配置维护。</p>
        </aside>
      </div>
    </div>
  );
}
