import type { ModelProvider, ModelSelection, OptionsResponse, RunConfig } from './types';

/** 优先使用登记名称，历史缺少名称时保留原始 provider 标识。 */
export function modelProviderName(provider: ModelProvider | null, name?: string | null): string {
  return name || provider || '未配置模型';
}

/** 显示 selection 的供应商和模型；actual 缺省时显示已冻结的请求模型。 */
export function modelLabel(selection: Pick<RunConfig, 'provider' | 'requested_model'>, actual?: string | null, name?: string | null): string {
  return selection.requested_model
    ? modelProviderName(selection.provider, name) + ' · ' + (actual || selection.requested_model)
    : '未配置模型';
}

/** 将 selection 编码为下拉控件值，两个字段允许各自包含分隔符。未选时返回 undefined。 */
export function modelChoiceValue(selection?: Pick<RunConfig, 'provider' | 'requested_model'>): string | undefined {
  return selection?.provider && selection.requested_model ? JSON.stringify([selection.provider, selection.requested_model]) : undefined;
}

/** 列出目录中所有启用的模型，返回可供下拉选择的标签、值及实际配置。 */
export function modelChoices(directory: OptionsResponse['model_providers']) {
  return (Object.entries(directory) as [ModelProvider, OptionsResponse['model_providers'][ModelProvider]][])
    .filter(/* 停用服务商仍留在目录设置，任务与角色选择仅显示启用模型。 */ ([, item]) => item.enabled)
    .flatMap(/* 同一连接可以选择多个模型。 */ ([provider, item]) =>
      [item.default_model, ...item.models].map((requested_model) => {
        const selection: ModelSelection = { provider, requested_model };
        return { selection, value: modelChoiceValue(selection)!, label: modelLabel(selection, undefined, item.name) };
      }),
    );
}
