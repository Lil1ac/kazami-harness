import { useEffect, useState } from 'react';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Select, Space, Tag } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage } from '../api';
import { discoverMcp, getMcp, reviewMcp, saveMcp, saveMcpCredential } from '../mcpApi';
import type { McpConnection, McpServerConfig } from '../mcpTypes';
import './Mcp.css';

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

/** 单条连接 item 的完整审核；onEdit 打开配置编辑，desktop 控制秘密写入入口。 */
function Connection({
  item,
  onEdit,
  desktop,
}: {
  item: McpConnection;
  onEdit: () => void;
  desktop: boolean;
}) {
  const cache = useQueryClient();
  const [research, setResearch] = useState(item.research_tools); // 只在用户点击保存后更新授权。
  const [secret, setSecret] = useState(''); // 仅表单内存，不进入配置或浏览器持久化。
  const action = useMutation({
    mutationFn: /* 明确按钮决定产生操作，查看不执行。 */ (operation: () => Promise<unknown>) =>
      operation(),
    onSuccess: /* 更新目录及新任务选项，旧运行历史保持原快照。 */ async () => {
      setSecret('');
      await cache.invalidateQueries({ queryKey: ['mcp'] });
      await cache.invalidateQueries({ queryKey: ['options'] });
    },
  });
  return (
    <article className="mcp-connection">
      <Space wrap>
        <h3>{item.config.label}</h3>
        <Tag>{labels[item.status] || item.status}</Tag>
        <Button size="small" onClick={onEdit} disabled={action.isPending}>
          编辑
        </Button>
      </Space>
      <p>
        <code>{item.config.url || item.config.command}</code>
      </p>
      <details open={!item.trusted}>
        <summary>连接配置与工具定义</summary>
        <pre>{JSON.stringify(item.config, null, 2)}</pre>
        {item.config.transport === 'stdio' && (
          <Alert
            type="warning"
            title="此程序在本机执行，可按系统权限访问文件与网络。审核前请核对程序、参数和目录。"
          />
        )}
        {item.catalog.map(
          /* 真实定义直接作为纯文本供审核，不执行其中内容。 */ (tool) => (
            <details key={tool.name}>
              <summary>{tool.name}</summary>
              <p>{tool.description}</p>
              <pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre>
            </details>
          ),
        )}
        <code>{item.fingerprint}</code>
      </details>
      {item.error && <Alert type="warning" title={item.error} />}
      {action.error && <Alert type="error" title={errorMessage(action.error)} />}
      <Space wrap style={{ marginTop: 12 }}>
        {!item.trusted && (
          <Button
            onClick={
              /* 明确信任只允许服务启动与发现。 */ () =>
                action.mutate(() => reviewMcp(item, 'trust'))
            }
            loading={action.isPending}
          >
            已核对，允许连接
          </Button>
        )}
        <Button
          disabled={!item.trusted || action.isPending}
          onClick={
            /* 用户主动发现，不调用业务工具。 */ () => action.mutate(() => discoverMcp(item))
          }
        >
          连接并发现工具
        </Button>
        <Button
          disabled={
            action.isPending ||
            (!item.enabled &&
              (!item.trusted || !item.catalog.length || item.status === 'needs_review'))
          }
          onClick={
            /* 启停只影响后续真实调用，不回滚历史副作用。 */ () =>
              action.mutate(() => reviewMcp(item, item.enabled ? 'disable' : 'enable'))
          }
        >
          {item.enabled ? '停用' : '启用'}
        </Button>
      </Space>
      {item.catalog.length > 0 && (
        <details style={{ marginTop: 12 }}>
          <summary>研究阶段与子 Agent 可用工具</summary>
          <p>仅勾选你已确认用于研究的工具，具体调用仍遵守宿主审批。</p>
          <Checkbox.Group
            value={research}
            options={item.catalog.map(
              /* 服务注解不作为默认勾选依据。 */ (tool) => ({ label: tool.name, value: tool.name }),
            )}
            onChange={/* 本地编辑不产生授权。 */ (value) => setResearch(value as string[])}
          />
          <Button
            disabled={action.isPending}
            onClick={
              /* 保存后需重新启用，避免旧选择继承新授权。 */ () =>
                action.mutate(() => reviewMcp(item, 'research', research))
            }
          >
            保存研究工具
          </Button>
        </details>
      )}
      {desktop && item.config.transport === 'http' && !item.config.credential_env && (
        <details style={{ marginTop: 12 }}>
          <summary>连接凭据</summary>
          <Space wrap>
            <Input.Password
              aria-label={`${item.config.label}的连接凭据`}
              autoComplete="new-password"
              value={secret}
              onChange={/* 密钥只保留内存，不能回读。 */ (event) => setSecret(event.target.value)}
            />
            <Button
              disabled={!secret || action.isPending}
              onClick={
                /* 原生保存会撤销旧审核。 */ () =>
                  action.mutate(() => saveMcpCredential(item, secret))
              }
            >
              加密保存
            </Button>
            <Button
              disabled={action.isPending}
              onClick={
                /* 空值明确清除此连接分区。 */ () =>
                  action.mutate(() => saveMcpCredential(item, ''))
              }
            >
              清除凭据
            </Button>
          </Space>
        </details>
      )}
    </article>
  );
}

/** 设置页扩展管理；完整配置可审核，保存与启动分开，刷新不自动连接服务。 */
export function McpSettings() {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ['mcp'], queryFn: getMcp });
  const [editing, setEditing] = useState<McpConnection | 'new' | null>(null); // 弹窗只编辑公开配置。
  const [form] = Form.useForm<McpServerConfig & { args_text: string; env_text: string }>();
  const transport = Form.useWatch('transport', form); // 按实际传输显示必要字段。
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
    onSuccess: /* 保存不启动；随后由用户审核真实配置。 */ async () => {
      setEditing(null);
      await cache.invalidateQueries({ queryKey: ['mcp'] });
      await cache.invalidateQueries({ queryKey: ['options'] });
    },
  });
  /** 打开 item 的编辑；new 使用空表单，旧连接固定原指纹，返回 void。 */
  function edit(item: McpConnection | 'new') {
    save.reset();
    setEditing(item);
  }
  return (
    <section className="settings-section mcp-settings">
      <Space wrap>
        <h2>外部工具连接</h2>
        <Button onClick={/* 新建不启动服务。 */ () => edit('new')}>添加连接</Button>
      </Space>
      {query.error && <Alert type="error" title={errorMessage(query.error)} />}
      {query.data?.policy === 'deny' && (
        <Alert type="warning" title="宿主当前禁止外部工具调用，启用连接不能覆盖此限制。" />
      )}
      {!query.data?.items.length && (
        <p className="muted">连接文档、代码托管或你信任的 MCP 服务。</p>
      )}
      {query.data?.items.map(
        /* 版本变化时重建本地研究工具草稿。 */ (item) => (
          <Connection
            key={item.config.id + item.fingerprint}
            item={item}
            desktop={query.data.desktop_credentials}
            onEdit={/* 只打开当前公开配置。 */ () => edit(item)}
          />
        ),
      )}
      <Modal
        title={editing === 'new' ? '添加 MCP 连接' : '编辑 MCP 连接'}
        open={editing !== null}
        onCancel={/* 取消不保存。 */ () => setEditing(null)}
        onOk={/* 唯一配置写入入口。 */ () => save.mutate()}
        confirmLoading={save.isPending}
        okText="保存配置"
        cancelText="取消"
      >
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
        </Form>
        {save.error && <Alert type="error" title={errorMessage(save.error)} />}
      </Modal>
    </section>
  );
}
