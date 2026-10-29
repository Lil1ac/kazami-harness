import { render, screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
import { beforeEach, expect, it, vi } from 'vitest';
import { getOptions, saveProjectSettings, createRun } from '../api';
import { ConfigDraftProvider } from '../config';
import { options, projectOption } from '../test/fixtures';
import type { LocalProjectOption } from '../types';
import { ProjectPage } from './ProjectPage';
import { RunsPage } from './RunsPage';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  getOptions: vi.fn(),
  saveProjectSettings: vi.fn(),
  createRun: vi.fn(),
  getConversations: vi.fn(
    /* 导航与配置均不创建执行记录。 */ async () => ({ items: [], total: 0, busy_run_id: null }),
  ),
}));

/** 挂载真实的新建、项目页和共享缓存；project 为本次来源，path 默认直接打开项目设置，返回交互驱动器。 */
function setup(
  project: LocalProjectOption = projectOption,
  path = '/projects/' + project.id + '?settings=1',
) {
  const configured = { ...options, recent_projects: [project] };
  vi.mocked(getOptions).mockResolvedValue(configured);
  localStorage.setItem('harness-selected-project', project.id);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(['options'], configured);
  render(
    <QueryClientProvider client={client}>
      <ConfigDraftProvider>
        <MemoryRouter initialEntries={[path]}>
          <Link to="/runs">返回新建</Link>
          <Routes>
            <Route path="/projects/:projectId" element={<ProjectPage />} />
            <Route path="/runs" element={<RunsPage />} />
          </Routes>
        </MemoryRouter>
      </ConfigDraftProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.EachTarget });
}

beforeEach(
  /* 各用例独立保存草稿和模型调用计数。 */ () => {
    vi.resetAllMocks();
    sessionStorage.clear();
    localStorage.clear();
  },
);

it('从输入区进入项目设置再返回，保留任务草稿且不创建任务', async () => {
  // 路由往返覆盖新入口，配置保存的字段验证由下一例负责。
  const user = setup(projectOption, '/runs');
  await user.click(await screen.findByLabelText('任务描述'));
  await user.paste('保留这条任务草稿');
  await user.click(screen.getByRole('link', { name: '项目设置' }));
  expect(await screen.findByLabelText('Python 解释器')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Close' }));
  await user.click(screen.getByRole('link', { name: '返回新建' }));
  expect(await screen.findByLabelText('任务描述')).toHaveValue('保留这条任务草稿');
  expect(saveProjectSettings).not.toHaveBeenCalled();
  expect(createRun).not.toHaveBeenCalled();
});

it('保存 Git 项目测试和解释器，只更新后续运行配置', async () => {
  // 直接挂载项目设置，避免把路由往返和字段保存串在同一超时窗口。
  vi.mocked(saveProjectSettings).mockResolvedValue({
    ...projectOption,
    python_executable: 'D:/env/python.exe',
    test_command: 'python -m pytest -q',
  });
  const user = setup();
  await user.click(await screen.findByLabelText('Python 解释器'));
  await user.paste('D:/env/python.exe');
  await user.click(screen.getByLabelText('项目测试命令'));
  await user.paste('python -m pytest -q');
  await user.click(screen.getByRole('button', { name: '保存项目设置' }));
  expect(await screen.findByRole('status')).toHaveTextContent('项目设置已保存');
  expect(saveProjectSettings).toHaveBeenCalledWith(
    projectOption.id,
    'python -m pytest -q',
    60,
    'D:/env/python.exe',
  );
  expect(createRun).not.toHaveBeenCalled();
});

it('普通目录只保存解释器，保存失败保留输入且允许重试', async () => {
  // 普通目录不能借表单提交 Git 测试命令；后端失败不能伪造保存成功。
  const folder: LocalProjectOption = {
    id: 'plain',
    kind: 'workspace',
    label: '普通目录',
    path: 'D:/plain',
  };
  vi.mocked(saveProjectSettings)
    .mockRejectedValueOnce(new Error('目录暂不可访问'))
    .mockResolvedValueOnce({ ...folder, python_executable: 'D:/env/python.exe' });
  const user = setup(folder);
  expect(screen.queryByLabelText('项目测试命令')).not.toBeInTheDocument();
  await user.click(await screen.findByLabelText('Python 解释器'));
  await user.paste('D:/env/python.exe');
  await user.click(screen.getByRole('button', { name: '保存项目设置' }));
  expect(await screen.findByText('目录暂不可访问')).toBeInTheDocument();
  expect(screen.getByLabelText('Python 解释器')).toHaveValue('D:/env/python.exe');
  await user.click(screen.getByRole('button', { name: '保存项目设置' }));
  await waitFor(
    /* 重试完成后展示真实保存确认。 */ () =>
      expect(screen.getByRole('status')).toHaveTextContent('项目设置已保存'),
  );
  expect(saveProjectSettings).toHaveBeenLastCalledWith('plain', '', 30, 'D:/env/python.exe');
  expect(createRun).not.toHaveBeenCalled();
});
