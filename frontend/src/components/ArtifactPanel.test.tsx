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
        missing={[]}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByRole('button', { name: 'app.py' })).toBeEnabled();
  expect(screen.getByRole('link', { name: '下载 app.py' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '下载 verification.stdout.txt' })).toBeInTheDocument();
});
