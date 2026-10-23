import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { SkillSelector } from './SkillSelector';

it('失效的已有选择可以取消，失效新项不能新增，八项上限可读', async () => {
  // 目录移除已选文件时不困住用户，取消后能继续使用无技能模式。
  const changed = vi.fn();
  const user = userEvent.setup();
  const { rerender } = render(
    <SkillSelector options={[]} selected={['gone']} onChange={changed} />,
  );
  expect(screen.getByRole('checkbox', { name: 'gone' })).toBeEnabled();
  expect(screen.getByText(/当前目录未找到此技能/)).toBeInTheDocument();
  await user.click(screen.getByRole('checkbox', { name: 'gone' }));
  expect(changed).toHaveBeenCalledWith([]);
  const names = Array.from(
    { length: 8 },
    /* 构造有效八项名称，不执行任何技能。 */ (_, index) => 'skill-' + index,
  );
  rerender(
    <SkillSelector
      options={[{ name: 'ninth', description: '额外流程', source: '/ninth', available: true }]}
      selected={names}
      onChange={changed}
    />,
  );
  expect(screen.getByRole('checkbox', { name: 'ninth' })).toBeDisabled();
  expect(screen.getByText('最多选择 8 个技能，可取消后再调整。')).toBeInTheDocument();
});

it('重复名称和目录级错误都有独立可读项，目录错误不能选中', () => {
  // 同名冲突不能由 React 重复 key 隐藏，缺少技能名的根错误也要展示来源和原因。
  render(
    <SkillSelector
      selected={[]}
      onChange={vi.fn()}
      options={[
        {
          name: 'duplicate',
          description: '冲突项一',
          source: '/root-a/duplicate/SKILL.md',
          available: false,
          error_message: '技能名称重复。',
        },
        {
          name: 'duplicate',
          description: '冲突项二',
          source: '/root-b/duplicate/SKILL.md',
          available: false,
          error_message: '技能名称重复。',
        },
        {
          name: '',
          description: '',
          source: '/missing-root',
          available: false,
          error_message: '技能目录无法读取。',
        },
      ]}
    />,
  );
  expect(screen.getAllByRole('checkbox', { name: 'duplicate' })).toHaveLength(2);
  expect(screen.getByRole('checkbox', { name: '技能目录不可用' })).toBeDisabled();
  expect(screen.getByText('/missing-root')).toBeInTheDocument();
  expect(screen.getByText('技能目录无法读取。')).toBeInTheDocument();
});
