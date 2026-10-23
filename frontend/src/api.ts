import type {
  ArtifactRef,
  ConversationView,
  FollowupRequest,
  CreateRunRequest,
  EventsResponse,
  DiffResponse,
  ProjectReview,
  OptionsResponse,
  RunView,
} from './types';

/** HTTP 请求失败；message 使用后端受控文案，code 用于稳定分类。 */
export class ApiError extends Error {
  readonly code: string; // 后端稳定错误码或 network_error。
  readonly status: number; // HTTP 状态，网络错误为 0。
  /** 根据安全提示与状态构建错误；参数不应包含密钥，返回 Error 实例。 */
  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

/** 请求同源 API 并返回 JSON；signal 用于页面离开时停止读取，不取消后台任务。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('无法连接本地服务，请确认服务仍在运行。', 'network_error', 0);
  }
  if (!response.ok) {
    const body = await response
      .json()
      .catch(/* 非 JSON 失败使用固定提示，不回显 HTML。 */ () => null);
    throw new ApiError(
      body?.error?.message || '请求未完成，请稍后重试。',
      body?.error?.code || 'http_error',
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

/** 读取宿主允许的工作区、验收器和默认配置；可传 AbortSignal 中止本次请求。 */
export function getOptions(signal?: AbortSignal): Promise<OptionsResponse> {
  return request('/api/options', { signal });
}

/** 分页查询历史任务；status 为空时不筛选，返回列表和总数。 */
export function getRuns(
  offset: number,
  status: string,
  signal?: AbortSignal,
): Promise<{ items: RunView[]; total: number; busy_run_id: string | null }> {
  const query = new URLSearchParams({ offset: String(offset), limit: '12' });
  if (status) query.set('status', status);
  return request(`/api/runs?${query}`, { signal });
}

/** 提交新任务，返回已登记视图；失败时调用方保留表单内容。 */
export function createRun(body: CreateRunRequest): Promise<RunView> {
  return request('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** 读取单次任务快照；不会触发模型调用。 */
export function getRun(runId: string, signal?: AbortSignal): Promise<RunView> {
  return request(`/api/runs/${encodeURIComponent(runId)}`, { signal });
}

/** 幂等请求后台取消；成功返回仍可能处于清理中的视图。 */
export function cancelRun(runId: string): Promise<RunView> {
  return request(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
}

/** 从 after 之后读取最多 limit 条事件；用于初次加载、缺口补齐和历史展开。 */
export function getEvents(
  runId: string,
  after: number,
  limit = 100,
  signal?: AbortSignal,
): Promise<EventsResponse> {
  return request(
    `/api/runs/${encodeURIComponent(runId)}/events?${new URLSearchParams({ after: String(after), limit: String(limit) })}`,
    { signal },
  );
}

/** 生成受控产物 URL；preview=false 时后端以附件提供，路径仍由后端校验。 */
export function artifactUrl(runId: string, artifact: ArtifactRef, preview = false): string {
  const query = new URLSearchParams({ location: artifact.location, path: artifact.path });
  if (preview) query.set('preview', 'true');
  return `/api/runs/${encodeURIComponent(runId)}/artifacts?${query}`;
}

/** 请求 UTF-8 有界文本预览；二进制或不可读文件由后端给出明确错误。 */
export function previewArtifact(
  runId: string,
  artifact: ArtifactRef,
): Promise<{ content: string; truncated: boolean; size_bytes: number; encoding: 'utf-8' }> {
  return request(artifactUrl(runId, artifact, true));
}

/** 把未知异常转为用户可读文案；不把对象或堆栈直接写进页面。 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '操作未完成，请重试。';
}

/** 读取任务保存的代码补丁预览；signal 只取消下载，不影响工作树或后台任务。 */
export function getDiff(runId: string, signal?: AbortSignal): Promise<DiffResponse> {
  return request(`/api/runs/${encodeURIComponent(runId)}/diff`, { signal });
}

/** 生成完整补丁附件 URL；即使任务工作树已丢弃也从运行证据下载。 */
export function patchUrl(runId: string): string {
  return `/api/runs/${encodeURIComponent(runId)}/patch`;
}

/** 明确记录人工审阅；keep 只保留工作树，discard 仅移除本任务工作树，均不合并代码。 */
export function reviewRun(runId: string, decision: 'keep' | 'discard'): Promise<ProjectReview> {
  return request(`/api/runs/${encodeURIComponent(runId)}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  });
}

/** 读取终止任务的审阅记录；宿主可为重启遗留 pending 状态补捕获一次，不恢复任务执行。 */
export function getProjectReview(runId: string, signal?: AbortSignal): Promise<ProjectReview> {
  return request(`/api/runs/${encodeURIComponent(runId)}/review`, { signal });
}

/** 读取某运行所属会话，不启动执行；signal只取消HTTP读取。 */
export function getConversation(runId: string, signal?: AbortSignal): Promise<ConversationView> {
  return request(`/api/runs/${encodeURIComponent(runId)}/conversation`, { signal });
}

/** 幂等追加新要求，成功返回新轮或原请求对应运行；禁止自动换请求编号重试。 */
export function createFollowup(runId: string, body: FollowupRequest): Promise<RunView> {
  return request(`/api/runs/${encodeURIComponent(runId)}/followups`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
