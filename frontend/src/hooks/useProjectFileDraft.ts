import { useState } from 'react';
import type { ProjectFileReference, ProjectFileTarget } from '../types';

/** 一个发送入口最近选择的来源和引用；浏览器草稿只保存路径、行段和版本。 */
interface ReferenceDraft {
  scope: string; // 登记项目、执行方式与基线，或当前续轮编号。
  items: ProjectFileReference[]; // 源文件内容始终由宿主提供。
}

/** 从 key 恢复当前标签页引用；损坏的草稿视为空，不访问项目文件。 */
function readDraft(key: string): ReferenceDraft | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (
      typeof value?.scope === 'string' &&
      Array.isArray(value.items) &&
      value.items.length <= 8 &&
      value.items.every(
        /* 检查预览所需字段，避免恢复任意结构的本地数据。 */ (item: ProjectFileReference) =>
          item &&
          typeof item.path === 'string' &&
          /^[a-f0-9]{64}$/.test(item.version) &&
          (item.start_line === null || Number.isSafeInteger(item.start_line)) &&
          (item.end_line === null || Number.isSafeInteger(item.end_line)),
      )
    )
      return value;
  } catch {
    // 浏览器禁用草稿存储时仍保留当前编辑中的选择。
  }
  return null;
}

/** 随稳定 draftKey 管理引用；target 变化后只显示与实际执行位置匹配的资料。 */
export function useProjectFileDraft(draftKey: string, target?: ProjectFileTarget) {
  const key = draftKey + ':file-references';
  const scope = target ? JSON.stringify(target) : '';
  const [saved, setSaved] = useState(/* 初次进入输入区才恢复草稿。 */ () => readDraft(key));
  const items = scope && saved?.scope === scope ? saved.items : [];

  /** 保存 items 到当前来源；清空后移除草稿，存储失败时只使用当前内存状态。 */
  function change(next: ProjectFileReference[]): void {
    const draft = next.length ? { scope, items: next } : null;
    setSaved(draft);
    try {
      if (draft) sessionStorage.setItem(key, JSON.stringify(draft));
      else sessionStorage.removeItem(key);
    } catch {
      // 文件引用仍可随当前输入发送。
    }
  }

  return { items, change, scope };
}
