import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Skeleton } from 'antd';
import { CheckOutlined, UndoOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { errorMessage, getOptions } from '../api';
import { effectiveConfig, useConfigDraft } from '../config';
import type { OptionsResponse, RunConfig, RunLimits, ToolName } from '../types';

/** 可编辑预算的语义元数据，单位直接展示给使用者。 */
interface LimitField {
  key: keyof RunLimits; // 与后端字段保持一致。
  label: string; // 用户可读的限制名称。
  unit: string; // 时间、字节、字符或次数的单位。
  help: string; // 与其他预算的边界关系。
}
const limitFields: LimitField[] = [
  {
    key: 'max_model_turns',
    label: '模型轮次',
    unit: '轮',
    help: 'HTTP 重试不会额外消耗逻辑轮次。',
  },
  { key: 'max_tool_calls', label: '工具调用', unit: '次', help: '非法参数和未知工具也计入预算。' },
  {
    key: 'model_max_output_tokens',
    label: '单轮模型输出',
    unit: 'token',
    help: '每次模型响应的输出上限。',
  },
  {
    key: 'max_http_attempts_per_turn',
    label: '每轮 HTTP 尝试',
    unit: '次',
    help: '包含首次请求和重试。',
  },
  {
    key: 'run_timeout_seconds',
    label: '整个任务超时',
    unit: '秒',
    help: '同时覆盖模型、工具和独立验收阶段。',
  },
  {
    key: 'model_turn_timeout_seconds',
    label: '单轮模型超时',
    unit: '秒',
    help: '包含本轮请求、重试和退避。',
  },
  {
    key: 'command_timeout_seconds',
    label: '工具命令超时',
    unit: '秒',
    help: '每个工具子进程的运行期限。',
  },
  {
    key: 'verifier_timeout_seconds',
    label: '默认验收超时',
    unit: '秒',
    help: '宿主验收清单可声明更具体的超时。',
  },
  {
    key: 'cleanup_grace_seconds',
    label: '进程清理宽限',
    unit: '秒',
    help: '停止进程后等待退出的宽限。',
  },
  {
    key: 'tool_output_preview_chars',
    label: '工具输出预览',
    unit: '字符',
    help: '只限制预览长度，原始输出另有限制。',
  },
  {
    key: 'max_process_capture_bytes',
    label: '进程输出保存',
    unit: '字节',
    help: '标准输出和错误输出合计上限。',
  },
  {
    key: 'max_file_write_bytes',
    label: '单次文件写入',
    unit: '字节',
    help: '写入工具每次可提交的数据上限。',
  },
];
const toolOptions: { value: ToolName; label: string }[] = [
  { value: 'read_file', label: 'read_file · 读取文件' },
  { value: 'search_files', label: 'search_files · 搜索内容' },
  { value: 'write_file', label: 'write_file · 写入文件' },
  { value: 'run_command', label: 'run_command · 执行命令' },
];

/** 配置页入口，先读取当前宿主配置再建立可编辑的浏览器草稿。 */
export function SettingsPage() {
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 读取不包含密钥值的能力快照。 */ ({ signal }) => getOptions(signal),
  });
  if (options.isPending) return <Skeleton active paragraph={{ rows: 10 }} />;
  if (options.error)
    return <Alert type="error" title="无法读取配置" description={errorMessage(options.error)} />;
  return <SettingsForm options={options.data} />;
}

/** 编辑下一次提交的配置，不覆盖服务端 TOML；刷新页面会恢复服务端默认值。 */
function SettingsForm({ options }: { options: OptionsResponse }) {
  const { draft, setDraft } = useConfigDraft();
  const [form] = Form.useForm<RunConfig>();
  const [applied, setApplied] = useState(false); // 仅表示本浏览器草稿已更新。

  /** 保存已通过表单校验的模型、工具和预算到上下文，返回 void，不发送 API 请求。 */
  function apply(values: RunConfig): void {
    setDraft({
      requested_model: values.requested_model.trim(),
      tools: values.tools,
      limits: values.limits,
    });
    setApplied(true);
  }

  /** 清除浏览器覆盖并恢复宿主默认；不改变正在运行任务的快照。 */
  function reset(): void {
    setDraft({});
    form.setFieldsValue(options.config);
    setApplied(false);
  }

  return (
    <>
      <header className="page-heading">
        <div>
          <span className="eyebrow">WORKSPACE / CONFIGURATION</span>
          <h1>
            运行配置
            <span className="title-dot" />
          </h1>
          <p className="muted">应用到本浏览器之后提交的任务，刷新后恢复宿主默认。</p>
        </div>
      </header>
      <div className="settings-layout">
        <div>
          <Form
            form={form}
            layout="vertical"
            initialValues={effectiveConfig(options.config, draft)}
            onFinish={apply}
            onValuesChange={/* 修改后撤去过期的已应用提示。 */ () => setApplied(false)}
            requiredMark="optional"
          >
            <section className="settings-section">
              <div className="section-heading">
                <h2>模型与工具</h2>
                <span className="muted">{options.config.provider}</span>
              </div>
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
                  /* 按后端字段创建带单位与范围校验的预算输入。 */ (field) => (
                    <Form.Item
                      key={field.key}
                      name={['limits', field.key]}
                      label={`${field.label}（${field.unit}）`}
                      tooltip={field.help}
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
        </div>
        <aside className="settings-aside">
          <section>
            <span className="eyebrow">CREDENTIAL SOURCE</span>
            <h2>模型凭据</h2>
            <p>
              <code>{options.config.api_key_env}</code>
            </p>
            <Alert
              type={options.key_configured ? 'success' : 'warning'}
              showIcon
              title={options.key_configured ? '宿主环境已配置' : '宿主环境未配置'}
            />
            <p className="muted">密钥由启动后端的进程环境提供，网页不接收、不保存密钥值。</p>
          </section>
          <section>
            <h2>配置作用范围</h2>
            <p className="muted">这份草稿用于下一次提交。正在运行的任务使用创建时的配置快照。</p>
            <p className="muted">
              工作区和验收程序由服务端登记。如需增加目录或检查程序，请调整宿主启动配置。
            </p>
          </section>
          <details>
            <summary>宿主系统提示词（只读）</summary>
            <pre>{options.config.system_prompt}</pre>
          </details>
        </aside>
      </div>
    </>
  );
}
