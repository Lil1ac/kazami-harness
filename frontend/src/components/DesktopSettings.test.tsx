import { render, screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { getOptions, saveDesktopCredential, saveDesktopSettings } from '../api';
import { ConfigDraftProvider } from '../config';
import { options } from '../test/fixtures';
import { SettingsPage } from '../pages/SettingsPage';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getOptions: vi.fn(),
  saveDesktopCredential: vi.fn(),
  saveDesktopSettings: vi.fn(),
}));

beforeEach(/* 清除调用记录，避免把前一例输入当成当前持久设置。 */ () => vi.clearAllMocks());

/** 挂载真实设置页并返回缓存与用户操作器；失败开关用于验证输入与凭据不会误写。 */
function setup(failed = false) {
  const value = {
    ...options,
    key_configured: false,
    desktop: {
      downloads_directory: 'D:/Downloads',
      data_directory: 'D:/Harness',
      credential_source: 'electron_safe_storage' as const,
    },
  };
  vi.mocked(getOptions).mockResolvedValue(value);
  vi.mocked(saveDesktopSettings).mockResolvedValue(value);
  vi.mocked(saveDesktopCredential).mockImplementation(
    /* 只模拟存在标记，密钥正文不进入响应。 */ async (secret) => {
      if (failed) throw new Error('凭据库暂时不可用');
      return { key_configured: Boolean(secret) };
    },
  );
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={cache}>
      <ConfigDraftProvider>
        <SettingsPage />
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  // 每个控件检查一次可点击性，避免大型设置表单在并发回归中反复计算同一 CSS。
  return {
    cache,
    user: userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.EachTarget }),
  };
}

it('桌面设置保存到宿主并明确显示已保存', async () => {
  const { user } = setup();
  await screen.findByLabelText('保存目录');
  await user.click(screen.getByRole('button', { name: /保存设置/ }));
  expect(await screen.findByText('已保存')).toBeInTheDocument();
  expect(saveDesktopSettings).toHaveBeenCalledWith(
    {
      requested_model: options.config.requested_model,
      tools: options.config.tools,
      limits: {
        max_model_turns: options.config.limits.max_model_turns,
        max_tool_calls: options.config.limits.max_tool_calls,
        run_timeout_seconds: options.config.limits.run_timeout_seconds,
      },
    },
    'D:/Downloads',
  );
});

it('密钥保存后清空输入且不进入查询缓存，并允许明确清除', async () => {
  const { cache, user } = setup();
  await screen.findByLabelText('DeepSeek API Key');
  const input = screen.getByLabelText('DeepSeek API Key');
  await user.click(input);
  await user.paste('desktop-ui-test-secret');
  await user.click(screen.getByRole('button', { name: '保存密钥' }));
  await waitFor(
    /* 保存后输入必须置空，避免切换页面仍保留凭据正文。 */ () => expect(input).toHaveValue(''),
  );
  expect(screen.getByText('已加密保存到本机')).toBeInTheDocument();
  expect(JSON.stringify(cache.getQueryData(['options']))).not.toContain('desktop-ui-test-secret');
  await user.click(screen.getByRole('button', { name: '清除密钥' }));
  await screen.findByText('请配置 DeepSeek API Key');
  expect(saveDesktopCredential).toHaveBeenLastCalledWith('');
});

it('凭据保存失败保留输入以供重试，并且不改变配置状态', async () => {
  const { cache, user } = setup(true);
  const input = await screen.findByLabelText('DeepSeek API Key');
  await user.click(input);
  await user.paste('desktop-retry-secret');
  await user.click(screen.getByRole('button', { name: '保存密钥' }));
  expect(await screen.findByText('凭据库暂时不可用')).toBeInTheDocument();
  expect(input).toHaveValue('desktop-retry-secret');
  expect(cache.getQueryData(['options'])).toMatchObject({ key_configured: false });
});
