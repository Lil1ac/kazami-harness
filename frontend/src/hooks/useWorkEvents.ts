import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';

/** 观察已提交任务/作业事件；enabled=false 不订阅，原有轮询同时承担断线回退，返回 void。 */
export function useWorkEvents(enabled = true): void {
  const cache = useQueryClient();
  useEffect(
    /* EventSource 自动携带最后游标重连；读取不恢复任务，卸载时关闭连接。 */ () => {
      if (!enabled || typeof EventSource === 'undefined') return;
      const source = new EventSource('/api/work-stream'); // 新连接从当前事件尾部开始，避免重放多年列表。
      source.onmessage = /* 收到提交事件后按领域刷新，运行事实仍由原SSE及轮询读取。 */ (event) => {
        let type = ''; // 未知事件仍触发基础状态回读，不把损坏内容当成事实。
        try {
          type = JSON.parse(event.data).type;
        } catch {
          /* 空事件采用安全回读。 */
        }
        void cache.invalidateQueries({ queryKey: ['tasks'] });
        void cache.invalidateQueries({ queryKey: ['jobs'] });
        void cache.invalidateQueries({ queryKey: ['job'] });
        if (type !== 'tasks_updated') {
          void cache.invalidateQueries({ queryKey: ['conversations'] });
          void cache.invalidateQueries({ queryKey: ['conversation'] });
          void cache.invalidateQueries({ queryKey: ['conversation-management'] });
        }
      };
      return /* 关闭观察不取消运行。 */ () => source.close();
    },
    [enabled, cache],
  );
}
