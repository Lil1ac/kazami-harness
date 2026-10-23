import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Skeleton, Switch, Select } from 'antd';
import { CheckOutlined, UndoOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { errorMessage, getOptions } from '../api';
import { effectiveConfig, useConfigDraft } from '../config';
import type { ContextPolicy, OptionsResponse, RunConfig, RunLimits, ToolName } from '../types';

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
/** 压缩参数按后端单位编辑；None窗口单独作为可选项。 */
const compressionFields: {
  key: keyof ContextPolicy;
  label: string;
  min: number;
  max?: number;
  step?: number;
}[] = [
  { key: 'trigger_ratio', label: '触发比例（0～1）', min: 0.01, max: 0.99, step: 0.05 },
  { key: 'target_ratio', label: '目标比例（0～1）', min: 0.01, max: 0.99, step: 0.05 },
  { key: 'inline_tool_bytes', label: '工具结果外置阈值（字节）', min: 512 },
  { key: 'keep_recent_groups', label: '保留近期完整批次（组）', min: 1, max: 100 },
  { key: 'max_summary_calls', label: '摘要调用上限（次）', min: 0, max: 20 },
  { key: 'max_summary_bytes', label: '摘要正文上限（字节）', min: 512 },
  { key: 'token_safety_margin', label: '额外窗口预留（token）', min: 0 },
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
      // 模型与预算表单不编辑任务技能，保留任务页已经明确选择的名称。
      ...draft,
      requested_model: values.requested_model.trim(),
      tools: values.tools,
      limits: values.limits,
      context: values.context,
    });
    setApplied(true);
  }

  /** 清除浏览器覆盖并恢复宿主默认；不改变正在运行任务的快照。 */
  function reset(): void {
    setDraft({});
    form.setFieldsValue(effectiveConfig(options.config, {}));
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
              <h2>上下文输入</h2>
              <p className="muted">
                整体输入按消息和工具定义的 UTF-8 JSON 字节数计量，不等于模型 token
                数。启用压缩后先处理输入，仍超限时停止并保留已有记录。
              </p>
              <Form.Item
                name={['context', 'load_project_instructions']}
                label="自动加载项目指令"
                valuePropName="checked"
                extra="读取本次实际工作区根目录的 AGENTS.md，固定为本次运行快照。"
              >
                <Switch
                  aria-label="自动加载项目指令"
                  checkedChildren="开启"
                  unCheckedChildren="关闭"
                />
              </Form.Item>
              <div className="limits-grid">
                <Form.Item
                  name={['context', 'max_input_bytes']}
                  label="整体输入上限（KiB）"
                  getValueProps={
                    /* 表单保存字节，输入框统一按 KiB 展示。 */ (
                      bytes: number | null | undefined,
                    ) => ({
                      value: bytes == null ? null : bytes / 1024,
                    })
                  }
                  normalize={
                    /* 将用户输入的 KiB 还原为整数字节，避免误交 256 字节。 */ (
                      kib: number | null,
                    ) => (kib === null ? null : Math.round(kib * 1024))
                  }
                  rules={[
                    { required: true, message: '请填写整体输入上限' },
                    { type: 'number', min: 1, message: '至少允许 1 字节' },
                  ]}
                >
                  <InputNumber min={1 / 1024} step={1} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item
                  name={['context', 'max_project_instruction_bytes']}
                  label="项目指令文件上限（KiB）"
                  getValueProps={
                    /* 原始 UTF-8 文件字节大小转换为 KiB 显示。 */ (
                      bytes: number | null | undefined,
                    ) => ({
                      value: bytes == null ? null : bytes / 1024,
                    })
                  }
                  normalize={
                    /* 以字节保存，读取失败或超限时后端不会截断加载。 */ (kib: number | null) =>
                      kib === null ? null : Math.round(kib * 1024)
                  }
                  rules={[
                    { required: true, message: '请填写指令文件上限' },
                    { type: 'number', min: 1, message: '至少允许 1 字节' },
                  ]}
                >
                  <InputNumber min={1 / 1024} step={1} style={{ width: '100%' }} />
                </Form.Item>
              </div>
            </section>
            <section className="settings-section">
              <h2>历史压缩</h2>
              <p className="muted">
                启用后自动提供只读 read_context 工具回读证据；它也占用工具次数和输入预算。
              </p>
              <Form.Item name={['context', 'compression']} label="压缩策略">
                <Select
                  aria-label="压缩策略"
                  options={[
                    { value: 'off', label: '关闭：完整历史，超限停止' },
                    { value: 'masking', label: '规则处理：外置与遮蔽旧结果' },
                    { value: 'hybrid', label: '混合：规则处理后按需生成摘要' },
                  ]}
                />
              </Form.Item>
              <p className="muted">
                摘要也计入总模型轮次、HTTP尝试、用量和超时。原始轨迹持续保存，固定保留用户原文和失败批次。
              </p>
              <details>
                <summary>压缩预算与模型窗口</summary>
                <div className="limits-grid">
                  {compressionFields.map(
                    /* 所有阈值随下一次任务冻结，运行中修改不会改变当前输入。 */ (field) => (
                      <Form.Item
                        key={field.key}
                        name={['context', field.key]}
                        label={field.label}
                        dependencies={
                          field.key === 'target_ratio' ? [['context', 'trigger_ratio']] : undefined
                        }
                        rules={[
                          {
                            required: true,
                            type: 'number',
                            min: field.min,
                            max: field.max,
                            message: '请填写允许范围内的值',
                          },
                          ...(field.key === 'target_ratio'
                            ? [
                                {
                                  validator: /* 目标低于触发阈值，避免每次请求反复触发。 */ async (
                                    _: unknown,
                                    value: number,
                                  ) => {
                                    if (value >= form.getFieldValue(['context', 'trigger_ratio']))
                                      throw new Error('目标比例必须小于触发比例');
                                  },
                                },
                              ]
                            : []),
                        ]}
                      >
                        <InputNumber
                          min={field.min}
                          max={field.max}
                          step={field.step ?? 1}
                          style={{ width: '100%' }}
                        />
                      </Form.Item>
                    ),
                  )}
                  <Form.Item
                    name={['context', 'model_context_tokens']}
                    label="声明模型窗口（token，可留空）"
                    extra="不根据模型名称猜测。估算输入加最大输出和安全余量应小于窗口；估算无法保证避免厂商拒绝。"
                    normalize={
                      /* 清空显式保存null，后端据此关闭窗口限制。 */ (value: number | null) =>
                        value ?? null
                    }
                    rules={[{ type: 'number', min: 1, message: '窗口必须为正整数' }]}
                  >
                    <InputNumber
                      aria-label="声明模型窗口（token，可留空）"
                      min={1}
                      precision={0}
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </div>
              </details>
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
