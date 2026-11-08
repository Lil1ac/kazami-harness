import type { ModelProvider, ModelSelection } from './types';

/** 显式供应商名称，供设置、任务和历史共用。 */
const builtinNames: Partial<Record<ModelProvider, string>> = {
  deepseek: 'DeepSeek',
  qwen: 'Qwen',
};

/** 优先使用登记名称，历史缺少名称时保留原始 provider 标识。 */
export function modelProviderName(provider: ModelProvider, name?: string | null): string {
  return name || builtinNames[provider] || provider;
}

/** 显示 selection 的供应商和模型；actual 缺省时显示已冻结的请求模型。 */
export function modelLabel(selection: ModelSelection, actual?: string | null, name?: string | null): string {
  return modelProviderName(selection.provider, name) + ' · ' + (actual || selection.requested_model);
}
