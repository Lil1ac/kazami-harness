import { render, screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { ConfigDraftProvider } from '../config';
import { createRun, openProject, pickProject, saveProjectSettings } from '../api';
import { makeRun, options, projectOption } from '../test/fixtures';
import { TaskForm } from './TaskForm';
import type { LocalProjectOption, OptionsResponse } from '../types';

vi.mock(
  '../api',
  /* 所有目录选择和任务请求只模拟HTTP边界。 */ () => ({
    createRun: vi.fn(),
    openProject: vi.fn(),
    pickProject: vi.fn(),
    saveProjectSettings: vi.fn(),
    errorMessage: (error: unknown) => (error instanceof Error ? error.message : '操作失败'),
  }),
);
const folder: LocalProjectOption = {
  id: 'mine',
  label: '我的项目',
  path: 'D:/code/我的项目',
  kind: 'workspace',
  available: true,
}; // 个人目录测试来源。
const gitProject: LocalProjectOption = {
  ...projectOption,
  kind: 'project',
  test_argv: [],
  test_command: '',
  branch: 'main',
}; // 可不配置测试的Git项目。

/** 读取真实内存路由，核对最近项目进入会话列表而非仅改新任务来源。 */
function LocationProbe() {
  const location = useLocation();
  return <span aria-label="当前页面">{location.pathname + location.search}</span>;
}

/** 在真实查询、路由和配置上下文中挂载表单；remember决定是否恢复该项目编号。 */
function setup(extra: Partial<OptionsResponse> = {}, remember = true, busy: string | null = null) {
  const configured: OptionsResponse = { ...options, recent_projects: [folder], ...extra };
  if (remember && configured.recent_projects?.[0])
    localStorage.setItem('harness-selected-project', configured.recent_projects[0].id);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(['options'], configured);
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ConfigDraftProvider>
          <TaskForm options={configured} busyId={busy} />
          <LocationProbe />
        </ConfigDraftProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // 每个控件仍核对可点击性，减少同一复杂AntD表单的重复样式计算；不放宽断言或超时。
  return Object.assign(
    userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.EachTarget }),
    { unmount: view.unmount },
  );
}

beforeEach(
  /* 每例只使用本次公开目录状态，避免前例选择泄漏。 */ () => {
    localStorage.clear();
    sessionStorage.clear();
    vi.resetAllMocks();
    vi.mocked(createRun).mockRejectedValue(new Error('离线阻止模型运行'));
  },
);

it('首次无项目时以打开本机文件夹开始，不默认选择日志样例', async () => {
  // 实际打开接口返回后采用真实目录，打开本身不提交任务。
  vi.mocked(pickProject).mockResolvedValue({ cancelled: false, project: folder });
  const user = setup({ recent_projects: [] }, false);
  expect(screen.getByRole('button', { name: '开始执行' })).toBeDisabled();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('分析我的文件');
  await user.click(screen.getByRole('button', { name: '选择项目文件夹' }));
  expect(await screen.findByRole('button', { name: '我的项目' })).toBeInTheDocument();
  expect(createRun).not.toHaveBeenCalled();
  expect(screen.getByLabelText('任务描述')).toHaveValue('分析我的文件');
  expect(localStorage.getItem('harness-selected-project')).toBe('mine');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(
    /* 校验实际任务请求只引用登记ID。 */ () => expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createRun).mock.calls[0][0]).toMatchObject({
    workspace_id: 'mine',
    execution_mode: 'local',
    prompt: '分析我的文件',
    config: {},
  });
  expect(vi.mocked(createRun).mock.calls[0][0]).not.toHaveProperty('path');
});

it('取消原生窗口保留当前项目和任务草稿', async () => {
  // 不把取消误判成清空目录或开始执行。
  vi.mocked(pickProject).mockResolvedValue({ cancelled: true, project: null });
  const user = setup();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('保留草稿');
  await user.click(screen.getByRole('button', { name: '我的项目' }));
  await waitFor(/* 等待取消响应完成。 */ () => expect(pickProject).toHaveBeenCalledOnce());
  expect(screen.getByRole('button', { name: '我的项目' })).toBeInTheDocument();
  expect(screen.getByLabelText('任务描述')).toHaveValue('保留草稿');
  expect(createRun).not.toHaveBeenCalled();
});

it('最近项目重新核验后进入项目会话页，不改变新建来源或自动提交', async () => {
  // 最近目录点击明确调用open，与模型运行分离。
  vi.mocked(openProject).mockResolvedValue(gitProject);
  const user = setup({ recent_projects: [gitProject] }, false);
  await user.click(screen.getByRole('button', { name: /示例后端.*Git/ }));
  expect(openProject).toHaveBeenCalledWith(projectOption.id, expect.anything());
  await waitFor(
    /* 核对真实路由改变且没有改写新建来源。 */ () =>
      expect(screen.getByLabelText('当前页面')).toHaveTextContent('/projects/' + projectOption.id),
  );
  expect(localStorage.getItem('harness-selected-project')).toBeNull();
  expect(createRun).not.toHaveBeenCalled();
});

it('空要求不提交；发送失败后仍保留任务与高级产物字段', async () => {
  // 防止目录入口改造丢失既有提交校验与草稿。
  const user = setup();
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  expect(await screen.findByText('请描述要完成的任务')).toBeInTheDocument();
  expect(createRun).not.toHaveBeenCalled();
  await user.click(screen.getByText(/高级设置/));
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('清洗CSV');
  await user.click(screen.getByLabelText('预期产物'));
  await user.paste('clean.csv');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  expect(await screen.findByText('离线阻止模型运行')).toBeInTheDocument();
  expect(screen.getByLabelText('任务描述')).toHaveValue('清洗CSV');
  expect(screen.getByLabelText('预期产物')).toHaveValue('clean.csv');
  expect(vi.mocked(createRun).mock.calls[0][0]).toMatchObject({ expected_outputs: ['clean.csv'] });
});

it('隔离Git保存测试设置仍保留执行方式，保存本身不创建任务', async () => {
  // 项目设置是独立用户动作，不能成为任务请求中的任意命令注入。
  vi.mocked(saveProjectSettings).mockResolvedValue({
    ...gitProject,
    test_command: 'python -m pytest -q',
    test_argv: ['python', '-m', 'pytest', '-q'],
  });
  const user = setup({ recent_projects: [gitProject] });
  await user.click(screen.getByText(/高级设置/));
  await user.click(screen.getByLabelText('执行方式'));
  await user.click(screen.getByRole('option', { name: '隔离执行' }));
  await user.click(screen.getByLabelText('项目测试命令'));
  await user.paste('python -m pytest -q');
  await user.click(screen.getByRole('button', { name: '保存项目设置' }));
  expect(await screen.findByRole('status')).toHaveTextContent('项目设置已保存');
  expect(saveProjectSettings).toHaveBeenCalledWith(gitProject.id, 'python -m pytest -q', 60);
  expect(createRun).not.toHaveBeenCalled();
  expect(screen.getByLabelText('提交基线')).toHaveValue('HEAD');
  expect(screen.getByText(/在独立 Git 工作树中执行/)).toBeInTheDocument();
});

it('隔离Git提交使用用户指定基线，执行方式与来源一并发送', async () => {
  // 基线提交与保存设置各有独立副作用，分别验证，避免一个用例串行过多操作而超时。
  const user = setup({ recent_projects: [gitProject] });
  await user.click(screen.getByText(/高级设置/));
  await user.click(screen.getByLabelText('执行方式'));
  await user.click(screen.getByRole('option', { name: '隔离执行' }));
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('修复代码');
  await user.click(screen.getByLabelText('提交基线'));
  await user.keyboard('{Control>}a{/Control}');
  await user.paste('main');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(/* 请求仍为受限任务协议。 */ () => expect(createRun).toHaveBeenCalledOnce());
  expect(vi.mocked(createRun).mock.calls[0][0]).toEqual({
    prompt: '修复代码',
    project_id: gitProject.id,
    execution_mode: 'isolated',
    base_ref: 'main',
    expected_outputs: [],
    config: {},
  });
});

it('技能收在高级设置，明确选择与清空仍随实际任务请求提交', async () => {
  // 隐藏高级选项不改变技能预算和选择语义。
  const user = setup({
    skills: [
      {
        name: 'review',
        description: '审阅流程',
        source: '/skills/review/SKILL.md',
        available: true,
      },
    ],
  });
  expect(screen.getByText(/高级设置/).closest('details')).not.toHaveAttribute('open');
  await user.click(screen.getByText(/高级设置/));
  await user.click(screen.getByRole('checkbox', { name: 'review' }));
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('检查实现');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(/* 首次选择进入请求。 */ () => expect(createRun).toHaveBeenCalledOnce());
  expect(vi.mocked(createRun).mock.calls[0][0].config.skills).toEqual(['review']);
  await screen.findByText('离线阻止模型运行');
  await user.click(screen.getByRole('checkbox', { name: 'review' }));
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(/* 清空提交明确的空数组。 */ () => expect(createRun).toHaveBeenCalledTimes(2));
  expect(vi.mocked(createRun).mock.calls[1][0].config.skills).toEqual([]);
});

it('样例只在辅助入口出现，填充后仍等待用户发送', async () => {
  // 示例用于试用，不再占用初始项目选择。
  const user = setup({ recent_projects: [] }, false);
  expect(screen.getByText('试用示例').closest('details')).not.toHaveAttribute('open');
  await user.click(screen.getByText('试用示例'));
  await user.click(screen.getByRole('button', { name: '填充日志样例' }));
  expect((screen.getByLabelText('任务描述') as HTMLTextAreaElement).value).toContain('app.log');
  expect(createRun).not.toHaveBeenCalled();
});

it('项目不可用、任务忙碌和缺少凭据时禁止发送，仍可编辑要求', async () => {
  // 页面不把不可用的项目状态当成已经启动。
  const user = setup(
    { recent_projects: [{ ...gitProject, available: false, error: '仓库没有提交' }] },
    true,
    'busy',
  );
  expect(screen.getByRole('button', { name: '开始执行' })).toBeDisabled();
  expect(screen.getByText('仓库没有提交')).toBeInTheDocument();
  expect(screen.getByText('不可用')).toBeInTheDocument();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('稍后执行');
  expect(screen.getByLabelText('任务描述')).toHaveValue('稍后执行');
});

it('配置页往返或刷新重挂载后保留文字，成功接纳前不清空草稿', async () => {
  // 模拟真实路由卸载，确认草稿不随表单实例丢失。
  const user = setup();
  await user.click(screen.getByText(/高级设置/));
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('继续编辑');
  await user.click(screen.getByLabelText('预期产物'));
  await user.paste('result.csv');
  const stored = JSON.parse(sessionStorage.getItem('harness-new-task-draft') || '{}');
  expect(stored).toMatchObject({ prompt: '继续编辑', outputs: 'result.csv' });
  user.unmount();
  setup();
  expect(screen.getByLabelText('任务描述')).toHaveValue('继续编辑');
  expect(screen.getByLabelText('预期产物')).toHaveValue('result.csv');
});

it('真实接纳任务后清除新任务草稿，下一次打开不会重复填写已发送内容', async () => {
  // 只有服务端成功响应触发清理；失败保留语义由前例覆盖。
  vi.mocked(createRun).mockResolvedValue(makeRun());
  const user = setup();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('发送后清理');
  expect(sessionStorage.getItem('harness-new-task-draft')).toContain('发送后清理');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(
    /* 接纳后草稿已清理。 */ () =>
      expect(sessionStorage.getItem('harness-new-task-draft')).toBeNull(),
  );
});

it('本机Git项目默认原目录执行，不允许隐藏的旧基线切换到其他代码', async () => {
  // 本机项目发送明确local，用户当前未提交文件应参与执行。
  const user = setup({ recent_projects: [gitProject] });
  expect(screen.getByText('直接修改上方所选目录，使用当前文件与未提交改动。')).toBeInTheDocument();
  await user.click(screen.getByText(/高级设置/));
  expect(screen.queryByLabelText('提交基线')).not.toBeInTheDocument();
  await user.click(screen.getByLabelText('任务描述'));
  await user.paste('修改我选择的目录');
  await user.click(screen.getByRole('button', { name: '开始执行' }));
  await waitFor(
    /* 核对真实请求含明确的目录执行方式。 */ () => expect(createRun).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createRun).mock.calls[0][0]).toMatchObject({
    project_id: gitProject.id,
    execution_mode: 'local',
    base_ref: 'HEAD',
  });
});
