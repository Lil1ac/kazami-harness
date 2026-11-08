import { fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { getOptions, probeModelProvider, saveCustomProvider } from '../api';
import { options } from '../test/fixtures';
import type { CustomProviderSettings, OptionsResponse } from '../types';
import { CustomProviderPanel } from './CustomProviderPanel';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  getOptions: vi.fn(),
  probeModelProvider: vi.fn(),
  saveCustomProvider: vi.fn(),
}));

it('设置页登记并测试自定义服务商，探测只提交连接版本', async () => {
  // 挂载实际的选项缓存和面板，保存后用服务端返回的版本驱动测试按钮。
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const desktop: OptionsResponse = { ...options, desktop: {
    downloads_directory: 'D:/Downloads', data_directory: 'D:/Harness', credential_source: 'electron_safe_storage',
  } };
  const definition: CustomProviderSettings = {
    name: '测试网关', base_url: 'https://gateway.test/v1', default_model: 'gateway-model', auth: 'bearer', enabled: true,
  };
  const version = 'b'.repeat(64);
  const updated: OptionsResponse = { ...desktop, model_providers: {
    ...desktop.model_providers, 'custom-gateway': { ...definition, version, api_key_env: null, key_configured: true },
  } };
  vi.mocked(saveCustomProvider).mockResolvedValue({ provider: 'custom-gateway' });
  vi.mocked(getOptions).mockResolvedValue(updated);
  vi.mocked(probeModelProvider).mockResolvedValue({ connection: 'passed', authentication: 'passed', stream: 'passed', tools: 'passed', error_code: null });
  const view = render(<QueryClientProvider client={cache}><CustomProviderPanel options={desktop} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole('button', { name: '新增服务商' }));
  fireEvent.change(screen.getByLabelText('服务商名称'), { target: { value: definition.name } });
  fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: definition.base_url } });
  fireEvent.change(screen.getByLabelText('默认模型 ID'), { target: { value: definition.default_model } });
  fireEvent.click(screen.getByRole('button', { name: '保存服务商' }));
  await screen.findByRole('button', { name: '新增服务商' });
  await vi.waitFor(() => expect(cache.getQueryData(['options'])).toEqual(updated));
  expect(saveCustomProvider).toHaveBeenCalledWith(definition, undefined);
  view.rerender(<QueryClientProvider client={cache}><CustomProviderPanel options={updated} /></QueryClientProvider>);
  fireEvent.click(within(screen.getByRole('article', { name: '测试网关' })).getByRole('button', { name: '测试连接' }));
  expect(await screen.findByRole('status')).toHaveTextContent('工具调用通过');
  expect(probeModelProvider).toHaveBeenCalledWith('custom-gateway', version);
});
