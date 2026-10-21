import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { expect, it, vi } from 'vitest';
import { App } from './App';

vi.mock(
  './pages/RunsPage',
  /* 壳层测试不读取任务或调用模型。 */ () => ({ RunsPage: () => <div>任务页面</div> }),
);
vi.mock(
  './pages/RunDetailPage',
  /* 详情组件由自身测试覆盖，这里只检查导航语义。 */ () => ({
    RunDetailPage: () => <div>详情页面</div>,
  }),
);
vi.mock(
  './pages/SettingsPage',
  /* 以可见文本验证路由跳转，不依赖后端配置。 */ () => ({
    SettingsPage: () => <div>配置页面</div>,
  }),
);

it('窄屏隐藏导航文字后，链接仍保持中文可访问名称并可跳转', async () => {
  // 使用实际导航与路由，仅模拟窄屏 CSS 对文字的隐藏。
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/runs']}>
      <App />
    </MemoryRouter>,
  );
  const navigation = screen.getByRole('navigation', { name: '主导航' });
  navigation.querySelectorAll<HTMLElement>('a > span:not(.anticon)').forEach(
    /* 保留图标，模拟移动布局隐藏文案。 */ (label) => {
      label.style.display = 'none';
    },
  );
  expect(within(navigation).getByRole('link', { name: '任务工作台' })).toBeInTheDocument();
  await user.click(within(navigation).getByRole('link', { name: '运行配置' }));
  expect(await screen.findByText('配置页面')).toBeInTheDocument();
  expect(within(navigation).getByRole('link', { name: '运行配置' })).toHaveAttribute(
    'aria-current',
    'page',
  );
});
