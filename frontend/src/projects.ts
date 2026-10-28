import type { LocalProjectOption, OptionsResponse } from './types';

/** 从服务端目录快照取得个人项目；未提供聚合列表时补齐已有预设的目录类型。 */
export function localProjects(options: OptionsResponse): LocalProjectOption[] {
  return (
    options.recent_projects || [
      ...(options.projects || []).map(
        /* 已有Git预设保留稳定编号。 */ (item) => ({ ...item, kind: 'project' as const }),
      ),
      ...options.workspaces.map(
        /* 普通目录同样可以拥有多个会话。 */ (item) => ({ ...item, kind: 'workspace' as const }),
      ),
    ]
  );
}
