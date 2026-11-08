import { Alert, Button, Checkbox, Form, Input, Select, Space, Tag } from 'antd';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { errorMessage, getOptions, probeModelProvider, saveCustomProvider } from '../api';
import { modelFailureLabel } from '../format';
import type { CustomProviderSettings, ModelProbeResult, ModelProvider, ModelProviderOption, OptionsResponse } from '../types';
import { DesktopCredentials } from './DesktopCredentials';

/** 自定义连接的公开定义和编辑版本，可用作表单初始值。 */
interface Connection extends CustomProviderSettings {
  provider: ModelProvider; // 持久服务商标识。
  version: string; // 当前连接的绑定版本。
  key_configured: boolean; // 当前分区的凭据状态。
  api_key_env: string | null; // 独立 Web 的宿主凭据来源。
}

/** 测试结果的显示名称，缺项保持「未测」。 */
const resultLabels = { passed: '通过', failed: '失败', not_tested: '未测', not_required: '免密' };

/** 把已登记的自定义服务商转为列表项，内置供应商由现有凭据区展示。 */
function customConnections(options: OptionsResponse['model_providers']): Connection[] {
  return (Object.entries(options) as [ModelProvider, ModelProviderOption][]).flatMap(
    /* 只有带地址和版本的记录可进入自定义连接管理。 */ ([provider, item]) =>
      item.base_url && item.version ? [{ ...item, provider, base_url: item.base_url, version: item.version }] : [],
  );
}

/** 连接设置区。独立 Web 可查看和测试已登记的配置，桌面可编辑。 */
export function CustomProviderPanel({ options }: { options: OptionsResponse }) {
  const cache = useQueryClient();
  const [editor, setEditor] = useState<Connection | 'new' | null>(null); // 当前编辑记录，null 收起表单。
  const [saving, setSaving] = useState(false); // 提交期间锁定表单和入口。
  const [failure, setFailure] = useState<string | null>(null); // 保存失败的受控提示。
  const connections = customConnections(options.model_providers);
  const editing = editor && editor !== 'new' ? editor : null;

  /** 打开 value 的编辑表单，切换时清除上一条保存错误。 */
  function open(value: Connection | 'new' | null): void {
    setFailure(null);
    setEditor(value);
  }

  /** 保存已校验的 definition 并重读目录。失败时保留用户输入。 */
  async function save(definition: CustomProviderSettings): Promise<void> {
    setSaving(true);
    setFailure(null);
    try {
      await saveCustomProvider(definition, editing ? { provider: editing.provider, version: editing.version } : undefined);
      setEditor(null);
      try {
        cache.setQueryData(['options'], await getOptions());
      } catch (error) {
        setFailure(errorMessage(error));
      }
    } catch (error) {
      setFailure(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="settings-section" aria-label="自定义服务商">
      <h2>自定义服务商</h2>
      {options.desktop ? <Button disabled={saving || editor !== null} onClick={/* 展开空白登记表单。 */ () => open('new')}>新增服务商</Button>
        : !connections.length && <p className="muted">可在宿主 TOML 中登记自定义服务商。</p>}
      {editor && (
        <Form<CustomProviderSettings>
          key={editing ? editing.provider : 'new'}
          layout="vertical"
          requiredMark={false}
          disabled={saving}
          initialValues={editing ? { name: editing.name, base_url: editing.base_url, default_model: editing.default_model,
            auth: editing.auth, enabled: editing.enabled } : { auth: 'bearer', enabled: true }}
          onFinish={save}
        >
          <Form.Item name="name" label="服务商名称" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={80} />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={2048} placeholder="http://127.0.0.1:11434/v1" />
          </Form.Item>
          <Form.Item name="default_model" label="默认模型 ID" rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item name="auth" label="认证方式">
            <Select virtual={false} options={[{ value: 'bearer', label: 'API Key' }, { value: 'none', label: '本机免密' }]} />
          </Form.Item>
          <Form.Item name="enabled" valuePropName="checked"><Checkbox>启用服务商</Checkbox></Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={saving}>保存服务商</Button>
            <Button onClick={/* 关闭编辑器并舍弃未保存输入。 */ () => open(null)}>取消</Button>
          </Space>
        </Form>
      )}
      {failure && <Alert type="error" title={failure} />}
      {connections.map(/* 地址或默认模型变化后重置上一版探测结果。 */ (connection) => (
        <ProviderRow key={connection.provider + ':' + connection.version + ':' + connection.default_model}
          connection={connection} desktop={Boolean(options.desktop)} canEdit={editor === null && !saving}
          onEdit={/* 用该行当前版本打开编辑。 */ () => open(connection)} />
      ))}
    </section>
  );
}

/** 显示单个连接的公开地址、凭据入口和逐项探测结果。 */
function ProviderRow({ connection, desktop, canEdit, onEdit }: {
  connection: Connection; // 当前公开定义和密钥状态。
  desktop: boolean; // 桌面可调用本机加密存储。
  canEdit: boolean; // 其他表单已打开时为 false。
  onEdit: () => void; // 打开父级编辑表单。
}) {
  const [result, setResult] = useState<ModelProbeResult | null>(null); // 当前行最后一次探测。
  const [error, setError] = useState<string | null>(null); // 探测入口失败或连接版本过期。
  const [testing, setTesting] = useState(false); // 防止重复触发探测。

  /** 调用固定探测接口，不发送会话内容。完成后显示每项状态。 */
  async function probe(): Promise<void> {
    setTesting(true);
    setResult(null);
    setError(null);
    try {
      setResult(await probeModelProvider(connection.provider, connection.version));
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setTesting(false);
    }
  }

  return (
    <article className="settings-section" aria-label={connection.name}>
      <h3>{connection.name} {!connection.enabled && <Tag>已停用</Tag>}</h3>
      <p className="muted"><code>{connection.base_url}</code> · {connection.default_model}</p>
      <Space wrap>
        {desktop && <Button onClick={onEdit} disabled={!canEdit}>编辑</Button>}
        <Button onClick={probe} loading={testing} disabled={!connection.enabled}>测试连接</Button>
      </Space>
      {connection.auth === 'bearer' && (desktop ? (
        <DesktopCredentials provider={connection.provider} name={connection.name} version={connection.version} configured={connection.key_configured} />
      ) : <p><code>{connection.api_key_env}</code> · {connection.key_configured ? '已配置' : '未配置'}</p>)}
      {result && <p role="status">连接{resultLabels[result.connection]} · 认证{resultLabels[result.authentication]} · 流式{resultLabels[result.stream]} · 工具调用{resultLabels[result.tools]}</p>}
      {(error || result?.error_code) && <Alert type="error" title={error || modelFailureLabel(result?.error_code) || '连接测试失败'} />}
    </article>
  );
}
