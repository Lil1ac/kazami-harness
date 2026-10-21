import { createContext, useContext, useState } from 'react';
import type { ReactNode } from 'react';
import type { ConfigOverrides, RunConfig } from './types';

/** 本浏览器的临时配置上下文；刷新后恢复宿主默认值，不持久化任何凭据。 */
interface DraftContextValue {
  draft: ConfigOverrides; // 仅用于后续任务的明确覆盖。
  setDraft: (draft: ConfigOverrides) => void; // 替换浏览器内覆盖值。
}
const DraftContext = createContext<DraftContextValue | null>(null);

/** 为工作台提供配置草稿；children 为页面，不读写后端全局设置。 */
export function ConfigDraftProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState<ConfigOverrides>({}); // 初始为空即沿用服务端默认。
  return <DraftContext.Provider value={{ draft, setDraft }}>{children}</DraftContext.Provider>;
}

/** 读取当前浏览器草稿；缺少 Provider 属于程序错误。 */
export function useConfigDraft(): DraftContextValue {
  const value = useContext(DraftContext);
  if (!value) throw new Error('配置上下文缺失');
  return value;
}

/** 将局部覆盖合并到宿主配置，返回只用于展示的新对象，不修改输入。 */
export function effectiveConfig(base: RunConfig, draft: ConfigOverrides): RunConfig {
  return { ...base, ...draft, limits: { ...base.limits, ...draft.limits } };
}
