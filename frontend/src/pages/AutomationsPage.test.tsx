import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import * as api from '../api';
import { options, config, projectOption } from '../test/fixtures';
import type { AutomationRule } from '../automationTypes';
import { AutomationsPage } from './AutomationsPage';

vi.mock(
  '../api',
  /* 只替换HTTP边界，页面交互和请求构造使用真实实现。 */ () => ({
    getAutomations: vi.fn(),
    getOptions: vi.fn(),
    getAutomationHistory: vi.fn(),
    getAutomationNotifications: vi.fn(),
    getAutomationReview: vi.fn(),
    grantAutomation: vi.fn(),
    automationAction: vi.fn(),
    previewAutomation: vi.fn(),
    saveAutomation: vi.fn(),
    readAutomationNotification: vi.fn(),
    errorMessage: (error: Error) => error?.message || '请求失败',
  }),
);

const rule: AutomationRule = {
  id: '11111111-1111-4111-8111-111111111111',
  revision: 1,
  grant_generation: 1,
  enabled: false,
  has_grant: false,
  invalid_reason: null,
  next_at: '2026-11-09T01:00:00Z',
  next_times: ['2026-11-09T09:00:00+08:00'],
  history_versions: [],
  definition: {
    name: '每日巡检',
    overlap: 'skip',
    task: {
      project_id: projectOption.id,
      base_ref: 'HEAD',
      execution_mode: 'isolated',
      prompt: '检查测试并生成报告',
      config: {},
    },
    schedule: {
      kind: 'cron',
      cron: '0 9 * * mon-fri',
      timezone: 'Asia/Shanghai',
      at: null,
      interval_minutes: null,
      event: null,
      catch_up: false,
      catch_up_minutes: 60,
    },
  },
}; // 固定规则来源，不伪造真实运行完成。

/** 在真实路由和查询上下文挂载页面，返回用户交互器；不连接真实模型。 */
function show() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <AutomationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(
  /* 每个测试隔离服务快照和按钮请求。 */ () => {
    vi.resetAllMocks();
    vi.mocked(api.getOptions).mockResolvedValue({ ...options, recent_projects: [projectOption] });
    vi.mocked(api.getAutomations).mockResolvedValue({
      items: [rule],
      error: null,
      queue_paused: false,
      grants_allowed: false,
    });
    vi.mocked(api.getAutomationNotifications).mockResolvedValue({ items: [] });
    vi.mocked(api.getAutomationHistory).mockResolvedValue({ items: [] });
    vi.mocked(api.automationAction).mockResolvedValue(rule);
    vi.mocked(api.saveAutomation).mockResolvedValue(rule);
    vi.mocked(api.previewAutomation).mockResolvedValue({ items: ['2026-11-09T09:00:00+08:00'] });
    vi.mocked(api.getAutomationReview).mockResolvedValue({
      revision: 1,
      fingerprint: 'a'.repeat(64),
      path: 'D:/project',
      config,
      verifier: null,
      hooks: [],
      grant: null,
    });
  },
);

it('进入页面和查看历史不投递任务，试跑请求绑定当前规则和独立UUID', async () => {
  const user = show();
  expect(await screen.findByText('每日巡检')).toBeInTheDocument();
  expect(api.automationAction).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: /每日巡检/ }));
  await user.click(screen.getByRole('tab', { name: '运行记录' }));
  expect(await screen.findByText('还没有触发记录')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '立即运行' }));
  expect(api.automationAction).toHaveBeenCalledWith(
    rule,
    'run',
    expect.stringMatching(/^[0-9a-f-]{36}$/),
  );
});

it('新建预览与保存相互独立，Git项目默认工作树并保持暂停', async () => {
  const user = show();
  await user.click(await screen.findByRole('button', { name: '新建自动化' }));
  await user.type(screen.getByLabelText('名称'), '工作日检查');
  await user.type(screen.getByRole('textbox', { name: '任务' }), '运行公开测试并报告');
  await user.click(screen.getByRole('button', { name: '预览执行时间' }));
  await waitFor(
    /* 时间预览不保存规则或投递任务。 */ () =>
      expect(api.previewAutomation).toHaveBeenCalledWith(
        expect.objectContaining({ timezone: 'Asia/Shanghai' }),
      ),
  );
  expect(api.saveAutomation).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '保存并保持暂停' }));
  await waitFor(
    /* 验证前端提交的是公开模板，不包含内部授权字段。 */ () =>
      expect(api.saveAutomation).toHaveBeenCalledWith(
        expect.objectContaining({
          name: '工作日检查',
          task: expect.objectContaining({ prompt: '运行公开测试并报告' }),
        }),
        undefined,
      ),
  );
  expect(api.automationAction).not.toHaveBeenCalled();
});

it('宿主未开启持续授权时可审阅配置但不能保存许可', async () => {
  const user = show();
  await user.click(await screen.findByRole('button', { name: /每日巡检/ }));
  await user.click(screen.getByRole('button', { name: '审核授权' }));
  expect(
    await screen.findByText('宿主尚未开启持续授权；任务仍可运行，遇到动作审批时等待处理。'),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存精确授权' })).toBeDisabled();
  expect(api.grantAutomation).not.toHaveBeenCalled();
});

it('待审批历史定位原作业，并保留未验收状态', async () => {
  vi.mocked(api.getAutomationHistory).mockResolvedValue({
    items: [
      {
        id: 'fire',
        status: 'waiting_input',
        scheduled_at: '2026-11-06T09:00:00+08:00',
        reason: null,
        job_id: 'job-original',
        run_id: 'run-original',
        source_run_id: null,
        validation_status: 'unverified',
      },
    ],
  });
  const user = show();
  await user.click(await screen.findByRole('button', { name: /每日巡检/ }));
  await user.click(screen.getByRole('tab', { name: '运行记录' }));
  expect(await screen.findByText('需要你的决定')).toBeInTheDocument();
  expect(screen.getByText('未验收')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '处理此项' })).toHaveAttribute(
    'href',
    '/jobs/job-original',
  );
});

it('试跑响应不明时再次点击复用原请求编号，避免重复触发', async () => {
  vi.mocked(api.automationAction)
    .mockRejectedValueOnce(new Error('连接中断'))
    .mockResolvedValue(rule);
  const user = show();
  await user.click(await screen.findByRole('button', { name: /每日巡检/ }));
  await user.click(screen.getByRole('button', { name: '立即运行' }));
  expect(await screen.findByText('连接中断')).toBeInTheDocument();
  const requestId = vi.mocked(api.automationAction).mock.calls[0][2];
  await user.click(screen.getByRole('button', { name: '立即运行' }));
  await waitFor(
    /* 重试仍指向宿主同一个持久触发。 */ () =>
      expect(api.automationAction).toHaveBeenNthCalledWith(2, rule, 'run', requestId),
  );
});

it('列表不加载逐行历史，启停只调用当前规则动作', async () => {
  const user = show();
  expect(await screen.findByText('工作日 09:00')).toBeInTheDocument();
  expect(screen.queryByText(rule.definition.task.prompt)).not.toBeInTheDocument();
  expect(api.getAutomationHistory).not.toHaveBeenCalled();
  await user.click(screen.getByRole('switch', { name: '启用每日巡检' }));
  expect(api.automationAction).toHaveBeenCalledWith(rule, 'enable', expect.any(String));
});

it('修改可视时间生成相同时区 Cron，预览失效，保存不隐式启用', async () => {
  const user = show();
  await user.click(await screen.findByRole('button', { name: '新建自动化' }));
  await user.type(screen.getByRole('textbox', { name: '名称' }), '晚间检查');
  await user.type(screen.getByRole('textbox', { name: '任务' }), '检查项目');
  await user.click(screen.getByRole('button', { name: '预览执行时间' }));
  expect(await screen.findByText('2026-11-09 09:00:00+08:00')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('触发时间'), { target: { value: '18:30' } });
  expect(screen.queryByText('2026-11-09 09:00:00+08:00')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '保存并保持暂停' }));
  await waitFor(
    /* 控件只改变计划，显式时区和暂停语义保留。 */ () =>
      expect(api.saveAutomation).toHaveBeenCalledWith(
        expect.objectContaining({
          schedule: expect.objectContaining({
            cron: '30 18 * * mon-fri',
            timezone: 'Asia/Shanghai',
          }),
        }),
        undefined,
      ),
  );
  expect(api.automationAction).not.toHaveBeenCalled();
});

it('复杂 Cron 编辑保留原表达式与公开运行配置', async () => {
  const custom = {
    ...rule,
    definition: {
      ...rule.definition,
      schedule: { ...rule.definition.schedule, cron: '*/17 8-19 * * mon,wed' },
      task: { ...rule.definition.task, config: { requested_model: 'test-model' } },
    },
  };
  vi.mocked(api.getAutomations).mockResolvedValue({
    items: [custom],
    error: null,
    queue_paused: false,
    grants_allowed: false,
  });
  const user = show();
  await user.click(await screen.findByRole('button', { name: /每日巡检/ }));
  await user.click(screen.getByRole('button', { name: /编\s*辑/ }));
  expect(screen.getByRole('textbox', { name: /^Cron/ })).toHaveValue('*/17 8-19 * * mon,wed');
  await user.click(screen.getByRole('button', { name: '保存并保持暂停' }));
  await waitFor(
    /* 重新保存不把复杂表达式转换为常用预设。 */ () =>
      expect(api.saveAutomation).toHaveBeenCalledWith(
        expect.objectContaining({
          schedule: custom.definition.schedule,
          task: expect.objectContaining({
            config: expect.objectContaining({ requested_model: 'test-model' }),
          }),
        }),
        custom,
      ),
  );
});

it('没有活动执行时隐藏停止操作，撤销授权必须确认', async () => {
  const granted = { ...rule, has_grant: true };
  vi.mocked(api.getAutomations).mockResolvedValue({
    items: [granted],
    error: null,
    queue_paused: false,
    grants_allowed: true,
  });
  const user = show();
  await user.click(await screen.findByRole('button', { name: /每日巡检/ }));
  await user.click(screen.getByRole('tab', { name: '运行记录' }));
  expect(await screen.findByText('还没有触发记录')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '停止当前执行' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '更多操作' }));
  await user.click(await screen.findByRole('menuitem', { name: '撤销授权' }));
  expect(api.automationAction).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '撤销授权' }));
  await waitFor(
    /* 确认后才请求宿主撤销。 */ () =>
      expect(api.automationAction).toHaveBeenCalledWith(granted, 'revoke', expect.any(String)),
  );
}, 10000);

it('通知单独呈现未读计数，点击结果后读取原通知', async () => {
  vi.mocked(api.getAutomationNotifications).mockResolvedValue({
    items: [
      {
        id: 'notice',
        rule_id: rule.id,
        name: '每日巡检',
        state: 'failed',
        message: '执行失败',
        read: false,
        count: 2,
        job_id: 'job-a',
        run_id: 'run-a',
      },
    ],
  });
  const user = show();
  await user.click(await screen.findByRole('tab', { name: /需要关注1/ }));
  expect(screen.getByText('执行失败（2次）')).toBeInTheDocument();
  expect(api.readAutomationNotification).not.toHaveBeenCalled();
  const link = screen.getByRole('link', { name: /查看与处理/ });
  expect(link).toHaveAttribute('href', '/jobs/job-a');
  await user.click(link);
  expect(api.readAutomationNotification).toHaveBeenCalledWith('notice', expect.anything());
});
