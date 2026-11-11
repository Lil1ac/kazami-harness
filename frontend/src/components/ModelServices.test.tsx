import { fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { probeModelProvider, saveCustomProvider } from '../api';
import { options } from '../test/fixtures';
import type { CustomProviderSettings, OptionsResponse } from '../types';
import { ModelServices } from './ModelServices';

vi.mock('../api', async (original) => ({
  ...(await original<typeof import('../api')>()),
  probeModelProvider: vi.fn(),
  saveCustomProvider: vi.fn(),
}));

it('从空目录一次保存连接、密钥和两个模型，再按当前版本测试', async () => {
  // 模拟认证桌面返回的公开目录，核对密钥只在提交时发送。
  const cache = new QueryClient();
  const desktop: OptionsResponse = { ...options, model_providers: {}, desktop: {
    downloads_directory: 'D:/Downloads', data_directory: 'D:/Harness', credential_source: 'electron_safe_storage',
  } };
  const definition: CustomProviderSettings = {
    name: '测试网关', base_url: 'https://gateway.test/v1', default_model: 'fast', models: ['quality'], auth: 'bearer', enabled: true,
  };
  const version = 'b'.repeat(64);
  const updated: OptionsResponse = { ...desktop, model_providers: {
    'custom-gateway': { ...definition, version, api_key_env: null, key_configured: true },
  } };
  vi.mocked(saveCustomProvider).mockResolvedValue({ provider: 'custom-gateway', options: updated });
  vi.mocked(probeModelProvider).mockResolvedValue({ connection: 'passed', authentication: 'passed', stream: 'passed', tools: 'passed', error_code: null });
  const view = render(<QueryClientProvider client={cache}><ModelServices options={desktop} /></QueryClientProvider>);
  expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '添加服务商' }));
  const drawer = await screen.findByRole('dialog', { name: '添加服务商' });
  fireEvent.change(within(drawer).getByLabelText('名称'), { target: { value: definition.name } });
  fireEvent.change(within(drawer).getByLabelText('Base URL'), { target: { value: definition.base_url } });
  fireEvent.change(within(drawer).getByLabelText('API Key'), { target: { value: 'offline-test-secret' } });
  fireEvent.change(within(drawer).getByLabelText('默认模型 ID'), { target: { value: definition.default_model } });
  fireEvent.click(within(drawer).getByRole('button', { name: '添加模型' }));
  fireEvent.change(await within(drawer).findByLabelText('模型 ID 2'), { target: { value: 'quality' } });
  fireEvent.click(within(drawer).getByRole('button', { name: /^保\s*存$/ }));
  await vi.waitFor(() => expect(cache.getQueryData(['options'])).toEqual(updated));
  expect(saveCustomProvider).toHaveBeenCalledWith(definition, 'offline-test-secret', undefined);
  expect(JSON.stringify(cache.getQueryData(['options']))).not.toContain('offline-test-secret');
  view.rerender(<QueryClientProvider client={cache}><ModelServices options={updated} /></QueryClientProvider>);
  fireEvent.click(within(screen.getByRole('listitem')).getByRole('button', { name: '测试连接' }));
  expect(await screen.findByRole('status')).toHaveTextContent('流式输出和工具调用可用');
  expect(probeModelProvider).toHaveBeenCalledWith('custom-gateway', version);
});
