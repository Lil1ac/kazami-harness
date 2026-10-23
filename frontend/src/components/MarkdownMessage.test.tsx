import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { MarkdownMessage } from './MarkdownMessage';

it('显示语义Markdown、GFM表格和代码，用户点击才复制原文', async () => {
  // 代码仅展示与复制，不执行；中文和换行必须保留。
  const user = userEvent.setup();
  const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
  render(
    <MarkdownMessage
      text={
        '## 修改说明\n\n- 保留修复\n\n|文件|状态|\n|---|---|\n|app.py|已修改|\n\n```python\n# 中文说明\nprint(1 + 2)\n```'
      }
    />,
  );
  expect(screen.getByRole('heading', { name: '修改说明' })).toBeInTheDocument();
  expect(screen.getByRole('table')).toHaveTextContent('app.py');
  expect(copy).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '复制代码' }));
  expect(copy).toHaveBeenCalledWith('# 中文说明\nprint(1 + 2)');
  expect(screen.getByText('已复制')).toBeInTheDocument();
});

it('原始HTML、危险链接和外部图片不会成为可执行或自动加载的节点', () => {
  // 保留合法网页链接，过滤javascript/data及模型自行引用的图片资源。
  render(
    <MarkdownMessage
      text={
        '<script>window.hacked=1</script>\n\n[危险](javascript:alert%281%29)\n\n[文档](https://example.com/docs)\n\n![截图](https://example.com/track.png)'
      }
    />,
  );
  expect(document.querySelector('script')).toBeNull();
  expect(document.querySelector('img')).toBeNull();
  expect(screen.queryByRole('link', { name: '危险' })).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('rel', 'noopener noreferrer');
  expect(screen.getByText('[图片：截图]')).toBeInTheDocument();
});
