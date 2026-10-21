import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getDiff, getProjectReview, reviewRun } from '../api';
import { makeProject, makeRun } from '../test/fixtures';
import type { ProjectReview, RunView } from '../types';
import { ProjectReviewPanel } from './ProjectReviewPanel';

vi.mock(
  '../api',
  /* 本地替身只模拟 HTTP 响应，不运行 Git、子进程或模型。 */ () => ({
    getDiff: vi.fn(),
    getProjectReview: vi.fn(),
    reviewRun: vi.fn(),
    patchUrl: (id: string) => `/api/runs/${id}/patch`, // 模拟稳定附件 URL。
    errorMessage: (error: unknown) => (error instanceof Error ? error.message : '请求失败'), // 安全错误说明。
  }),
);

/** 通过真实 Query 缓存读取审阅快照，验证成功响应更新与失败保留，active 表示后台占用状态。 */
function setup(project: ProjectReview = makeProject(), active = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(['run', project.run_id], makeRun({ project, active }));
  /** 订阅任务缓存；无额外 RunView 读取，由恢复或审阅响应更新当前项目事实。 */
  function ReviewHost() {
    const run = useQuery<RunView>({ queryKey: ['run', project.run_id], enabled: false }).data!;
    return <ProjectReviewPanel project={run.project!} active={run.active} />;
  }
  render(
    <QueryClientProvider client={client}>
      <ReviewHost />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(
  /* 每个场景清除决定与预览，并提供最小普通文本补丁。 */ () => {
    vi.mocked(reviewRun).mockReset();
    vi.mocked(getProjectReview)
      .mockReset()
      .mockResolvedValue(makeProject({ snapshot_status: 'pending' }));
    vi.mocked(getDiff).mockReset().mockResolvedValue({
      content: 'diff --git a/app.py b/app.py\n+fixed',
      truncated: false,
      size_bytes: 42,
    });
  },
);

describe('代码审阅', /* 代码事实、测试和人工决定相互独立；不会预先假定操作成功。 */ () => {
  it('列出增改删和二进制文件，补丁中的标签仅按纯文本渲染', async () => {
    // 模型可以写入任何文本；浏览器不能把补丁变成 HTML。
    vi.mocked(getDiff).mockResolvedValue({
      content: '+<script>window.hacked = true</script>',
      truncated: true,
      size_bytes: 90000,
    });
    setup(
      makeProject({
        files: [
          { path: 'new.py', status: 'added', binary: false },
          { path: 'old.py', status: 'deleted', binary: false },
          { path: 'image.png', status: 'modified', binary: true },
        ],
      }),
    );
    const preview = await screen.findByLabelText('代码补丁预览');
    expect(preview).toHaveTextContent('+<script>window.hacked = true</script>');
    expect(preview.querySelector('script')).toBeNull();
    expect(screen.getByText('new.py')).toBeInTheDocument();
    expect(screen.getByText('删除')).toBeInTheDocument();
    expect(screen.getByText('二进制文件')).toBeInTheDocument();
    expect(screen.getByText(/补丁预览已截断/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /下载完整补丁/ })).toHaveAttribute(
      'href',
      '/api/runs/run-a/patch',
    );
  });
  it.each([
    { active: true, snapshot_status: 'ready' as const },
    { active: false, snapshot_status: 'pending' as const },
    { active: false, snapshot_status: 'error' as const },
  ])('运行或证据尚未就绪时禁止做决定：%s', ({ active, snapshot_status }) => {
    // 即使按钮被触发，后端还会独立检查；前端先明确呈现原因。
    setup(makeProject({ snapshot_status }), active);
    expect(screen.getByRole('button', { name: '保留工作树' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '丢弃工作树' })).toBeDisabled();
    expect(reviewRun).not.toHaveBeenCalled();
  });
  it('确认前不丢弃，取消确认后工作树状态仍等待审阅', async () => {
    // 打开危险操作确认本身不触发 mutation。
    const user = setup();
    await user.click(screen.getByRole('button', { name: '丢弃工作树' }));
    expect(reviewRun).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: '继续审阅' }));
    expect(screen.getByText('等待审阅')).toBeInTheDocument();
    expect(reviewRun).not.toHaveBeenCalled();
  });
  it('丢弃失败显示错误并保留已有补丁和等待审阅状态', async () => {
    // 网络或归属校验失败后不能伪称工作树已移除。
    vi.mocked(reviewRun).mockRejectedValue(new Error('工作树归属校验未通过'));
    const user = setup();
    await user.click(screen.getByRole('button', { name: '丢弃工作树' }));
    await user.click(await screen.findByRole('button', { name: '确认丢弃' }));
    expect(await screen.findByText('工作树归属校验未通过')).toBeInTheDocument();
    expect(screen.getByText('等待审阅')).toBeInTheDocument();
    expect(screen.getByLabelText('代码补丁预览')).toBeInTheDocument();
    expect(reviewRun).toHaveBeenCalledWith('run-a', 'discard');
  });
  it('服务端确认丢弃后显示证据保留且不能再次决定', async () => {
    // mutation 完成后才从缓存中的新审阅状态渲染。
    vi.mocked(reviewRun).mockResolvedValue(makeProject({ status: 'discarded' }));
    const user = setup();
    await user.click(screen.getByRole('button', { name: '丢弃工作树' }));
    await user.click(await screen.findByRole('button', { name: '确认丢弃' }));
    expect(await screen.findByText('本任务工作树已丢弃。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '丢弃工作树' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /下载完整补丁/ })).toBeInTheDocument();
  });
  it('保留只保存独立工作树，不提示已提交或已合并', async () => {
    // 此状态与测试是否通过无关，用户明确看到保留范围。
    vi.mocked(reviewRun).mockResolvedValue(makeProject({ status: 'kept' }));
    const user = setup();
    await user.click(screen.getByRole('button', { name: '保留工作树' }));
    await waitFor(
      /* 等待服务端返回并写入缓存。 */ () =>
        expect(screen.getByRole('button', { name: '已保留工作树' })).toBeDisabled(),
    );
    expect(screen.getByText(/不会自动提交或合并/)).toBeInTheDocument();
    expect(reviewRun).toHaveBeenCalledWith('run-a', 'keep');
  });
  it('空改动明确展示，不依赖补丁内容猜测文件数量', async () => {
    // 完成任务也可以没有修改，仍可保留或丢弃工作树。
    vi.mocked(getDiff).mockResolvedValue({ content: '', truncated: false, size_bytes: 0 });
    setup(makeProject({ files: [] }));
    expect(await screen.findByText('相对基线没有代码改动')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保留工作树' })).toBeEnabled();
  });
  it('已丢弃工作树的运行仍能读取保存的差异', async () => {
    // 历史视图完全依赖保存证据，不能因工作树不存在就丢失补丁。
    setup(makeProject({ status: 'discarded' }));
    const section = screen.getByRole('region', { name: '代码改动' });
    expect(within(section).getByText('已丢弃工作树')).toBeInTheDocument();
    expect(await screen.findByLabelText('代码补丁预览')).toBeInTheDocument();
  });
});

it('服务重启遗留的待捕获代码恢复后可继续审阅', async () => {
  // 恢复接口仅补捕获证据；不发新模型任务，不重新执行测试。
  vi.mocked(getProjectReview).mockResolvedValue(makeProject());
  setup(makeProject({ snapshot_status: 'pending', files: [], patch_available: false }));
  await waitFor(
    /* 等待宿主返回的已固化快照写入详情缓存。 */ () =>
      expect(screen.getByRole('button', { name: '保留工作树' })).toBeEnabled(),
  );
  expect(getProjectReview).toHaveBeenCalledWith('run-a', expect.any(AbortSignal));
  expect(await screen.findByLabelText('代码补丁预览')).toBeInTheDocument();
  expect(reviewRun).not.toHaveBeenCalled();
});
