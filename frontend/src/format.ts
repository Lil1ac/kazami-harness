import type { ProjectReview } from './types';

/** 格式化毫秒为可读耗时；负值按零处理，不返回倒计时推断。 */
export function duration(milliseconds: number): string {
  const seconds = Math.max(0, milliseconds) / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
}

/** 将带时区时间转换为浏览器本地时间；非法值显示占位。 */
export function dateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
}

/** 将任意 JSON 数据变成安全纯文本；由 React 转义，不使用 innerHTML。 */
export function pretty(value: unknown): string {
  return typeof value === 'string' ? value : (JSON.stringify(value, null, 2) ?? '');
}

/** 将宿主阶段码翻译成任务操作语言；未知阶段保留原始码便于定位。 */
export function phaseLabel(phase: string): string {
  return (
    (
      {
        created: '等待执行',
        starting: '开始执行',
        model: '等待模型',
        model_request: '等待模型',
        context_compaction: '整理历史上下文',
        model_response: '处理模型响应',
        tool: '执行工具',
        tool_execution: '执行工具',
        hook_execution: '执行 Hook',
        subagent_execution: '子 Agent 正在研究',
        budget_handoff: '整理交接结论',
        tool_result: '处理工具结果',
        execution_completed: '执行完成',
        verification: '独立验收',
        repair: '自动修复',
        project_snapshot: '整理代码改动',
        artifact_collection: '整理交付文件',
        workspace_snapshot: '保存工作区快照',
        verification_result: '保存验收结果',
        finished: '已结束',
        planning_complete: '规划已结束',
        plan_rejected: '已拒绝计划',
        user_question: '等待回答',
        plan_approval: '等待批准',
        action_approval: '等待动作审批',
        permission_rejected: '已拒绝动作',
        permission_cancelled: '已取消动作',
        verification_permission_denied: '验收未获授权',
        interaction_cancelled: '已取消等待',
        user_paused: '已暂停',
        subagent_paused: '子 Agent 已暂停',
        team_attention_required: '等待处理团队任务',
        running: '正在执行',
      } as Record<string, string>
    )[phase] ?? phase
  );
}

/** 人工审阅状态文案；独立于运行是否完成和测试是否通过。 */
export const reviewLabels: Record<ProjectReview['status'], string> = {
  pending: '等待审阅',
  kept: '已保留工作树',
  discarded: '已丢弃工作树',
};

/** 将模型证书失败reason翻译为操作文案；其他或尚未结束的原因返回null，保留原有状态规则。 */
export function modelFailureLabel(reason: string | null | undefined): string | null {
  switch (reason) {
    case 'model_tls_verification': return '模型服务证书校验失败';
    case 'model_credentials_invalid': return '请配置当前模型供应商的密钥';
    case 'model_authentication': return '模型服务认证失败，请检查密钥和地域';
    case 'model_rate_limited': return '模型服务限流';
    case 'model_stream_interrupted': return '模型响应中断';
    case 'model_unavailable': return '模型服务暂时不可用';
    case 'model_transport': return '模型服务连接失败';
    case 'model_bad_request': return '模型服务拒绝请求，请检查模型配置';
    case 'model_provider_changed': return '服务商连接已变更，请使用当前配置新建会话';
    case 'model_provider_disabled': return '所选自定义服务商已停用';
    case 'model_redirect': return '接口返回重定向，请填写实际服务地址';
    case 'model_probe_tool_missing': return '模型未返回预期的测试工具调用';
    case 'model_protocol_error': return '模型响应格式无效';
    case 'model_timeout': return '模型服务响应超时';
    default: return null;
  }
}
