import { useEffect, useRef, useState } from 'react';
import { errorMessage, getEvents } from '../api';
import { hasEventGap, mergeEvents, parseRunEvent } from '../events';
import type { RunEvent } from '../types';

/** 单任务订阅状态；断线时仍保留已读取事件。 */
export type ConnectionState = 'loading' | 'live' | 'reconnecting' | 'closed' | 'error';

/** 管理唯一 SSE 连接；初始分页、断线及缺口均从持久化历史补齐，卸载只关闭订阅。 */
export function useRunEvents(runId: string, active: boolean, onSettled: () => void) {
  const [events, setEvents] = useState<RunEvent[]>([]); // 有界事件窗口，默认 400 条。
  const [connection, setConnection] = useState<ConnectionState>('loading'); // 当前订阅可见状态。
  const [error, setError] = useState<string | null>(null); // 受控读取错误。
  const [loadingOlder, setLoadingOlder] = useState(false); // 防止重复历史展开。
  const generation = useRef(0); // 页面切换后拒绝旧异步响应。
  const windowLimit = useRef(400); // 显式展开最多保留 1000 条，避免长任务无限增大 DOM。
  const onSettledRef = useRef(onSettled); // 回调更新不重建活动连接。
  onSettledRef.current = onSettled;

  useEffect(
    /* 每个 run_id/active 生命周期只持有一条连接，兼容 StrictMode 的挂载清理。 */ () => {
      const current = ++generation.current;
      const abort = new AbortController();
      let source: EventSource | null = null; // 该 effect 独占，清理时关闭。
      let retry: ReturnType<typeof setTimeout> | undefined; // 手动重连使用最新 after 游标。
      let cursor = 0; // 已连续补齐到的序号，不直接跳过缺失事件。
      let disposed = false; // 卸载后不再触发 setState 或刷新缓存。
      windowLimit.current = 400;
      setEvents([]);
      setConnection('loading');
      setError(null);
      setLoadingOlder(false);

      /** 检查 effect 是否仍代表当前任务；返回 false 时丢弃异步结果。 */
      function valid(): boolean {
        return !disposed && generation.current === current;
      }

      /** 从连续游标向后读完当前历史；返回后才允许重建实时连接。 */
      async function catchUp(): Promise<void> {
        let more = true;
        while (more && valid()) {
          const page = await getEvents(runId, cursor, 100, abort.signal);
          if (!valid()) return;
          const rows = page.items.filter(
            /* 只处理本次任务、游标之后的事件。 */ (event) =>
              event.run_id === runId && event.seq > cursor,
          );
          if (rows.length) {
            setEvents(
              /* 保留有界最新窗口，完整轨迹仍可从历史接口读取。 */ (previous) =>
                mergeEvents(previous, rows, runId, windowLimit.current),
            );
            cursor = rows[rows.length - 1].seq;
          }
          more = page.has_more && rows.length > 0;
        }
      }

      /** 关闭旧连接并延迟补齐历史；active=false 时无需重连。 */
      function reconnect(): void {
        source?.close();
        source = null;
        if (!valid() || !active || retry) return;
        setConnection('reconnecting');
        retry = setTimeout(
          /* 延迟避免服务短暂断开时形成重连风暴。 */ () => {
            retry = undefined;
            void start();
          },
          1200,
        );
      }

      /** 历史补齐后创建带游标的 SSE；错误保留轨迹并允许重试。 */
      async function start(): Promise<void> {
        try {
          await catchUp();
          if (!valid()) return;
          setError(null);
          if (!active) {
            setConnection('closed');
            return;
          }
          source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream?after=${cursor}`);
          source.onopen = /* 打开只表示连接成功，不修改任务终态。 */ () => {
            if (valid()) setConnection('live');
          };
          source.onerror = /* 丢弃浏览器旧地址自动重连，使用最新 after 重建。 */ () => reconnect();
          source.addEventListener(
            'run_event',
            /* 检查连续性，缺口先回到持久化记录补齐。 */ (message) => {
              if (!valid()) return;
              const event = parseRunEvent((message as MessageEvent<string>).data, runId);
              if (!event) {
                reconnect();
                return;
              }
              if (hasEventGap(cursor, event)) {
                reconnect();
                return;
              }
              if (event.seq <= cursor) return;
              cursor = event.seq;
              setEvents(
                /* 重复事件由序号消除，不依赖网络到达顺序。 */ (previous) =>
                  mergeEvents(previous, [event], runId, windowLimit.current),
              );
            },
          );
          source.addEventListener(
            'stream_end',
            /* 终态后读取一次历史并刷新任务快照。 */ () => {
              source?.close();
              source = null;
              if (!valid()) return;
              void catchUp()
                .then(
                  /* 完成后以服务端结果为准。 */ () => {
                    if (valid()) {
                      setConnection('closed');
                      onSettledRef.current();
                    }
                  },
                )
                .catch(
                  /* 末页读取失败仍刷新快照，并明确提示。 */ (failure: unknown) => {
                    if (valid()) {
                      setConnection('error');
                      setError(errorMessage(failure));
                      onSettledRef.current();
                    }
                  },
                );
            },
          );
        } catch (failure) {
          if (!valid() || abort.signal.aborted) return;
          setError(errorMessage(failure));
          setConnection('error');
          if (active) reconnect();
        }
      }
      void start();
      return /* 页面关闭只取消读取和订阅，绝不调用任务取消 API。 */ () => {
        disposed = true;
        abort.abort();
        source?.close();
        if (retry) clearTimeout(retry);
      };
    },
    [runId, active],
  );

  /** 向前补充最多 100 条已归档事件；达到 1000 条后不再扩大浏览器窗口。 */
  async function loadOlder(): Promise<void> {
    if (!events.length || loadingOlder || events[0].seq <= 1 || windowLimit.current >= 1000) return;
    const current = generation.current;
    const before = events[0].seq;
    setLoadingOlder(true);
    try {
      const page = await getEvents(runId, Math.max(0, before - 101), 100);
      if (current !== generation.current) return;
      windowLimit.current = Math.min(1000, windowLimit.current + 100);
      const older = page.items.filter(
        /* 仅插入当前窗口以前的历史。 */ (event) => event.seq < before,
      );
      setEvents(
        /* 增加窗口时保留现有实时事件。 */ (previous) =>
          mergeEvents(previous, older, runId, windowLimit.current),
      );
    } catch (failure) {
      if (current === generation.current) setError(errorMessage(failure));
    } finally {
      if (current === generation.current) setLoadingOlder(false);
    }
  }

  return {
    events,
    connection,
    error,
    loadingOlder,
    loadOlder,
    canLoadOlder: (events[0]?.seq ?? 1) > 1 && windowLimit.current < 1000,
  };
}
