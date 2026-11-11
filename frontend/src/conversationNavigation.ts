/** 只保存本标签页最近打开会话的列表位置；路由状态携带完整筛选 URL。 */
const LIST_POSITION_KEY = 'harness-conversation-list-position';

/** 记录 path 列表当前滚动位置，返回会话时一次性恢复。 */
export function rememberConversationList(path: string): void {
  try {
    sessionStorage.setItem(LIST_POSITION_KEY, JSON.stringify({ path, top: window.scrollY }));
  } catch {
    // 存储不可用时仍能通过会话链接返回原筛选。
  }
}

/** path 对应的列表渲染后恢复上次阅读位置；进入其他列表时不使用无关记录。 */
export function restoreConversationList(path: string): void {
  try {
    const saved = JSON.parse(sessionStorage.getItem(LIST_POSITION_KEY) || 'null');
    if (saved?.path === path && typeof saved.top === 'number' && saved.top >= 0) {
      sessionStorage.removeItem(LIST_POSITION_KEY);
      if (saved.top !== window.scrollY) window.scrollTo({ top: saved.top, behavior: 'instant' });
    }
  } catch {
    // 位置无法恢复时保留浏览器当前滚动。
  }
}

/** 从 state 提取内部会话列表 URL；缺少来源时返回 undefined，由详情选择默认入口。 */
export function conversationListFrom(state: unknown): string | undefined {
  const from = (state as { conversationList?: unknown } | null)?.conversationList;
  if (typeof from !== 'string') return undefined;
  const [path] = from.split('?');
  return path === '/runs' || /^\/projects\/[^/?#\\]+$/.test(path) ? from : undefined;
}
