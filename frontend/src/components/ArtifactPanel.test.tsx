import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it } from 'vitest';
import { ArtifactPanel } from './ArtifactPanel';

it('工作区历史文件与运行证据均保留快照下载入口', () => {
  // 下载由宿主读取该轮固定快照，不依赖共享工作树仍存在。
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <ArtifactPanel
        runId="run-a"
        artifacts={[
          { path: 'app.py', location: 'workspace', kind: 'file' },
          { path: 'verification.stdout.txt', location: 'run', kind: 'stdout' },
        ]}
        status="complete"
        warnings={[]}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByRole('button', { name: 'app.py' })).toBeEnabled();
  expect(screen.getByRole('link', { name: '下载 app.py' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '下载 verification.stdout.txt' })).toBeInTheDocument();
});

it('收集受限明确提示，同时保留已保存的运行输出下载', () => {
  // 文件采集状态独立于执行和测试，不能隐藏已有输出或显示成产物验收失败。
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ArtifactPanel
        runId="run-incomplete"
        artifacts={[{ path: 'stdout.bin', location: 'run', kind: 'stdout' }]}
        status="incomplete"
        warnings={['目录超过采集范围，文件未完整保存。']}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByText('文件收集不完整')).toBeInTheDocument();
  expect(screen.getByText('目录超过采集范围，文件未完整保存。')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '下载 stdout.bin' })).toBeInTheDocument();
});

it('收集不完整且没有索引时，不把未知结果表述成没有文件变化', () => {
  // 快照失败只能说明缺少可信索引，不能据此断言本轮没有产物。
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ArtifactPanel runId="run-empty" artifacts={[]} status="incomplete" warnings={[]} />
    </QueryClientProvider>,
  );
  expect(screen.getByText('暂无可核对的文件产物')).toBeInTheDocument();
  expect(screen.queryByText('本轮没有新增或修改的文件及输出')).not.toBeInTheDocument();
});
