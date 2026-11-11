import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { changeSkill, importSkill, previewSkill, readSkillFolder } from '../skillApi';
import type { SkillOption } from '../types';
import { SkillSettings } from './SkillSettings';

vi.mock(
  '../skillApi',
  /* 管理交互测试在同源接口边界捕获固定请求。 */ () => ({
    changeSkill: vi.fn(),
    importSkill: vi.fn(),
    previewSkill: vi.fn(),
    previewSkillResource: vi.fn(),
    readSkillFolder: vi.fn(),
  }),
);

const skill: SkillOption = {
  id: 'managed-code-delivery',
  name: 'code-delivery',
  description: '代码交付流程',
  source: '受管技能/code-delivery/SKILL.md',
  available: false,
  enabled: false,
  managed: true,
  revision: 'shown-revision',
  error_code: 'skill_disabled',
}; // 测试中用户实际看到的已停用副本。

/** 挂载带查询缓存的管理界面，返回渲染工具。 */
function setup() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={cache}>
      <SkillSettings skills={[skill]} />
    </QueryClientProvider>,
  );
}

it('从管理页预览技能并按显示的版本启用', async () => {
  vi.mocked(previewSkill).mockResolvedValue({
    name: skill.name,
    description: skill.description,
    source: skill.source,
    content: '冻结正文',
    sha256: 'hash',
    size_bytes: 12,
  });
  vi.mocked(changeSkill).mockResolvedValue({ ok: true });
  setup();
  fireEvent.click(screen.getByRole('button', { name: skill.name }));
  const dialog = await screen.findByRole('dialog');
  expect(await within(dialog).findByText('冻结正文')).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: /启\s*用/ }));
  await waitFor(
    /* 确认异步变更携带页面展示的旧版本。 */ () =>
      expect(changeSkill).toHaveBeenCalledWith(skill, 'enable'),
  );
});

it('重新导入先展示预览，确认后提交已见的同名副本版本', async () => {
  const draft = {
    name: skill.name,
    files: [{ path: 'SKILL.md', content: '新的正文' }],
    ignored: 0,
  };
  vi.mocked(readSkillFolder).mockResolvedValue(draft);
  vi.mocked(importSkill).mockResolvedValue(skill);
  const { container } = setup();
  fireEvent.change(container.querySelector('input[type=file]')!, {
    target: { files: [new File(['text'], 'SKILL.md')] },
  });
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText('新的正文')).toBeInTheDocument();
  expect(importSkill).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: '替换副本' }));
  await waitFor(
    /* 等待真实 mutation 回调接纳替换请求。 */ () =>
      expect(importSkill).toHaveBeenCalledWith(draft, skill.revision),
  );
});
