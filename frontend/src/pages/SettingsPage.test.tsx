import { render, screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { createRun, getOptions } from '../api';
import { ConfigDraftProvider, effectiveConfig } from '../config';
import { DEFAULT_CONTEXT_POLICY } from '../context';
import { config, options } from '../test/fixtures';
import { TaskForm } from '../components/TaskForm';
import { SettingsPage } from './SettingsPage';
import type { OptionsResponse } from '../types';

vi.mock(
  '../api',
  /* 页面编辑不启动模型，提交使用可检查请求体的离线替身。 */ () => ({
    getOptions: vi.fn(),
    createRun: vi.fn(),
    openProject: vi.fn(),
    pickProject: vi.fn(),
    saveProjectSettings: vi.fn(),
    errorMessage: /* 保留受控失败说明，便于检查输入仍然存在。 */ (error: unknown) =>
      error instanceof Error ? error.message : '请求失败',
  }),
);

beforeEach(
  /* 新任务草稿属于各测试自身，不把上一例用户文字带入下一例。 */ () => {
    sessionStorage.clear();
  },
);

/** 以一次真实粘贴替换输入；仍触发控件输入和校验，避免逐字重渲染整张预算表单。 */
async function replaceInput(
  user: ReturnType<typeof userEvent.setup>,
  input: HTMLElement,
  value: string,
): Promise<void> {
  await user.click(input);
  await user.keyboard('{Control>}a{/Control}');
  await user.paste(value);
}

/** 在相同配置上下文内挂载设置与提交两个真实页面，返回用户交互驱动器。 */
function setup(initialPath = '/settings', configuredOptions: OptionsResponse = options) {
  // 配置联动测试明确使用个人目录，不把固定示例伪装成用户选定的项目。
  const projectOptions: OptionsResponse = {
    ...configuredOptions,
    recent_projects: [
      {
        id: 'settings-files',
        label: '配置验收目录',
        path: '/workspace',
        kind: 'workspace',
        available: true,
      },
    ],
  };
  localStorage.setItem('harness-selected-project', 'settings-files');
  vi.mocked(getOptions).mockReset().mockResolvedValue(projectOptions);
  vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线检查阻止实际模型运行'));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <Link to="/runs">前往提交</Link>
          <Routes>
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/runs" element={<TaskForm options={projectOptions} busyId={null} />} />
          </Routes>
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  // 每个控件仍核对一次可点击性，避免JSDOM对整张复杂设置页反复计算相同CSS。
  // 原生布局、抽屉与遮挡另由真实浏览器链路核验，不扩大用例超时。
  return userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.EachTarget });
}

it('上下文设置按 KiB 编辑并按字节随真实任务表单提交，关闭加载也会保留', async () => {
  // 使用两个页面共享的真实草稿，验证输入控件到 API 请求体的换算与传递。
  const user = setup();
  const input = await screen.findByLabelText('整体输入上限（KiB）');
  expect(input).toHaveValue('256');
  expect(screen.getByLabelText('项目指令文件上限（KiB）')).toHaveValue('32');
  expect(screen.getByText(/不等于模型 token 数/)).toBeInTheDocument();
  await replaceInput(user, input, '512');
  await replaceInput(user, screen.getByLabelText('项目指令文件上限（KiB）'), '16.5');
  await user.click(screen.getByRole('switch', { name: '自动加载项目指令' }));
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByRole('status')).toHaveTextContent('已应用到本浏览器');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  await user.click(await screen.findByText(/高级设置/));
  expect(await screen.findByText('输入上限 512 KiB')).toBeInTheDocument();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('检查项目指令加载配置');
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
  await replaceInput(user, input, '64');
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

it('非法目标比例阻止应用，不生成已应用状态', async () => {
  // 无效输入是独立用户动作，不与完整跨页面提交串成一个过长用例。
  const user = setup();
  await user.click(await screen.findByLabelText('压缩策略'));
  await user.click(screen.getByText('混合：规则处理后按需生成摘要'));
  await user.click(screen.getByText('压缩预算与模型窗口'));
  await replaceInput(user, screen.getByLabelText('目标比例（0～1）'), '0.9');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByText('目标比例必须小于触发比例')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
}, 20000);

it('混合压缩与预算设置随真实任务提交', async () => {
  // 保留全部预算与实际提交断言，只在本例设置有效输入，不重复非法提交步骤。
  const user = setup();
  await user.click(await screen.findByLabelText('压缩策略'));
  await user.click(screen.getByText('混合：规则处理后按需生成摘要'));
  await user.click(screen.getByText('压缩预算与模型窗口'));
  await replaceInput(user, screen.getByLabelText('目标比例（0～1）'), '0.5');
  await replaceInput(user, screen.getByLabelText('摘要调用上限（次）'), '4');
  await replaceInput(user, screen.getByLabelText('声明模型窗口（token，可留空）'), '64000');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByRole('status')).toHaveTextContent('已应用到本浏览器');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  await user.click(await screen.findByText(/高级设置/));
  await user.click(await screen.findByLabelText('任务描述'));
  await user.paste('保留兼容接口并测试');
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

it('任务页选择技能后调整模型预算，不丢失已选择的技能', async () => {
  // 设置表单不编辑技能，其应用动作必须保留任务页此前的明确选择。
  const user = setup('/runs', {
    ...options,
    skills: [
      {
        name: 'review-code',
        description: '代码审阅流程。',
        source: '/skills/review-code/SKILL.md',
        available: true,
      },
    ],
  });
  await user.click(await screen.findByText(/高级设置/));
  await user.click(screen.getByRole('checkbox', { name: 'review-code' }));
  await user.click(screen.getByRole('button', { name: '调整配置' }));
  await screen.findByLabelText('整体输入上限（KiB）');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  await screen.findByRole('status');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  await user.click(await screen.findByText(/高级设置/));
  expect(await screen.findByRole('checkbox', { name: 'review-code' })).toBeChecked();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('保持已选审阅技能');
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(
    /* 检查跨页面编辑后的真实提交参数。 */ () => expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createRun).mock.calls[0][0].config.skills).toEqual(['review-code']);
}, 20000);

it('配置页启用局部编辑后，真实任务提交包含 edit_file', async () => {
  // 既有宿主未启用新工具时，由用户明确选择，跨页请求保留权限配置。
  const user = setup();
  const checkbox = await screen.findByRole('checkbox', { name: 'edit_file · 局部编辑文件' });
  expect(checkbox).not.toBeChecked();
  await user.click(checkbox);
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  await screen.findByRole('status');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('只修改加法函数并运行测试');
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(
    /* 等待真实表单校验与请求构造，不直接调用提交函数。 */ () =>
      expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(new Set(vi.mocked(createRun).mock.calls[0][0].config.tools)).toEqual(
    new Set([...config.tools, 'edit_file']),
  );
}, 20000);
