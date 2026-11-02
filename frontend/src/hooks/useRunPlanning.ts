import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getTasks } from '../api';
import type { RunView } from '../types';

/** 读取 run 的任务并投影计划进度；historical=true 使用当轮快照，缺少 run 时不请求。
 * 返回 tasks 查询和独立 planning 副本；只读投影不改写历史计划正文、批准状态或服务端结果。
 */
export function useRunPlanning(run?: RunView, historical = false) {
  const tasks = useQuery({
    queryKey: ['tasks', run?.run_id || '', historical],
    queryFn: /* 与任务列表共享缓存，历史查询显式固定来源。 */ () =>
      getTasks(run!.run_id, historical),
    enabled: Boolean(run),
    refetchInterval: !historical && run?.active ? 1500 : false,
  });
  const planning = useMemo(
    /* 切换展示位置后仍以工作项状态计算步骤，不依赖旧 Run 的冻结进度。 */ () => {
      if (!run) return undefined;
      const state = structuredClone(run.result.planning);
      for (const plan of state.plans) {
        for (const step of plan.steps) {
          const task = tasks.data?.items.find(
            /* 版本与稳定步骤编号同时匹配，拒绝跨版本套用进度。 */ (item) =>
              item.source?.plan_version === plan.version && item.source.step_id === step.id,
          );
          if (task)
            step.status =
              task.blocked_reason || task.status === 'cancelled' ? 'blocked' : task.status;
        }
      }
      return state;
    },
    [run?.result.planning, tasks.data],
  );
  return { tasks, planning };
}
