import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it } from 'vitest';
import { ArtifactPanel } from './ArtifactPanel';

it('丢弃项目工作树后禁用工作区文件，保留运行证据下载', () => {
  // 避免页面留下会访问已移除目录的入口，宿主也会再次检查该状态。
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <ArtifactPanel
        runId="run-a"
        workspaceUnavailable
        artifacts={[
          { path: 'app.py', location: 'workspace', kind: 'file' },
          { path: 'verification.stdout.txt', location: 'run', kind: 'stdout' },
        ]}
        missing={[]}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByRole('button', { name: 'app.py' })).toBeDisabled();
  expect(screen.queryByRole('link', { name: '下载 app.py' })).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: '下载 verification.stdout.txt' })).toBeInTheDocument();
});
