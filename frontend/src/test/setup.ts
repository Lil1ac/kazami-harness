import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// JSDOM 无媒体查询布局，Ant Design 只需要可订阅的标准接口。
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn(
    /* 返回固定桌面匹配结果与无副作用订阅。 */ (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  ),
});

/** 测试专用尺寸观察器，不模拟布局，仅满足组件订阅/解除契约。 */
class TestResizeObserver {
  /** 测试中忽略观察节点，返回 void，不触发异步尺寸变化。 */
  observe(): void {}
  /** 测试中解除观察为空操作。 */
  unobserve(): void {}
  /** 测试中关闭观察为空操作。 */
  disconnect(): void {}
}
vi.stubGlobal('ResizeObserver', TestResizeObserver);
afterEach(/* 每个测试销毁 React 树，验证 effect 能清理订阅。 */ () => cleanup());
