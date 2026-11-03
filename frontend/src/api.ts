import type {
  PermissionAction,
  PermissionState,
  PermissionPolicy,
  ArtifactRef,
  ConversationPage,
  LocalProjectOption,
  ConversationView,
  FollowupRequest,
  CreateRunRequest,
  EventsResponse,
  DiffResponse,
  ProjectReview,
  OptionsResponse,
  RunView,
  DesktopRunSettings,
  JobView,
  TaskSnapshot,
} from './types';

/** 读取runId的权限与分页历史；historical=true固定当轮，不创建审批或作业。 */
export function getPermissions(runId: string, historical = false, offset = 0): Promise<{ state: PermissionState; policy: PermissionPolicy; items: PermissionAction[]; total: number }> {
  return request(`/api/runs/${runId}/permissions?historical=${historical}&offset=${offset}`);
}

/** 保存桌面默认设置并返回最新能力；只影响之后提交的任务，不改变活动任务快照。 */
export function saveDesktopSettings(
  config: DesktopRunSettings,
  downloadsDirectory: string,
): Promise<OptionsResponse> {
  return request('/api/desktop/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, downloads_directory: downloadsDirectory }),
  });
}

/** 将 secret 交给桌面宿主进行系统加密保存；空字符串明确清除，返回值不包含密钥正文。 */
export function saveDesktopCredential(secret: string): Promise<{ key_configured: boolean }> {
  return request('/api/desktop/credential', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ secret }),
  });
}

/** 由原生宿主处理用户点击的 url；下载/定位仅接受已登记产物，外链只开放网页与邮件。 */
export function desktopAction(
  action: 'download' | 'reveal' | 'external',
  url: string,
): Promise<{ filename?: string; path?: string; opened?: boolean }> {
  return request(`/api/desktop/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
}

/** HTTP 请求失败；message 使用后端受控文案，code 用于稳定分类。 */
export class ApiError extends Error {
  readonly code: string; // 后端稳定错误码或 network_error。
  readonly status: number; // HTTP 状态，网络错误为 0。
  readonly details: { conflicts?: import('./types').MemoryConflict[]; session_id?: string } | null; // 同项目冲突审阅信息，其他错误为空。
  /** 根据安全提示与状态构建错误；参数不应包含密钥，返回 Error 实例。 */
  constructor(message: string, code: string, status: number, details: ApiError['details'] = null) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

/** 请求同源 API 并返回 JSON；signal 用于页面离开时停止读取，不取消后台任务。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, cache: 'no-store' }); // API状态可变，恢复后不能沿用浏览器缓存的410。
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
      body?.error?.details || null,
    );
  }
  return response.json() as Promise<T>;
}

/** 读取宿主允许的工作区、验收器和默认配置；可传 AbortSignal 中止本次请求。 */
export function getOptions(signal?: AbortSignal): Promise<OptionsResponse> {
  return request('/api/options', { signal });
}

/** 读取作业状态；只观察，不重新投递或调用模型。 */
export function getJob(id: string): Promise<JobView> {
  return request('/api/jobs/' + encodeURIComponent(id));
}

/** 只读parentId直属childId的轨迹和报告；after为子事件序号，offset为报告UTF-8字节偏移。 */
export function getSubagent(
  parentId: string,
  childId: string,
  after = 0,
  offset = 0,
  signal?: AbortSignal,
): Promise<import('./types').SubagentDetail> {
  return request(
    `/api/runs/${encodeURIComponent(parentId)}/subagents/${encodeURIComponent(childId)}?after=${after}&offset=${offset}`,
    { signal },
  );
}

/** 停止parentId直属childId并等待封存；重复调用返回原终态，不停止父或兄弟研究。 */
export function cancelSubagent(
  parentId: string,
  childId: string,
): Promise<{ subagent: import('./types').SubagentRecord }> {
  return request(
    `/api/runs/${encodeURIComponent(parentId)}/subagents/${encodeURIComponent(childId)}/cancel`,
    { method: 'POST' },
  );
}

/** 返回后台清单与暂停状态，供后台中心刷新。 */
export function getJobs(): Promise<{ items: JobView[]; paused: boolean }> {
  return request('/api/jobs');
}

/** 明确执行作业 action；resolve 表示用户已核对进程及目录。 */
export function jobAction(id: string, action: 'cancel' | 'resolve'): Promise<JobView> {
  return request(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
}

/** 明确恢复重启后保留的队列，待核对项存在时后端拒绝。 */
export function resumeQueue(): Promise<{ paused: boolean }> {
  return request('/api/queue/resume', { method: 'POST' });
}

/** 读取任务；historical 为 true 时只读当轮固定快照。 */
export function getTasks(runId: string, historical = false): Promise<TaskSnapshot> {
  return request(`/api/runs/${encodeURIComponent(runId)}/tasks?historical=${historical}`);
}

/** 人工任务变更；body 携带稳定请求编号及编辑时的任务版本，返回精简回执。 */
export function mutateTask(runId: string, body: object, create = false): Promise<object> {
  return request(`/api/runs/${encodeURIComponent(runId)}/tasks`, {
    method: create ? 'POST' : 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** 提交新任务，返回已登记视图；失败时调用方保留表单内容。 */
export function createRun(
  body: CreateRunRequest,
  requestId: string = crypto.randomUUID(),
): Promise<JobView> {
  return request('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, create: body }),
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

/** 返回 runId 内 version 的 Markdown 附件地址；版本从 1 开始，下载不发起执行或改变批准。 */
export function planMarkdownUrl(runId: string, version: number): string {
  return artifactUrl(runId, { location: 'run', path: `plans/v${version}.md`, kind: 'file' });
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
export function createFollowup(runId: string, body: FollowupRequest): Promise<JobView> {
  return request('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: body.request_id, parent_run_id: runId, followup: body }),
  });
}

/** 打开原生目录窗口，返回临时selection草稿或取消；不会立即登记项目或创建任务。 */
export function pickProject(): Promise<{
  cancelled: boolean;
  selection: import('./types').ProjectSelection | null;
}> {
  return request('/api/projects/pick', { method: 'POST' });
}

/** 用10分钟内的selectionId确认绑定并保存label；同目录复用已有项目，失败保留表单。 */
export function createProject(selectionId: string, label: string): Promise<LocalProjectOption> {
  return request('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selection_id: selectionId, label }),
  });
}

/** 把id项目重命名为label；只修改名称，保留绑定目录、运行设置和会话归属。 */
export function renameProject(id: string, label: string): Promise<LocalProjectOption> {
  return request('/api/projects/' + encodeURIComponent(id), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label }),
  });
}

/** 重新核验已登记项目id并更新最近时间；只读打开，不调用模型。 */
export function openProject(id: string): Promise<LocalProjectOption> {
  return request('/api/projects/' + encodeURIComponent(id) + '/open', { method: 'POST' });
}

/** 保存项目下一轮使用的测试设置；空命令清空，保存动作不执行程序。 */
export function saveProjectSettings(
  id: string,
  command: string,
  seconds: number,
  pythonExecutable = '', // 空白清除项目解释器，按 PATH 查找。
): Promise<LocalProjectOption> {
  return request('/api/projects/' + encodeURIComponent(id) + '/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      test_command: command,
      test_timeout_seconds: seconds,
      python_executable: pythonExecutable,
    }),
  });
}

/** 查询会话页；默认全项目、offset=0、limit=20，按最新轮状态筛选，只读不执行。 */
export function getConversations(
  filters: {
    projectId?: string;
    offset?: number;
    limit?: number;
    status?: string;
    state?: string;
    query?: string;
    ungrouped?: boolean;
  } = {},
  signal?: AbortSignal,
): Promise<ConversationPage> {
  const query = new URLSearchParams({
    offset: String(filters.offset || 0),
    limit: String(filters.limit || 20),
  });
  if (filters.projectId) query.set('project_id', filters.projectId);
  if (filters.status) query.set('status', filters.status);
  if (filters.state) query.set('state', filters.state);
  if (filters.query) query.set('q', filters.query);
  if (filters.ungrouped) query.set('ungrouped', 'true');
  return request('/api/conversations?' + query, { signal });
}

/** 读取 scope 绑定的当前项目列表或当前运行候选；只读，不启动执行。 */
export function getMemories(scope: string, params = ''): Promise<import('./types').MemoryPage> {
  return request('/api/' + scope + '/memories' + params);
}

/** 保存或更新 scope 的记忆；body 发送后保持编号及载荷一致，返回服务端修订。 */
export function mutateMemory(
  scope: string,
  id: string | null,
  body: object,
): Promise<import('./types').MemoryEntry> {
  return request('/api/' + scope + '/memories' + (id ? '/' + encodeURIComponent(id) : ''), {
    method: id ? 'PATCH' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** 读取 scope 中 id 的不可变修订；仅供人工追溯，不把旧正文注入活动运行。 */
export function getMemoryRevisions(
  scope: string,
  id: string,
): Promise<{ items: import('./types').MemoryEntry[] }> {
  return request('/api/' + scope + '/memories/' + encodeURIComponent(id) + '/revisions');
}

/** 读取id会话的管理信息；回收站和清理墓碑仍可读取，不返回消息正文。 */
export function getConversationManagement(
  id: string,
): Promise<import('./types').ConversationManagement> {
  return request('/api/conversations/' + encodeURIComponent(id));
}

/** 按id和body中的稳定请求编号修改管理状态；不提交执行目录或模型配置。 */
export function manageConversation(
  id: string,
  body: object,
  purge = false,
): Promise<import('./types').ConversationManagement> {
  return request('/api/conversations/' + encodeURIComponent(id), {
    method: purge ? 'DELETE' : 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** 对body明确选中的当前页会话批量整理；逐项返回成功或受控失败，不包含彻底删除。 */
export function manageConversationBatch(body: object): Promise<{
  items: {
    session_id: string;
    success: boolean;
    conversation?: import('./types').ConversationManagement;
    error?: { code: string; message: string };
  }[];
}> {
  return request('/api/conversations/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
