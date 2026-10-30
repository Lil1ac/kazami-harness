import { render, screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { createRun, getOptions } from '../api';
import { ConfigDraftProvider, effectiveConfig } from '../config';
import { config, options } from '../test/fixtures';
import { TaskForm } from '../components/TaskForm';
import { SettingsPage } from './SettingsPage';
import type { OptionsResponse } from '../types';

vi.mock(
  '../api',
  /* 页面编辑不启动模型，提交使用可检查请求体的离线替身。 */ () => ({
    getOptions: vi.fn(),
    createRun: vi.fn(),
    createProject: vi.fn(),
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
    localStorage.clear();
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
  // 配置与提交页共享真实目录编号，配置修改不会改变项目选择。
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

it('设置页只公开模型、工具和三项总体预算', async () => {
  // 删除的是调参入口，后端完整策略仍由 options 返回并用于执行。
  setup();
  await screen.findByLabelText('模型名称');
  expect(screen.getAllByRole('spinbutton')).toHaveLength(3);
  expect(screen.getByLabelText('模型轮次（轮）')).toBeInTheDocument();
  expect(screen.getByLabelText('工具调用（次）')).toBeInTheDocument();
  expect(screen.getByLabelText('整个任务超时（秒）')).toBeInTheDocument();
  expect(screen.queryByLabelText('压缩策略')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('整体输入上限（KiB）')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('每轮 HTTP 尝试（次）')).not.toBeInTheDocument();
});

it('可编辑预算随真实任务提交，内部限制沿用宿主配置', async () => {
  // 用非常规宿主阈值证明设置页没有把隐藏字段重置成浏览器默认值。
  const configured: OptionsResponse = {
    ...options,
    config: {
      ...config,
      limits: { ...config.limits, max_http_attempts_per_turn: 6, cleanup_grace_seconds: 12 },
      context: { ...config.context, compression: 'hybrid', inline_tool_bytes: 16384 },
    },
  };
  const user = setup('/settings', configured);
  await replaceInput(user, await screen.findByLabelText('模型名称'), 'offline-custom');
  await replaceInput(user, screen.getByLabelText('模型轮次（轮）'), '7');
  await replaceInput(user, screen.getByLabelText('工具调用（次）'), '30');
  await replaceInput(user, screen.getByLabelText('整个任务超时（秒）'), '120');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  await screen.findByRole('status');
  await user.click(screen.getByRole('link', { name: '前往提交' }));
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('按新的总体预算执行');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(/* 表单校验后检查实际 API 请求。 */ () => expect(createRun).toHaveBeenCalledOnce());
  const request = vi.mocked(createRun).mock.calls[0][0];
  expect(request.config).toMatchObject({
    requested_model: 'offline-custom',
    limits: { max_model_turns: 7, max_tool_calls: 30, run_timeout_seconds: 120 },
  });
  expect(request.config?.context).toBeUndefined();
  expect(Object.keys(request.config?.limits || {}).sort()).toEqual(
    ['max_model_turns', 'max_tool_calls', 'run_timeout_seconds'].sort(),
  );
  expect(configured.config.limits.max_http_attempts_per_turn).toBe(6);
  expect(configured.config.limits.cleanup_grace_seconds).toBe(12);
  expect(configured.config.context.inline_tool_bytes).toBe(16384);
}, 20000);

it('恢复宿主默认会清除已应用的总体预算', async () => {
  // 恢复同步更新表单与浏览器草稿，不改变活动运行。
  const user = setup();
  const input = await screen.findByLabelText('模型轮次（轮）');
  await replaceInput(user, input, '7');
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  await screen.findByRole('status');
  await user.click(screen.getByRole('button', { name: /恢复宿主默认/ }));
  expect(input).toHaveValue('20');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
}, 10000);

it('必填预算为空时不能应用', async () => {
  // 用户清空预算后必须得到校验提示，不能生成已应用状态。
  const user = setup();
  const input = await screen.findByLabelText('模型轮次（轮）');
  await user.clear(input);
  await user.click(screen.getByRole('button', { name: /应用到后续任务/ }));
  expect(await screen.findByText('请填写大于零的预算')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
}, 10000);

it('局部配置覆盖沿用完整宿主策略，不补造另一份默认值', () => {
  // 同一份宿主策略由后端维护；局部覆盖不丢失其他阈值，也不修改原对象。
  const merged = effectiveConfig(config, { context: { max_input_bytes: 8192 } });
  expect(merged.context).toEqual({ ...config.context, max_input_bytes: 8192 });
  expect(config.context.max_input_bytes).toBe(262144);
});

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
  await user.click(screen.getByRole('link', { name: '调整模型与预算' }));
  await screen.findByLabelText('模型名称');
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
