import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
import { expect, it, vi } from 'vitest';
import { createRun, getOptions } from '../api';
import { ConfigDraftProvider, effectiveConfig } from '../config';
import { DEFAULT_CONTEXT_POLICY } from '../context';
import { config, options } from '../test/fixtures';
import { TaskForm } from '../components/TaskForm';
import { SettingsPage } from './SettingsPage';

vi.mock(
  '../api',
  /* 页面编辑不启动模型，提交使用可检查请求体的离线替身。 */ () => ({
    getOptions: vi.fn(),
    createRun: vi.fn(),
    errorMessage: /* 保留受控失败说明，便于检查输入仍然存在。 */ (error: unknown) =>
      error instanceof Error ? error.message : '请求失败',
  }),
);

/** 在相同配置上下文内挂载设置与提交两个真实页面，返回用户交互驱动器。 */
function setup() {
  vi.mocked(getOptions).mockReset().mockResolvedValue(options);
  vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线检查阻止实际模型运行'));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter initialEntries={['/settings']}>
          <Link to="/runs">前往提交</Link>
          <Routes>
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/runs" element={<TaskForm options={options} busyId={null} />} />
          </Routes>
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

it('上下文设置按 KiB 编辑并按字节随真实任务表单提交，关闭加载也会保留', async () => {
  // 使用两个页面共享的真实草稿，验证输入控件到 API 请求体的换算与传递。
  const user = setup();
  const input = await screen.findByLabelText('整体输入上限（KiB）');
  expect(input).toHaveValue('256');
  expect(screen.getByLabelText('项目指令文件上限（KiB）')).toHaveValue('32');
  expect(screen.getByText(/不等于模型 token 数/)).toBeInTheDocument();
  await user.clear(input);
  await user.type(input, '512');
  await user.clear(screen.getByLabelText('项目指令文件上限（KiB）'));
  await user.type(screen.getByLabelText('项目指令文件上限（KiB）'), '16.5');
  await user.click(screen.getByRole('switch', { name: '自动加载项目指令' }));
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByRole('status')).toHaveTextContent('已应用到本浏览器');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  expect(await screen.findByText('输入上限 512 KiB')).toBeInTheDocument();
  await user.type(screen.getByLabelText('任务描述'), '检查项目指令加载配置');
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(
    /* 表单通过校验后才读取发送的数据。 */ () => expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createRun).mock.calls[0][0].config.context).toEqual({
    ...DEFAULT_CONTEXT_POLICY,
    load_project_instructions: false,
    max_input_bytes: 524288,
    max_project_instruction_bytes: 16896,
  });
  expect(config.context.max_input_bytes).toBe(262144);
}, 20000);

it('恢复宿主默认会恢复上下文输入和开关', async () => {
  // 恢复同时清除浏览器草稿与表单字段，避免 UI 值和后续请求不一致。
  const user = setup();
  const input = await screen.findByLabelText('整体输入上限（KiB）');
  await user.clear(input);
  await user.type(input, '64');
  await user.click(screen.getByRole('switch', { name: '自动加载项目指令' }));
  await user.click(screen.getByRole('button', { name: /恢复宿主默认/ }));
  expect(input).toHaveValue('256');
  expect(screen.getByRole('switch', { name: '自动加载项目指令' })).toBeChecked();
}, 10000);

it('上下文策略逐字段合并，旧宿主配置仅为下一次任务补充默认值', () => {
  // 局部覆盖不丢失指令上限；旧任务的 RunView.context 仍由后端事实决定。
  expect(effectiveConfig(config, { context: { max_input_bytes: 8192 } }).context).toEqual({
    ...DEFAULT_CONTEXT_POLICY,
    ...config.context,
    max_input_bytes: 8192,
  });
  const legacy = { ...config };
  Reflect.deleteProperty(legacy, 'context');
  expect(effectiveConfig(legacy, {}).context).toEqual({
    ...DEFAULT_CONTEXT_POLICY,
    ...config.context,
  });
});

it('混合压缩与预算设置随真实任务提交，非法目标比例阻止应用', async () => {
  // 明确配置策略、摘要额度、窗口，覆盖表单到提交请求的公共契约。
  const user = setup();
  const strategy = await screen.findByLabelText('压缩策略');
  await user.click(strategy);
  await user.click(screen.getByText('混合：规则处理后按需生成摘要'));
  await user.click(screen.getByText('压缩预算与模型窗口'));
  const target = screen.getByLabelText('目标比例（0～1）');
  await user.clear(target);
  await user.type(target, '0.9');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByText('目标比例必须小于触发比例')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  await user.clear(target);
  await user.type(target, '0.5');
  await user.clear(screen.getByLabelText('摘要调用上限（次）'));
  await user.type(screen.getByLabelText('摘要调用上限（次）'), '4');
  await user.type(screen.getByLabelText('声明模型窗口（token，可留空）'), '64000');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByRole('status')).toHaveTextContent('已应用到本浏览器');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  await user.type(await screen.findByLabelText('任务描述'), '保留兼容接口并测试');
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(
    /* 等待实际任务表单完成校验并构造请求。 */ () => expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createRun).mock.calls[0][0].config.context).toMatchObject({
    compression: 'hybrid',
    target_ratio: 0.5,
    max_summary_calls: 4,
    model_context_tokens: 64000,
  });
}, 20000);
