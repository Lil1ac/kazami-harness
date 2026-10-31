import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';

/** 观察已提交任务/作业事件；enabled=false 不订阅，原有轮询同时承担断线回退，返回 void。 */
export function useWorkEvents(enabled = true): void {
  const cache = useQueryClient();
  useEffect(
    /* EventSource 自动携带最后游标重连；读取不恢复任务，卸载时关闭连接。 */ () => {
      if (!enabled || typeof EventSource === 'undefined') return;
      const source = new EventSource('/api/work-stream'); // 新连接从当前事件尾部开始，避免重放多年列表。
      source.onmessage = /* 收到提交事件后重新读取事实，页面不自行推进状态。 */ () => {
        void cache.invalidateQueries({ queryKey: ['tasks'] });
        void cache.invalidateQueries({ queryKey: ['jobs'] });
        void cache.invalidateQueries({ queryKey: ['job'] });
      };
      return /* 关闭观察不取消运行。 */ () => source.close();
    },
    [enabled, cache],
  );
}
