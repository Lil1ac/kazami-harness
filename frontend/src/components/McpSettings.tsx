import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Select, Skeleton, Space } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage } from '../api';
import { discoverMcp, getMcp, reviewMcp, saveMcp, saveMcpCredential } from '../mcpApi';
import type { McpConnection, McpServerConfig } from '../mcpTypes';
import { ApiOutlined, PlusOutlined } from '@ant-design/icons';
import { IntegrationDrawer, IntegrationRow, IntegrationStatus } from './IntegrationCatalog';

const labels: Record<string, string> = {
  untrusted: '待审核',
  disconnected: '待连接',
  discovered: '已发现工具',
  ready: '已启用',
  disabled: '已停用',
  auth_required: '需要凭据',
  error: '连接失败',
  needs_review: '需要核对',
}; // 管理状态与运行结果分开。
const initial: McpServerConfig = {
  id: '',
  label: '',
  transport: 'http',
  url: '',
  command: '',
  args: [],
  cwd: '',
  env_vars: [],
  credential_env: '',
  credential_header: 'Authorization',
  startup_timeout_seconds: 15,
  tool_timeout_seconds: 60,
}; // 新连接不含授权和秘密。

/** 返回连接的真实状态；工具数量只代表已发现的目录。 */
function connectionStatus(item: McpConnection) {
  const tone =
    item.status === 'ready'
      ? 'ready'
      : item.status === 'error'
        ? 'error'
        : ['untrusted', 'auth_required', 'needs_review'].includes(item.status)
          ? 'attention'
          : 'neutral';
  return <IntegrationStatus label={labels[item.status] || item.status} tone={tone} />;
}

/** 展示 item 当前版本的连接配置与工具；blocked 仅限制增加权限的操作。 */
function Connection({
  item,
  onEdit,
  desktop,
  blocked,
}: {
  item: McpConnection; // 当前连接快照，审核和操作绑定该指纹。
  onEdit: () => void; // 打开公开配置编辑表单。
  desktop: boolean; // 桌面可保存加密凭据，网页使用宿主变量。
  blocked: boolean; // 宿主策略禁止外部工具时，锁定审核、连接和启用。
}) {
  const cache = useQueryClient();
  const [research, setResearch] = useState(item.research_tools); // 尚未保存的研究阶段工具名单。
  const [secret, setSecret] = useState(''); // 只在当前详情内存中保存。
  const action = useMutation({
    mutationFn: /* 执行用户刚确认的单个动作。 */ (operation: () => Promise<unknown>) => operation(),
    onSuccess: /* 重新读取服务状态，并刷新任务选择器。 */ async () => {
      setSecret('');
      await cache.invalidateQueries({ queryKey: ['mcp'] });
      await cache.invalidateQueries({ queryKey: ['options'] });
    },
  });
  const config = item.config;
  const local = config.transport === 'stdio';
  const hasTools = item.catalog.length > 0;
  const canEnable = item.trusted && hasTools && item.status !== 'needs_review';
  const canSaveCredential = desktop && !local && !config.credential_env;

  return (
    <div className="integration-detail">
      <div className="integration-toolbar integration-detail-heading">
        {connectionStatus(item)}
        <Button size="small" disabled={action.isPending} onClick={onEdit}>
          编辑配置
        </Button>
      </div>
      {item.error && <Alert type="warning" showIcon title={item.error} />}
      {action.error && <Alert type="error" showIcon title={errorMessage(action.error)} />}
      {blocked && <Alert type="warning" showIcon title="宿主策略已禁止外部工具调用" />}
      <h3>连接配置</h3>
      <dl className="integration-fields">
        <dt>连接方式</dt>
        <dd>{local ? '本机进程（stdio）' : '远程 HTTP'}</dd>
        <dt>{local ? '程序路径' : '服务地址'}</dt>
        <dd>
          <code>{local ? config.command : config.url}</code>
        </dd>
        {local ? (
          <>
            <dt>启动目录</dt>
            <dd>
              <code>{config.cwd}</code>
            </dd>
            <dt>程序参数</dt>
            <dd>
              <code>{JSON.stringify(config.args)}</code>
            </dd>
            {config.env_vars.length > 0 && (
              <>
                <dt>环境变量</dt>
                <dd>
                  <code>{config.env_vars.join('、')}</code>
                </dd>
              </>
            )}
          </>
        ) : (
          <>
            <dt>认证来源</dt>
            <dd>
              {config.credential_env ? (
                <code>{config.credential_env}</code>
              ) : canSaveCredential ? (
                '桌面凭据（如已保存）'
              ) : (
                '未指定'
              )}
            </dd>
          </>
        )}
      </dl>
      {local && !item.trusted && (
        <Alert type="warning" showIcon title="本机程序可按系统权限访问文件和网络" />
      )}
      <details className="integration-disclosure">
        <summary>原始配置与版本</summary>
        <pre>{JSON.stringify(config, null, 2)}</pre>
        <code>{item.fingerprint}</code>
      </details>
      {(!blocked || item.enabled) && (
        <div className="integration-actions">
          {!blocked && !item.trusted && (
            <Button
              type="primary"
              loading={action.isPending}
              onClick={
                /* 信任仅允许后续连接与发现，绑定当前指纹。 */ () =>
                  action.mutate(() => reviewMcp(item, 'trust'))
              }
            >
              已核对，允许连接
            </Button>
          )}
          {!blocked && item.trusted && canEnable && !item.enabled && (
            <Button
              type="primary"
              loading={action.isPending}
              onClick={
                /* 启用后进入后续任务的可选工具目录。 */ () =>
                  action.mutate(() => reviewMcp(item, 'enable'))
              }
            >
              启用连接
            </Button>
          )}
          {!blocked && item.trusted && (
            <Button
              type={canEnable ? 'default' : 'primary'}
              disabled={action.isPending}
              onClick={
                /* 主动发现工具，不调用业务工具。 */ () => action.mutate(() => discoverMcp(item))
              }
            >
              {hasTools ? '重新发现工具' : '连接并发现工具'}
            </Button>
          )}
          {item.enabled && (
            <Button
              disabled={action.isPending}
              onClick={/* 停用当前版本。 */ () => action.mutate(() => reviewMcp(item, 'disable'))}
            >
              停用连接
            </Button>
          )}
        </div>
      )}
      {hasTools && (
        <details className="integration-disclosure">
          <summary>工具定义（{item.catalog.length}）</summary>
          {item.catalog.map(
            /* 原始 Schema 作为纯文本按需展示。 */ (tool) => (
              <details className="integration-disclosure" key={tool.name}>
                <summary>
                  <code>{tool.name}</code>
                </summary>
                {tool.description && <p>{tool.description}</p>}
                <pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre>
              </details>
            ),
          )}
        </details>
      )}
      {hasTools && (
        <details className="integration-disclosure">
          <summary>研究阶段与子 Agent 可用工具</summary>
          <Checkbox.Group
            value={research}
            disabled={action.isPending}
            options={item.catalog.map(
              /* 使用服务发现的工具名，不根据注解预选。 */ (tool) => ({
                label: tool.name,
                value: tool.name,
              }),
            )}
            onChange={/* 仅记录本地草稿。 */ (value) => setResearch(value as string[])}
          />
          <div className="integration-actions">
            <Button
              disabled={action.isPending}
              onClick={
                /* 保存范围后需重新启用当前连接。 */ () =>
                  action.mutate(() => reviewMcp(item, 'research', research))
              }
            >
              保存研究工具
            </Button>
          </div>
        </details>
      )}
      {canSaveCredential && (
        <details
          className="integration-disclosure"
          open={item.status === 'auth_required' || undefined}
        >
          <summary>连接凭据</summary>
          <Input.Password
            aria-label={config.label + '的连接凭据'}
            autoComplete="new-password"
            value={secret}
            onChange={/* 密钥只保留在当前表单中。 */ (event) => setSecret(event.target.value)}
          />
          <div className="integration-actions">
            <Button
              disabled={!secret || action.isPending}
              onClick={
                /* 加密保存后由后端撤销旧审核。 */ () =>
                  action.mutate(() => saveMcpCredential(item, secret))
              }
            >
              加密保存
            </Button>
            <Button
              disabled={action.isPending}
              onClick={
                /* 空值仅清除当前连接的凭据分区。 */ () =>
                  action.mutate(() => saveMcpCredential(item, ''))
              }
            >
              清除凭据
            </Button>
          </div>
        </details>
      )}
    </div>
  );
}

/** 设置页扩展管理；完整配置可审核，保存与启动分开，刷新不自动连接服务。 */
export function McpSettings() {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ['mcp'], queryFn: getMcp });
  const drawerTrigger = useRef<HTMLElement | null>(null); // 返回连接行或工具栏的添加按钮。
  const [selected, setSelected] = useState<string | null>(null); // 当前详情的稳定连接编号。
  const [editing, setEditing] = useState<McpConnection | 'new' | null>(null); // 弹窗只编辑公开配置。
  const [form] = Form.useForm<McpServerConfig & { args_text: string; env_text: string }>();
  const transport = Form.useWatch('transport', form) || initial.transport; // 按实际传输显示必要字段。
  useEffect(
    /* 弹窗真正挂载后填入公开值；不预先渲染隐藏表单，避免设置页额外布局和无效 Form 绑定。 */ () => {
      if (!editing) return;
      const config = editing === 'new' ? initial : editing.config;
      form.setFieldsValue({
        ...config,
        args_text: JSON.stringify(config.args),
        env_text: config.env_vars.join(' '),
      });
    },
    [editing, form],
  );
  const save = useMutation({
    mutationFn: /* 仅显式提交时保存。 */ async () => {
      const values = await form.validateFields();
      const { args_text, env_text, ...config } = values;
      return saveMcp(
        {
          ...initial,
          ...config,
          ...(config.transport === 'http'
            ? { command: '', cwd: '', args: [], env_vars: [] }
            : {
                url: '',
                credential_env: '',
                args: JSON.parse(args_text || '[]'),
                env_vars: (env_text || '').split(/\s+/).filter(Boolean),
              }),
        },
        editing && editing !== 'new' ? editing.fingerprint : undefined,
      );
    },
    onSuccess: /* 读回新目录后显示保存的连接，方便用户审核配置。 */ async (saved) => {
      await cache.invalidateQueries({ queryKey: ['mcp'] });
      await cache.invalidateQueries({ queryKey: ['options'] });
      setSelected(saved.config.id);
      setEditing(null);
    },
  });
  /** 打开 item 的编辑；new 使用空表单，旧连接固定原指纹，返回 void。 */
  function edit(item: McpConnection | 'new') {
    save.reset();
    setEditing(item);
  }
  const current = query.data?.items.find((item) => item.config.id === selected);
  const blocked = query.data?.policy === 'deny';
  return (
    <section className="integration-catalog" aria-label="MCP 连接">
      <header className="integration-toolbar">
        <h2>MCP 连接</h2>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={
            /* 记住工具栏入口后打开空表单。 */ (event) => {
              drawerTrigger.current = event.currentTarget;
              edit('new');
            }
          }
        >
          添加连接
        </Button>
      </header>
      {query.error && <Alert type="error" showIcon title={errorMessage(query.error)} />}
      {blocked && <Alert type="warning" showIcon title="宿主策略已禁止外部工具调用" />}
      {query.isPending && <Skeleton active paragraph={{ rows: 2 }} />}
      {query.data &&
        (query.data.items.length ? (
          <ul className="integration-list">
            {query.data.items.map(
              /* 选择只读取已有配置。 */ (item) => (
                <IntegrationRow
                  key={item.config.id}
                  name={item.config.label}
                  icon={<ApiOutlined />}
                  status={connectionStatus(item)}
                  summary={
                    (item.config.transport === 'http' ? '远程 HTTP' : '本机进程') +
                    ' · ' +
                    item.catalog.length +
                    ' 个工具'
                  }
                  onOpen={
                    /* 保留目录入口和稳定编号，刷新后仍显示同一条连接。 */ (trigger) => {
                      drawerTrigger.current = trigger;
                      setSelected(item.config.id);
                    }
                  }
                />
              ),
            )}
          </ul>
        ) : (
          <p className="integration-empty">还没有 MCP 连接</p>
        ))}
      <IntegrationDrawer
        title={
          editing === 'new'
            ? '添加 MCP 连接'
            : editing
              ? '编辑 MCP 连接'
              : current?.config.label || 'MCP 连接'
        }
        open={Boolean(editing || current)}
        returnFocus={drawerTrigger.current}
        onClose={
          /* 编辑取消返回连接详情，详情关闭返回列表。 */ () => {
            if (save.isPending) return;
            if (editing) setEditing(null);
            else setSelected(null);
          }
        }
        footer={
          editing && (
            <div className="integration-actions">
              <Button disabled={save.isPending} onClick={/* 取消不提交。 */ () => setEditing(null)}>
                取消
              </Button>
              <Button
                type="primary"
                loading={save.isPending}
                onClick={/* 只保存当前配置。 */ () => save.mutate()}
              >
                保存配置
              </Button>
            </div>
          )
        }
      >
        {!editing && current && (
          <Connection
            key={current.config.id + current.fingerprint}
            item={current}
            desktop={Boolean(query.data?.desktop_credentials)}
            blocked={blocked}
            onEdit={/* 编辑使用当前详情看到的版本。 */ () => edit(current)}
          />
        )}
        {editing && (
          <>
            <Form form={form} layout="vertical" requiredMark={false}>
              <Form.Item
                name="id"
                label="连接标识"
                rules={[
                  {
                    required: true,
                    pattern: /^[a-z][a-z0-9_-]{0,31}$/,
                    message: '使用小写字母开头的字母、数字、下划线或连字符',
                  },
                ]}
              >
                <Input disabled={editing !== 'new'} />
              </Form.Item>
              <Form.Item name="label" label="名称" rules={[{ required: true, whitespace: true }]}>
                <Input maxLength={100} />
              </Form.Item>
              <Form.Item name="transport" label="连接方式">
                <Select
                  options={[
                    { value: 'http', label: '远程 Streamable HTTP' },
                    { value: 'stdio', label: '本机 stdio 程序' },
                  ]}
                />
              </Form.Item>
              {transport === 'http' ? (
                <Form.Item name="url" label="服务地址" rules={[{ required: true }]}>
                  <Input placeholder="https://developers.openai.com/mcp" />
                </Form.Item>
              ) : (
                <>
                  <Form.Item name="command" label="程序绝对路径" rules={[{ required: true }]}>
                    <Input />
                  </Form.Item>
                  <Form.Item name="cwd" label="启动目录绝对路径" rules={[{ required: true }]}>
                    <Input />
                  </Form.Item>
                  <Form.Item name="args_text" label="程序参数（JSON 数组）">
                    <Input.TextArea rows={2} />
                  </Form.Item>
                  <Form.Item name="env_text" label="传递的环境变量名（空格分隔）">
                    <Input />
                  </Form.Item>
                </>
              )}
              <details className="integration-disclosure">
                <summary>认证与超时设置</summary>
                {transport === 'http' && (
                  <>
                    <Form.Item name="credential_env" label="凭据环境变量名（可选）">
                      <Input autoComplete="off" />
                    </Form.Item>
                    <Form.Item name="credential_header" label="认证方式">
                      <Select
                        options={[
                          { value: 'Authorization', label: 'Bearer Token' },
                          { value: 'X-API-Key', label: 'X-API-Key' },
                        ]}
                      />
                    </Form.Item>
                  </>
                )}
                <Space>
                  <Form.Item name="startup_timeout_seconds" label="连接超时（秒）">
                    <InputNumber min={1} max={120} />
                  </Form.Item>
                  <Form.Item name="tool_timeout_seconds" label="调用超时（秒）">
                    <InputNumber min={1} max={600} />
                  </Form.Item>
                </Space>
              </details>
            </Form>
            {save.error && <Alert type="error" title={errorMessage(save.error)} />}
          </>
        )}
      </IntegrationDrawer>
    </section>
  );
}
