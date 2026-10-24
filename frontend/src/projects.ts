import type { LocalProjectOption, OptionsResponse } from './types';

/** 从服务端目录快照取得个人项目；示例不进入项目导航，缺省时补齐已有预设类型。 */
export function localProjects(options: OptionsResponse): LocalProjectOption[] {
  return (
    options.recent_projects || [
      ...(options.projects || []).map(
        /* 已有Git预设保留稳定编号。 */ (item) => ({ ...item, kind: 'project' as const }),
      ),
      ...options.workspaces
        .filter(/* 示例仅用于显式试用。 */ (item) => !item.is_example)
        .map(
          /* 普通目录同样可以拥有多个会话。 */ (item) => ({ ...item, kind: 'workspace' as const }),
        ),
    ]
  );
}
