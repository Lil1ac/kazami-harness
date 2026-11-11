import { Button, Checkbox, Drawer, Form, Input, Select } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { errorMessage, probeModelProvider, saveCustomProvider } from '../api';
import { modelFailureLabel } from '../format';
import type { CustomProviderSettings, ModelProbeResult, ModelProvider, ModelProviderOption, OptionsResponse } from '../types';
import './ModelServices.css';

/** 编辑器中连接、模型和本次凭据操作的输入。 */
interface ServiceValues extends CustomProviderSettings {
  secret?: string; // 留空保留同地址密钥，新的地址使用新分区。
  clearKey?: boolean; // 只有显式勾选才清除当前分区。
}

/** 返回 address 的就地填写提示；为空表示可交给宿主做最终校验。 */
function addressError(address: string, auth: ServiceValues['auth']): string | undefined {
  try {
    const url = new URL(address);
    const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if (url.username || url.password || /[?#\\]/.test(address) || /\/chat\/completions\/*$/.test(url.pathname)) {
      return '请填写服务根地址，去掉凭据、查询参数和 /chat/completions。';
    }
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
      return '请使用 HTTPS；本机回环地址可使用 HTTP。';
    }
    if (auth === 'none' && !local) return '免密连接只支持本机回环地址。';
  } catch {
    return '请输入完整的接口地址。';
  }
}

/** 用户登记的服务列表，桌面允许编辑，独立 Web 使用宿主配置。 */
export function ModelServices({ options }: { options: OptionsResponse }) {
  const cache = useQueryClient();
  const [editor, setEditor] = useState<{ id: ModelProvider; item: ModelProviderOption } | 'new' | null>(null); // 保留打开时的版本，null 关闭侧栏。
  const [form] = Form.useForm<ServiceValues>(); // 地址、模型和密钥由一次提交处理。
  const [saving, setSaving] = useState(false); // 提交期间锁定侧栏，避免重复登记。
  const [failure, setFailure] = useState<string | null>(null); // 服务端的受控错误。
  const [edits, setEdits] = useState<Record<string, number>>({}); // 公开定义相同时，换密钥也使该行旧探测失效。
  const entries = Object.entries(options.model_providers) as [ModelProvider, ModelProviderOption][];
  const current = editor && editor !== 'new' ? editor.item : undefined;
  const auth = Form.useWatch('auth', form);
  const clearKey = Form.useWatch('clearKey', form);

  /** 打开 id 对应连接的当前快照或空白表单，切换时清除临时密钥和表单反馈。 */
  function open(id: ModelProvider | 'new' | null): void {
    if (editor !== null) form.resetFields();
    setFailure(null);
    setEditor(id && id !== 'new' ? { id, item: options.model_providers[id] } : id);
  }

  /** 校验后保存 values，成功替换公开选项，失败保留输入供修正。 */
  async function save(values: ServiceValues): Promise<void> {
    const { secret, clearKey, ...definition } = values;
    const normalized = { ...definition, enabled: definition.enabled ?? true, name: definition.name.trim(), base_url: definition.base_url.trim(),
      default_model: definition.default_model.trim(), models: (definition.models || []).map((id) => id.trim()) };
    const invalidAddress = addressError(normalized.base_url, normalized.auth);
    if (invalidAddress) {
      form.setFields([{ name: 'base_url', errors: [invalidAddress] }]);
      form.scrollToField('base_url', { focus: true, block: 'center' });
      return;
    }
    if (new Set([normalized.default_model, ...normalized.models]).size !== normalized.models.length + 1) {
      setFailure('模型 ID 不能重复。');
      return;
    }
    setSaving(true);
    setFailure(null);
    try {
      const key = normalized.auth === 'bearer' ? (clearKey ? '' : secret?.trim() || undefined) : undefined;
      const edit = editor && editor !== 'new' ? { provider: editor.id, version: editor.item.version } : undefined;
      const result = await saveCustomProvider(normalized, key, edit);
      setEdits(/* 只撤去刚保存连接的旧测试结果。 */ (previous) => ({
        ...previous, [result.provider]: (previous[result.provider] ?? 0) + 1,
      }));
      cache.setQueryData(['options'], result.options);
      open(null);
    } catch (error) {
      setFailure(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section id="model-services" className="model-services" aria-labelledby="model-services-heading">
      <div className="model-services-heading">
        <h2 id="model-services-heading">模型服务</h2>
        {options.desktop && <Button type="primary" aria-label="添加服务商" icon={<PlusOutlined />} onClick={/* 打开未填任何模型的新表单。 */ () => open('new')}>添加服务商</Button>}
      </div>
      {entries.length ? (
        <ul className="model-service-list">
          {entries.map(/* 地址、默认模型或凭据状态变化后撤去原探测结果。 */ ([id, item]) => (
            <ModelServiceRow key={JSON.stringify([id, item.version, item.default_model, item.key_configured, edits[id]])}
              id={id} item={item} edit={options.desktop ? /* 编辑时捕获当前连接快照。 */ () => open(id) : undefined} />
          ))}
        </ul>
      ) : (
        <p className="model-services-empty">{options.desktop ? '添加服务商后，即可在新建对话中选择模型。' : '请在宿主 TOML 中登记模型服务。'}</p>
      )}
      <Drawer
        title={current ? '编辑服务商' : '添加服务商'}
        open={editor !== null}
        size="min(100vw, 480px)"
        onClose={/* 保存期间保留表单，防止提交状态丢失。 */ () => { if (!saving) open(null); }}
        destroyOnHidden
        footer={<div className="model-service-actions">
          <Button disabled={saving} autoInsertSpace={false} onClick={/* 关闭并清除未保存的密钥。 */ () => open(null)}>取消</Button>
          <Button type="primary" autoInsertSpace={false} loading={saving} onClick={/* 页脚触发侧栏内的表单验证。 */ () => form.submit()}>保存</Button>
        </div>}
      >
        <Form<ServiceValues>
          key={editor && editor !== 'new' ? editor.id : editor ?? 'closed'}
          form={form}
          layout="vertical"
          requiredMark={false}
          disabled={saving}
          preserve={false}
          clearOnDestroy
          initialValues={current ? {
            name: current.name, base_url: current.base_url, default_model: current.default_model,
            models: current.models, auth: current.auth, enabled: current.enabled,
          } : { auth: 'bearer', enabled: true, models: [] }}
          onFinish={save}
        >
          <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: '请输入服务商名称' }]}>
            <Input maxLength={80} />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true, whitespace: true, message: '请输入接口地址' }]}>
            <Input placeholder="https://example.com/v1" maxLength={2048} />
          </Form.Item>
          <Form.Item name="auth" label="认证">
            <Select virtual={false} options={[{ value: 'bearer', label: 'API Key' }, { value: 'none', label: '本机免密' }]} />
          </Form.Item>
          {(auth ?? current?.auth ?? 'bearer') === 'bearer' && (
            <>
              <Form.Item name="secret" label="API Key">
                <Input.Password autoComplete="new-password" maxLength={2560} disabled={saving || clearKey}
                  placeholder={current?.key_configured ? '已保存，输入新密钥可替换' : undefined} />
              </Form.Item>
              {current?.key_configured && <Form.Item name="clearKey" valuePropName="checked"><Checkbox>清除已保存的密钥</Checkbox></Form.Item>}
            </>
          )}
          <div className="model-service-models">
            <Form.Item name="default_model" label="默认模型 ID" rules={[{ required: true, whitespace: true, message: '请输入模型 ID' }]}>
              <Input maxLength={200} />
            </Form.Item>
            <Form.List name="models">
              {/* 其他模型作为同一连接下的独立 ID 输入。 */}
              {(fields, { add, remove }) => (
                <>
                  {fields.map(/* 使用表单生成的稳定 key，删行后保留其余输入。 */ ({ key, name }) => (
                    <div key={key} className="model-service-extra">
                      <Form.Item name={name} label={`模型 ID ${name + 2}`} rules={[{ required: true, whitespace: true, message: '请输入模型 ID' }]}>
                        <Input maxLength={200} />
                      </Form.Item>
                      <Button type="text" onClick={/* 从本次草稿删除对应模型。 */ () => remove(name)} aria-label={`移除模型 ${name + 2}`}>移除</Button>
                    </div>
                  ))}
                  <Button type="text" aria-label="添加模型" icon={<PlusOutlined />} disabled={saving || fields.length >= 31} onClick={/* 追加空白模型行。 */ () => add()}>添加模型</Button>
                </>
              )}
            </Form.List>
          </div>
          {current && <Form.Item name="enabled" valuePropName="checked" className="model-service-enabled"><Checkbox>启用服务商</Checkbox></Form.Item>}
          {failure && <p role="alert" className="model-service-error">{failure}</p>}
        </Form>
      </Drawer>
    </section>
  );
}

/** 将连接结果压成一条可读状态；失败指出具体原因，成功确认已验证能力。 */
function probeMessage(result: ModelProbeResult): string {
  if (result.error_code) return modelFailureLabel(result.error_code) || '连接测试失败';
  return '测试通过 · 流式输出和工具调用可用';
}

/** 以 id 的当前版本测试 item 的默认模型；edit 为空时只展示和测试。 */
function ModelServiceRow({ id, item, edit }: { id: ModelProvider; item: ModelProviderOption; edit?: () => void }) {
  const [result, setResult] = useState<{ text: string; failed: boolean } | null>(null); // 当前版本最后一次探测反馈。
  const [testing, setTesting] = useState(false); // 同一行测试期间禁用重复操作。
  const ready = item.auth === 'none' || item.key_configured;
  const status = !item.enabled ? '已停用' : ready ? '已配置' : '缺少 API Key';

  /** 以固定合成消息探测已保存连接，更新本行状态，不触发任务。 */
  async function testConnection(): Promise<void> {
    setTesting(true);
    setResult(null);
    try {
      const response = await probeModelProvider(id, item.version);
      setResult({ text: probeMessage(response), failed: Boolean(response.error_code) });
    } catch (error) {
      setResult({ text: errorMessage(error), failed: true });
    } finally {
      setTesting(false);
    }
  }

  return (
    <li className="model-service-row">
      <div className="model-service-identity">
        <strong title={item.name}>{item.name}</strong>
        <span title={item.default_model}>{item.default_model}{item.models.length > 0 && ` +${item.models.length}`}</span>
      </div>
      <span className="model-service-status">{status}</span>
      <div className="model-service-actions">
        <Button type="text" loading={testing} disabled={!item.enabled || !ready} onClick={testConnection}>测试连接</Button>
        {edit && <Button type="text" disabled={testing} onClick={edit}>编辑</Button>}
      </div>
      {result && <span role="status" className={result.failed ? 'model-service-error model-service-result' : 'model-service-result'}>{result.text}</span>}
      {!edit && item.auth === 'bearer' && !item.key_configured && item.api_key_env && (
        <span className="model-service-result">宿主环境变量 <code>{item.api_key_env}</code></span>
      )}
    </li>
  );
}
