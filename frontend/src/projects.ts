import type { QueryClient } from '@tanstack/react-query';
import type { LocalProjectOption, OptionsResponse } from './types';

/** 用服务端project刷新共享cache；recent=true置顶最近打开，false保留排序，返回void。 */
export function cacheProject(cache: QueryClient, project: LocalProjectOption, recent = true): void {
  cache.setQueryData<OptionsResponse>(
    ['options'],
    /* 沿用配置与能力，只替换同一编号的登记。 */ (current) => {
      if (!current) return current;
      const entries = current.recent_projects;
      return {
        ...current,
        recent_projects: recent
          ? [project, ...entries.filter(/* 一个目录只占一项。 */ (item) => item.id !== project.id)]
          : entries.map(
              /* 改名不改变最近顺序。 */ (item) => (item.id === project.id ? project : item),
            ),
      };
    },
  );
  void cache.invalidateQueries({ queryKey: ['options'] });
  void cache.invalidateQueries({ queryKey: ['conversations'] });
  void cache.invalidateQueries({ queryKey: ['conversation'] });
}
