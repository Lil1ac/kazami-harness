import { StrictMode } from 'react';
import type { PropsWithChildren } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getEvents } from '../api';
import { makeEvent } from '../test/fixtures';
import { useRunEvents } from './useRunEvents';
import type { EventsResponse } from '../types';

vi.mock(
  '../api',
  /* 事件读取全部离线，保留受控错误转换。 */ () => ({
    getEvents: vi.fn(),
    errorMessage: (error: unknown) => (error instanceof Error ? error.message : '读取失败'),
  }),
);

/** 可显式推送事件的 EventSource 替身，记录生命周期而不建立网络。 */
class FakeSource {
  static instances: FakeSource[] = []; // 本测试内创建过的连接。
  url: string; // 包含已补齐游标的订阅地址。
  closed = false; // close 调用后为 true。
  onopen: (() => void) | null = null; // 模拟连接打开通知。
  onerror: (() => void) | null = null; // 模拟网络断线通知。
  listeners = new Map<string, EventListener[]>(); // 按事件名称保存订阅回调。
  /** 记录订阅地址并加入实例表；不执行任何真实网络操作。 */
  constructor(url: string) {
    this.url = url;
    FakeSource.instances.push(this);
  }
  /** 保存事件回调；type 是 SSE 事件名，listener 接收单条消息。 */
  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  /** 模拟浏览器关闭订阅；已运行的后台任务不受影响。 */
  close(): void {
    this.closed = true;
  }
  /** 向该连接推送 JSON 消息，返回 void；允许测试关闭后的迟到消息。 */
  emit(type: string, data: unknown): void {
    for (const listener of this.listeners.get(type) ?? [])
      listener(new MessageEvent(type, { data: JSON.stringify(data) }));
  }
}

/** 为 hook 启用 StrictMode，从而真实触发首次 effect 的设置/清理/重建。 */
function StrictWrapper({ children }: PropsWithChildren) {
  return <StrictMode>{children}</StrictMode>;
}

beforeEach(
  /* 隔离连接表和请求返回值，禁止测试触发网络。 */ () => {
    FakeSource.instances = [];
    vi.stubGlobal('EventSource', FakeSource);
    vi.mocked(getEvents).mockReset();
  },
);
afterEach(
  /* 恢复计时器与全局构造器，避免跨用例污染。 */ () => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  },
);

describe('单任务 SSE 生命周期', /* 覆盖补齐、重连、终态与卸载，不只镜像状态更新。 */ () => {
  it('先读完历史分页，再以连续游标订阅，重复事件不重复展示', async () => /* 验证 HTTP 与实时事件交界的去重。 */ {
    vi.mocked(getEvents)
      .mockResolvedValueOnce({ items: [makeEvent(1)], has_more: true, last_seq: 2 })
      .mockResolvedValueOnce({ items: [makeEvent(2)], has_more: false, last_seq: 2 });
    const { result, unmount } = renderHook(
      /* 开始活动任务订阅。 */ () => useRunEvents('run-a', true, vi.fn()),
    );
    await waitFor(
      /* 历史补齐之后才建立连接。 */ () => expect(FakeSource.instances).toHaveLength(1),
    );
    const source = FakeSource.instances[0];
    expect(source.url).toContain('after=2');
    act(
      /* 推送重复与下一条合法事件。 */ () => {
        source.onopen?.();
        source.emit('run_event', makeEvent(2));
        source.emit('run_event', makeEvent(3));
      },
    );
    expect(result.current.events.map(/* 获取显示序号。 */ (event) => event.seq)).toEqual([1, 2, 3]);
    expect(result.current.connection).toBe('live');
    unmount();
    expect(source.closed).toBe(true);
  });
  it('发现缺口时关闭连接、补齐历史并带新游标重连', async () => /* 不允许直接跳过丢失的工具事件。 */ {
    vi.mocked(getEvents)
      .mockResolvedValueOnce({ items: [makeEvent(1)], has_more: false, last_seq: 1 })
      .mockResolvedValueOnce({ items: [makeEvent(2), makeEvent(3)], has_more: false, last_seq: 3 });
    const { result } = renderHook(/* 创建活动订阅。 */ () => useRunEvents('run-a', true, vi.fn()));
    await waitFor(/* 等待初始连接。 */ () => expect(FakeSource.instances).toHaveLength(1));
    const first = FakeSource.instances[0];
    act(/* 用跳号消息模拟丢失网络事件。 */ () => first.emit('run_event', makeEvent(3)));
    expect(first.closed).toBe(true);
    expect(result.current.connection).toBe('reconnecting');
    await waitFor(
      /* 延迟后读取历史并创建新连接。 */ () => expect(FakeSource.instances).toHaveLength(2),
      { timeout: 2500 },
    );
    expect(vi.mocked(getEvents).mock.calls[1][1]).toBe(1);
    expect(FakeSource.instances[1].url).toContain('after=3');
    expect(result.current.events.map(/* 连续事件应全部可见。 */ (event) => event.seq)).toEqual([
      1, 2, 3,
    ]);
  });
  it('终态关闭连接、补最后事件并刷新快照', async () => /* 不根据 SSE 连接关闭自行判定成功。 */ {
    vi.mocked(getEvents)
      .mockResolvedValueOnce({ items: [], has_more: false, last_seq: 0 })
      .mockResolvedValueOnce({ items: [makeEvent(1)], has_more: false, last_seq: 1 });
    const settled = vi.fn();
    const { result } = renderHook(
      /* 注入可观察的终态刷新回调。 */ () => useRunEvents('run-a', true, settled),
    );
    await waitFor(/* 等待实时连接建立。 */ () => expect(FakeSource.instances).toHaveLength(1));
    act(
      /* 模拟服务端终态通知。 */ () =>
        FakeSource.instances[0].emit('stream_end', { last_seq: 1, interrupted: false }),
    );
    await waitFor(/* 最后一页应提交后再刷新详情。 */ () => expect(settled).toHaveBeenCalledOnce());
    expect(FakeSource.instances[0].closed).toBe(true);
    expect(result.current.connection).toBe('closed');
    expect(result.current.events).toHaveLength(1);
  });
  it('StrictMode 只留下一个活动连接，卸载关闭全部连接', async () => /* 验证开发模式清理而非关闭 StrictMode。 */ {
    vi.mocked(getEvents).mockResolvedValue({ items: [], has_more: false, last_seq: 0 });
    const { unmount } = renderHook(
      /* StrictMode 可反复运行 effect，旧异步结果应丢弃。 */ () =>
        useRunEvents('run-a', true, vi.fn()),
      { wrapper: StrictWrapper },
    );
    await waitFor(
      /* 最终只能保留一个有效订阅。 */ () =>
        expect(
          FakeSource.instances.filter(/* 统计存活订阅。 */ (source) => !source.closed),
        ).toHaveLength(1),
    );
    unmount();
    expect(FakeSource.instances.every(/* 卸载后全部关闭。 */ (source) => source.closed)).toBe(true);
  });
  it('切换任务后旧请求迟到不能写入新轨迹', async () => /* 覆盖浏览器快速切换记录的异步竞态。 */ {
    let resolveOld!: (value: EventsResponse) => void;
    vi.mocked(getEvents)
      .mockImplementationOnce(
        /* 保存尚未完成的旧任务读取。 */ () =>
          new Promise<EventsResponse>(
            /* 仅用于模拟迟到响应。 */ (resolve) => {
              resolveOld = resolve;
            },
          ),
      )
      .mockResolvedValue({ items: [makeEvent(1, 'run-b')], has_more: false, last_seq: 1 });
    const { result, rerender } = renderHook(
      /* 根据路由任务切换订阅。 */ ({ runId }) => useRunEvents(runId, false, vi.fn()),
      { initialProps: { runId: 'run-a' } },
    );
    rerender({ runId: 'run-b' });
    await waitFor(
      /* 等待新任务历史。 */ () => expect(result.current.events[0]?.run_id).toBe('run-b'),
    );
    await act(async () => /* 旧请求在新任务之后返回。 */ {
      resolveOld({ items: [makeEvent(1)], has_more: false, last_seq: 1 });
    });
    expect(result.current.events.map(/* 轨迹只能属于新任务。 */ (event) => event.run_id)).toEqual([
      'run-b',
    ]);
    expect(FakeSource.instances).toHaveLength(0);
  });
});
