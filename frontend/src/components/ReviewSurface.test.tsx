import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { readableCommand, ReviewDiff, ReviewPatch } from './ReviewSurface';

it('命令展示保留空参数、引号和控制字符的边界', () => {
  expect(readableCommand(['python', '-c', 'print("hello world")', '', 'a\nb'])).toBe(
    'python -c "print(\\"hello world\\")" "" "a\\nb"',
  );
});

it('文件对比保留共同内容、两侧行号和删除新增文本，代码只作为文本展示', () => {
  render(<ReviewDiff before={'start\nold\nend\n'} after={'start\n<script>new</script>\nend\n'} />);
  expect(screen.getByText('old').closest('.review-diff-line')).toHaveClass('review-diff-removed');
  expect(screen.getByText('<script>new</script>').closest('.review-diff-line')).toHaveClass(
    'review-diff-added',
  );
  expect(screen.getByLabelText('原第2行')).toHaveTextContent('2');
  expect(screen.getByLabelText('新第2行')).toHaveTextContent('2');
  expect(document.querySelector('script')).toBeNull();
});

it('超长文件回退完整原文，不能把未展示部分当作已审阅或截断丢弃', () => {
  const before = '旧行\n'.repeat(1300) + '旧末行';
  const after = '新行\n'.repeat(1300) + '新末行';
  render(<ReviewDiff before={before} after={after} />);
  expect(screen.getByText(/旧末行/).textContent).toBe(before);
  expect(screen.getByText(/新末行/).textContent).toBe(after);
});

it('宿主补丁中的行号与元数据保持可读', () => {
  render(
    <ReviewPatch
      label="固定差异"
      content={'--- a/app.py\n+++ b/app.py\n@@ -5 +5 @@\n-old\n+new\n'}
    />,
  );
  expect(screen.getByLabelText('固定差异')).toHaveTextContent('@@ -5 +5 @@');
  expect(screen.getByText('-old').closest('.review-diff-line')).toHaveTextContent('5');
  expect(screen.getByText('+new').closest('.review-diff-line')).toHaveClass('review-diff-added');
});
