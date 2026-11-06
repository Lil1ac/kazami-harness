import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { McpSelector } from './McpSelector';
import { McpSettings } from './McpSettings';
import { McpEvidence } from './McpEvidence';
import { discoverMcp, getMcp, reviewMcp } from '../mcpApi';
import { activitySteps } from '../activity';
import type { RunEvent } from '../types';
import { makeEvent } from '../test/fixtures';
import { request } from '../api';
import type { McpConnection, McpToolOption } from '../mcpTypes';

vi.mock(
  '../mcpApi',
  /* 只模拟连接 HTTP 边界，用户按钮仍需显式触发。 */ () => ({
    getMcp: vi.fn(),
    saveMcp: vi.fn(),
    reviewMcp: vi.fn(),
    discoverMcp: vi.fn(),
    saveMcpCredential: vi.fn(),
  }),
);
vi.mock(
  '../api',
  /* 历史接口返回固定当轮来源，不读取当前连接。 */ () => ({
    errorMessage: () => '读取失败',
    request: vi.fn(async () => ({
      tools: [{ fingerprint: 'old-version' }],
      items: [
        {
          seq: 8,
          type: 'mcp_call_result',
          name: 'mcp_abc',
          server_id: 'docs',
          tool: 'search',
          fingerprint: 'old-version',
          sources: ['https://example.test/docs/v1'],
          result: { content: '原始结果' },
        },
      ],
    })),
  }),
);

const option: McpToolOption = {
  name: 'mcp_' + 'a'.repeat(24),
  fingerprint: 'a'.repeat(64),
  server_id: 'docs',
  server_label: '官方文档',
  tool_name: 'search',
  description: '检索文档',
  research: false,
}; // 固定可选择版本。
const connection: McpConnection = {
  config: {
    id: 'docs',
    label: '官方文档',
    transport: 'http',
    url: 'https://example.test/mcp',
    command: '',
    args: [],
    cwd: '',
    env_vars: [],
    credential_env: '',
    credential_header: 'Authorization',
    startup_timeout_seconds: 15,
    tool_timeout_seconds: 60,
  },
  fingerprint: option.fingerprint,
  revision: 1,
  trusted: false,
  enabled: false,
  catalog: [],
  research_tools: [],
  status: 'untrusted',
  error: null,
  protocol_version: null,
}; // 未审核的保存结果。

/** 用独立查询缓存渲染 node；无自动重试，返回 void，不共享其他用例的连接状态。 */
function show(node: React.ReactNode) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={cache}>{node}</QueryClientProvider>);
}

beforeEach(
  /* 每个用例从未审核连接开始。 */ () => {
    vi.mocked(getMcp).mockResolvedValue({
      items: [structuredClone(connection)],
      tools: [],
      desktop_credentials: false,
      policy: 'ask',
    });
  },
);

it('查看连接不会自动信任、发现或启用，按钮只提交当前指纹', async () => {
  vi.mocked(reviewMcp).mockResolvedValue({ ...connection, trusted: true, status: 'disconnected' });
  show(<McpSettings />);
  await screen.findByText('官方文档');
  expect(discoverMcp).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: '连接并发现工具' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '已核对，允许连接' }));
  await waitFor(
    /* 明确按钮只信任连接，不批准工具参数。 */ () =>
      expect(reviewMcp).toHaveBeenCalledWith(connection, 'trust'),
  );
  expect(discoverMcp).not.toHaveBeenCalled();
});

it('工具版本变化必须重新勾选，取消返回空列表', () => {
  const changed = vi.fn();
  const old = { name: option.name, fingerprint: 'b'.repeat(64) };
  render(<McpSelector options={[option]} selected={[old]} onChange={changed} />);
  expect(screen.getByText(/工具已停用或版本变化/)).toBeVisible();
  fireEvent.click(screen.getByLabelText('官方文档 / search'));
  expect(changed).toHaveBeenLastCalledWith([
    { name: option.name, fingerprint: option.fingerprint },
  ]);
  fireEvent.click(screen.getByLabelText(/工具已停用或版本变化/));
  expect(changed).toHaveBeenLastCalledWith([]);
});

it('历史展示固定来源和原始结果，不连接远端或重建来源', async () => {
  show(<McpEvidence runId="old-run" active={false} />);
  const link = await screen.findByRole('link', { name: 'https://example.test/docs/v1' });
  expect(link).toHaveAttribute('href', 'https://example.test/docs/v1');
  fireEvent.click(screen.getByText('当轮结果与版本'));
  expect(screen.getByText('old-version', { selector: 'code' })).toBeVisible();
  expect(screen.getByText(/原始结果/)).toBeVisible();
});

it('执行过程显示宿主固定的服务与工具名称，调用标识仍用于结果配对', () => {
  const events: RunEvent[] = [
    {
      ...makeEvent(1),
      type: 'tool_started',
      payload: {
        call_id: 'external-call',
        name: option.name,
        title: '官方文档 / search',
        arguments_json: '{"query":"input"}',
      },
    },
    {
      ...makeEvent(2),
      type: 'tool_result',
      payload: {
        call_id: 'external-call',
        name: option.name,
        result: { ok: true, data: { sources: ['https://example.test/docs/v1'] } },
      },
    },
  ];
  const [step] = activitySteps(events);
  expect(step.label).toBe('官方文档 / search');
  expect(step.id).toBe('external-call');
  expect(step.state).toBe('passed');
});

it('长来源列表首屏三条，其余可展开且保留原链接', async () => {
  vi.mocked(request).mockResolvedValueOnce({
    tools: [{}],
    items: [
      {
        seq: 2,
        type: 'mcp_call_result',
        name: option.name,
        server_id: 'docs',
        sources: [1, 2, 3, 4, 5].map(
          /* 构造确定的真实回执替身链接。 */ (index) => `https://example.test/${index}`,
        ),
      },
    ],
  });
  show(<McpEvidence runId="many-sources" active={false} />);
  await screen.findByRole('link', { name: 'https://example.test/1' });
  expect(screen.getByRole('link', { name: 'https://example.test/4' })).not.toBeVisible();
  fireEvent.click(screen.getByText('更多来源（2）'));
  expect(screen.getByRole('link', { name: 'https://example.test/4' })).toHaveAttribute(
    'href',
    'https://example.test/4',
  );
});
