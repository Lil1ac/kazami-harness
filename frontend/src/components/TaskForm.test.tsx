import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ConfigDraftProvider } from '../config';
import { createRun } from '../api';
import { options, projectOption } from '../test/fixtures';
import { TaskForm } from './TaskForm';
import type { OptionsResponse } from '../types';

vi.mock(
  '../api',
  /* 提交全部由离线替身处理，保留安全错误文本。 */ () => ({
    createRun: vi.fn(),
    errorMessage: (error: unknown) => (error instanceof Error ? error.message : '提交失败'),
  }),
);

/** 在真实 Query、Router 和配置上下文中挂载表单，返回用户交互驱动器。 */
function setup(busyId: string | null = null, configuredOptions: OptionsResponse = options) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ConfigDraftProvider>
          <TaskForm options={configuredOptions} busyId={busyId} />
        </ConfigDraftProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe('提交任务', /* 验证输入保留、空任务阻止和宿主预设的受控使用。 */ () => {
  it('空描述只显示校验提示，不调用任务接口', async () => /* 失败校验不创建任何运行。 */ {
    vi.mocked(createRun).mockReset();
    const user = setup();
    await user.click(screen.getByRole('button', { name: /开始执行/ }));
    expect(await screen.findByText('请描述要完成的任务')).toBeInTheDocument();
    expect(createRun).not.toHaveBeenCalled();
  });
  it('提交失败后保留任意任务描述与产物路径', async () => /* 防止用户因网络错误重新输入长任务。 */ {
    vi.mocked(createRun).mockReset().mockRejectedValue(new Error('运行槽已被占用'));
    const user = setup();
    await user.type(screen.getByLabelText('任务描述'), '清洗这份 CSV');
    await user.type(screen.getByLabelText(/预期产物/), 'clean.csv');
    await user.click(screen.getByRole('button', { name: /开始执行/ }));
    expect(await screen.findByText('运行槽已被占用')).toBeInTheDocument();
    expect(screen.getByLabelText('任务描述')).toHaveValue('清洗这份 CSV');
    expect(screen.getByLabelText(/预期产物/)).toHaveValue('clean.csv');
    expect(createRun).toHaveBeenCalledWith(
      {
        prompt: '清洗这份 CSV',
        workspace_id: 'logs',
        verifier_id: null,
        expected_outputs: ['clean.csv'],
        config: {},
      },
      expect.anything(),
    );
  });
  it('日志样例只填充草稿，不自动执行', async () => /* 固定验收的范围必须显式可见。 */ {
    vi.mocked(createRun).mockReset();
    const user = setup();
    await user.click(screen.getByRole('button', { name: '填充日志样例' }));
    await waitFor(
      /* 等待表单更新。 */ () =>
        expect(screen.getByLabelText(/预期产物/)).toHaveValue('analyze_logs.py\nreport.json'),
    );
    expect(screen.getByText(/样例已启用读取、搜索、写入、命令四类工具/)).toBeInTheDocument();
    expect(createRun).not.toHaveBeenCalled();
  });
  it('日志样例在宿主仅允许读取的默认配置下补齐本次所需工具', async () => {
    // 只调整本浏览器覆盖，不修改宿主默认配置或自动执行任务。
    vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线测试阻止真正提交'));
    const user = setup(null, { ...options, config: { ...options.config, tools: ['read_file'] } });
    await user.click(screen.getByRole('button', { name: '填充日志样例' }));
    await user.click(screen.getByRole('button', { name: /开始执行/ }));
    await waitFor(/* 等待异步表单校验结束。 */ () => expect(createRun).toHaveBeenCalledOnce());
    expect(vi.mocked(createRun).mock.calls[0][0].config.tools).toEqual([
      'read_file',
      'search_files',
      'write_file',
      'run_command',
    ]);
    expect(options.config.tools).toEqual([
      'read_file',
      'search_files',
      'write_file',
      'run_command',
    ]);
  });
  it('运行槽占用时禁止重复提交', /* 控件提前提示，后端仍需独立校验竞争。 */ () => {
    setup('busy-id');
    expect(screen.getByRole('button', { name: /开始执行/ })).toBeDisabled();
    expect(screen.getByText('已有任务正在运行')).toBeInTheDocument();
  });
});

describe('代码项目提交', /* 项目和普通工作区严格二选一，宿主测试命令只读。 */ () => {
  it('项目提交只包含登记 ID 和提交基线，不夹带工作区或验收器', async () => {
    // 切换任务类型前已有工作区值，提交时仍必须主动过滤掉另一种目标。
    vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线阻止实际启动'));
    const user = setup(null, { ...options, projects: [projectOption] });
    await user.click(screen.getByText('代码项目', { exact: true }));
    expect(screen.getByText('有未提交改动 · 不带入任务')).toBeInTheDocument();
    expect(screen.getByText('python -m pytest -q')).toBeInTheDocument();
    expect(screen.getByText(/原目录中的未提交改动/)).toBeInTheDocument();
    expect(screen.queryByLabelText('独立验收')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('任务描述'), '修复失败测试');
    await user.clear(screen.getByLabelText(/提交基线/));
    await user.type(screen.getByLabelText(/提交基线/), 'main');
    await user.click(screen.getByRole('button', { name: /开始执行/ }));
    await waitFor(/* 等待表单的异步校验和提交。 */ () => expect(createRun).toHaveBeenCalledOnce());
    expect(vi.mocked(createRun).mock.calls[0][0]).toEqual({
      prompt: '修复失败测试',
      project_id: 'sample-project',
      base_ref: 'main',
      expected_outputs: [],
      config: {},
    });
    expect(await screen.findByText('离线阻止实际启动')).toBeInTheDocument();
    expect(screen.getByLabelText(/提交基线/)).toHaveValue('main');
  });
  it('仅有项目时默认代码任务，空基线使用 HEAD', async () => {
    // 不要求用户额外登记普通工作区；空输入具有清楚的 HEAD 语义。
    vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线提交'));
    const user = setup(null, { ...options, workspaces: [], projects: [projectOption] });
    await user.type(screen.getByLabelText('任务描述'), '补充边界检查');
    await user.clear(screen.getByLabelText(/提交基线/));
    await user.click(screen.getByRole('button', { name: /开始执行/ }));
    await waitFor(/* 表单校验完成后检查请求。 */ () => expect(createRun).toHaveBeenCalledOnce());
    expect(vi.mocked(createRun).mock.calls[0][0]).toMatchObject({
      project_id: projectOption.id,
      base_ref: 'HEAD',
    });
  });
  it('无登记项目时不改变旧任务入口', () => {
    // 保持旧后端选项和历史配置的普通工作区交互。
    setup();
    expect(screen.queryByRole('radio', { name: '代码项目' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('工作区')).toBeInTheDocument();
    expect(screen.getByLabelText(/独立验收/)).toBeInTheDocument();
  });
  it('不可用项目禁止执行并显示宿主原因', () => {
    // 默认只预选可用项；没有可用项时按钮保持禁用。
    setup(null, {
      ...options,
      workspaces: [],
      projects: [{ ...projectOption, available: false, error: '仓库暂不可用' }],
    });
    expect(screen.getByRole('button', { name: /开始执行/ })).toBeDisabled();
    expect(screen.getByText('仓库暂不可用')).toBeInTheDocument();
  });
});

it('选择技能只提交名称，不夹带技能目录、正文或工具权限', async () => {
  // 目录由宿主管理；首次手动选择与取消使用同一真实表单提交入口。
  vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线技能提交'));
  const user = setup(null, {
    ...options,
    skills: [
      {
        name: 'review-code',
        description: '先读测试再修改。',
        source: '/skills/review-code/SKILL.md',
        available: true,
      },
      {
        name: 'broken',
        description: '损坏示例。',
        source: '/skills/broken/SKILL.md',
        available: false,
        error_message: '技能元数据无效。',
      },
    ],
  });
  expect(screen.getByRole('checkbox', { name: 'broken' })).toBeDisabled();
  expect(screen.getByText('技能元数据无效。')).toBeInTheDocument();
  await user.click(screen.getByRole('checkbox', { name: 'review-code' }));
  await user.type(screen.getByLabelText('任务描述'), '修复代码');
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(/* 表单异步校验后检查公开配置。 */ () => expect(createRun).toHaveBeenCalledOnce());
  expect(vi.mocked(createRun).mock.calls[0][0].config).toEqual({ skills: ['review-code'] });
  await screen.findByText('离线技能提交');
  await user.click(screen.getByRole('checkbox', { name: 'review-code' }));
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(
    /* 取消最后一项必须明确提交空列表。 */ () => expect(createRun).toHaveBeenCalledTimes(2),
  );
  expect(vi.mocked(createRun).mock.calls[1][0].config.skills).toEqual([]);
});

it('首轮默认展示宿主选择，未编辑时继续使用服务端默认', async () => {
  // 缺省覆盖和明确空列表具有不同含义，页面不能在展示时意外清空默认值。
  vi.mocked(createRun).mockReset().mockRejectedValue(new Error('离线默认技能'));
  const user = setup(null, {
    ...options,
    config: { ...options.config, skills: ['host-skill'] },
    skills: [
      {
        name: 'host-skill',
        description: '宿主默认流程。',
        source: '/host/SKILL.md',
        available: true,
      },
    ],
  });
  expect(screen.getByRole('checkbox', { name: 'host-skill' })).toBeChecked();
  await user.type(screen.getByLabelText('任务描述'), '按默认流程执行');
  await user.click(screen.getByRole('button', { name: /开始执行/ }));
  await waitFor(/* 未编辑时让后端加载宿主默认。 */ () => expect(createRun).toHaveBeenCalledOnce());
  expect(vi.mocked(createRun).mock.calls[0][0].config).toEqual({});
});
