import { fireEvent, render, screen } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getOptions, saveDesktopSettings } from '../api';
import { ConfigDraftProvider } from '../config';
import { options } from '../test/fixtures';
import { SettingsPage } from '../pages/SettingsPage';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getOptions: vi.fn(),
  saveDesktopSettings: vi.fn(),
}));

beforeEach(/* 清除调用记录，避免把前一例输入当成当前持久设置。 */ () => vi.clearAllMocks());

/** 挂载真实设置页并返回缓存与用户操作器；网络写入由替身收集。 */
function setup() {
  const value = {
    ...options,
    desktop: {
      downloads_directory: 'D:/Downloads',
      data_directory: 'D:/Harness',
      credential_source: 'electron_safe_storage' as const,
    },
  };
  vi.mocked(getOptions).mockResolvedValue(value);
  vi.mocked(saveDesktopSettings).mockResolvedValue(value);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={cache}>
      <MemoryRouter>
        <ConfigDraftProvider>
          <SettingsPage />
        </ConfigDraftProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // 每个控件检查一次可点击性，避免大型设置表单在并发回归中反复计算同一 CSS。
  return {
    user: userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.EachTarget }),
  };
}

it('桌面设置保存到宿主并明确显示已保存', async () => {
  // 从角色控件选择已登记模型，确认默认模型和角色覆盖一起保存。
  const { user } = setup();
  await screen.findByLabelText('默认模型');
  await user.click(screen.getByRole('tab', { name: '桌面' }));
  const download = screen.getByLabelText('保存目录');
  await user.clear(download);
  await user.paste('D:/Harness-Downloads');
  await user.click(screen.getByRole('tab', { name: '运行默认值' }));
  fireEvent.click(screen.getByRole('button', { name: '角色模型' }));
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '审阅' }));
  fireEvent.click(await screen.findByRole('option', { name: '审阅网关 · fixture-review' }));
  await user.click(screen.getByRole('button', { name: /保存设置/ }));
  expect(await screen.findByText('已保存')).toBeInTheDocument();
  expect(saveDesktopSettings).toHaveBeenCalledWith(
    {
      provider: 'custom-fixture',
      requested_model: options.config.requested_model,
      role_models: { review: { provider: 'custom-secondary', requested_model: 'fixture-review' } },
      tools: options.config.tools,
      limits: {
        max_model_turns: options.config.limits.max_model_turns,
        max_tool_calls: options.config.limits.max_tool_calls,
        run_timeout_seconds: options.config.limits.run_timeout_seconds,
      },
    },
    'D:/Harness-Downloads',
  );
}, 15000);
