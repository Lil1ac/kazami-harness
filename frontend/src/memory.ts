import type { MemoryContent, MemoryDetails, MemoryEntry, MemoryType } from './types';

/** 业务类型与后端专属策略一一对应。 */
export const memoryTypes: Record<MemoryType, string> = {
  rule: '行为约定',
  decision: '项目决策',
  fact: '项目事实',
  reference: '信息入口',
  experience: '历史经验',
};

/** 形成方式独立于类型，不改变宿主确认和权限。 */
export const memoryOrigins: Record<MemoryContent['origin'], string> = {
  user_correction: '用户纠正',
  user_confirmation: '用户确认',
  tool_observation: '工具观察',
  run_experience: '运行经历',
};

/** 宿主阶段的用户可读名称，不暴露运行协议枚举。 */
export const memoryPhaseLabels: Record<string, string> = {
  planning: '规划',
  execution: '执行',
  verification: '验收修复',
};

/** 实际执行平台的用户可读名称。 */
export const memoryPlatformLabels: Record<string, string> = {
  win32: 'Windows',
  linux: 'Linux',
  darwin: 'macOS',
};

/** 类型必要字段；required 表示必填，multiline 仅影响编辑布局。 */
export const memoryFields: Record<
  MemoryType,
  { key: string; label: string; required?: boolean; multiline?: boolean }[]
> = {
  rule: [
    { key: 'topic', label: '约定主题', required: true },
    { key: 'behavior', label: '期望行为', required: true, multiline: true },
    { key: 'reason', label: '约定理由' },
    { key: 'exceptions', label: '例外条件' },
  ],
  decision: [
    { key: 'topic', label: '决策主题', required: true },
    { key: 'choice', label: '方案选择', required: true, multiline: true },
    { key: 'rationale', label: '选择理由', required: true },
    { key: 'revisit_when', label: '重评条件' },
  ],
  fact: [
    { key: 'subject', label: '事实对象', required: true },
    { key: 'attribute', label: '对象属性', required: true },
    { key: 'value', label: '属性值', required: true, multiline: true },
    { key: 'observed_at', label: '观察时间（含时区）' },
  ],
  reference: [
    { key: 'target', label: '参考入口', required: true },
    { key: 'purpose', label: '入口用途', required: true },
    { key: 'consult_when', label: '查阅时机' },
  ],
  experience: [
    { key: 'situation', label: '当时情境', required: true },
    { key: 'action', label: '实际行动', required: true },
    { key: 'outcome', label: '实际结果', required: true, multiline: true },
    { key: 'limitations', label: '适用限制', required: true },
  ],
};

/** 创建 type 空白结构；不推测知识，事实时间缺省 null，返回独立对象。 */
export function emptyDetails(type: MemoryType): MemoryDetails {
  return Object.fromEntries(
    memoryFields[type].map(
      /* 未提供的时间不伪造。 */ (field) => [field.key, field.key === 'observed_at' ? null : ''],
    ),
  ) as MemoryDetails;
}

/** 创建空白草稿；type 默认事实，范围默认不限，保存前须填写主值。 */
export function emptyMemory(type: MemoryType = 'fact'): MemoryContent {
  return {
    title: '',
    content: '',
    type,
    details: emptyDetails(type),
    origin: 'user_confirmation',
    tags: [],
    applies_to: { paths: [], phases: [], platforms: [] },
    evidence: [],
    branch: null,
    commit: null,
    expires_at: null,
  };
}

/** 读取供人工阅读的类型字段；省略空值，不查询当前库，返回中文标签和值。 */
export function detailRows(
  type: MemoryType,
  details: MemoryDetails,
): { label: string; value: string }[] {
  const values = details as unknown as Record<string, string | null>;
  return memoryFields[type]
    .filter(/* 可选空值不占展示空间。 */ (field) => values[field.key])
    .map(/* 保持标签与值对应。 */ (field) => ({ label: field.label, value: values[field.key]! }));
}

/** 记忆生命周期的中文展示，删除仅影响后续使用，旧轮固定证据仍保留。 */
export const memoryStatuses: Record<MemoryEntry['status'], string> = {
  active: '已生效',
  proposed: '待保存',
  rejected: '已拒绝',
  deleted: '已删除',
};
