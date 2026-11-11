import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Drawer, Skeleton } from 'antd';
import {
  ArrowLeftOutlined,
  AuditOutlined,
  CloseOutlined,
  CompressOutlined,
  ExpandOutlined,
  LayoutOutlined,
  LeftOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router';
import type { ApiError } from '../api';
import { cancelRun, errorMessage, getConversation, getOptions, getRun } from '../api';
import { ConversationActions } from '../components/ConversationActions';
import { ConversationRecovery } from '../components/ConversationRecovery';
import { useRunEvents } from '../hooks/useRunEvents';
import { mergeConfirmedRun, pendingDecisionLabel } from '../conversation';
import { conversationListFrom } from '../conversationNavigation';
import { RunStatus } from '../components/RunStatus';
import { ConversationPanel } from '../components/ConversationPanel';
import { RunInspector } from '../components/RunInspector';
import type { ConversationView, RunView } from '../types';

/** 根据1100px断点选择侧栏或抽屉；订阅仅影响布局，卸载释放监听。 */
function useNarrowWorkspace(): boolean {
  const [narrow, setNarrow] = useState(
    /* 首帧按实际视口选择，避免手机先闪现桌面栏。 */ () =>
      window.matchMedia('(max-width: 1100px)').matches,
  );
  useEffect(
    /* 视口变化时同步折叠布局。 */ () => {
      const media = window.matchMedia('(max-width: 1100px)');
      /** 把媒体查询结果应用于布局，不更改任务与会话。 */
      function update(): void {
        setNarrow(media.matches);
      }
      media.addEventListener('change', update);
      return /* 页面卸载时解除当前查询监听。 */ () => media.removeEventListener('change', update);
    },
    [],
  );
  return narrow;
}

/** 校验详情分类；空值或未知标签回到结果，不改变持续会话。 */
function inspectorTab(value: string | null): string {
  return value &&
    [
      'tasks',
      'plan',
      'agents',
      'result',
      'verification',
      'output',
      'files',
      'artifacts',
      'diagnostics',
    ].includes(value)
    ? value
    : 'result';
}

/** 从 params 移除查看状态，返回可继续保留其他会话查询参数的副本。 */
function clearInspection(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of ['inspect', 'tab', 'command', 'integration', 'wide', 'decision'])
    next.delete(key);
  return next;
}

/** 读取路由运行后，以稳定session_id挂载工作区；新轮导航不会重建对话与输入。 */
export function RunDetailPage() {
  const { runId = '' } = useParams();
  const run = useQuery({
    queryKey: ['run', runId],
    queryFn: /* 路由读取不会启动任务。 */ ({ signal }) => getRun(runId, signal),
    retry: false,
  });
  const sessionId = run.data?.session_id || runId;
  const conversation = useQuery({
    queryKey: ['conversation', sessionId],
    queryFn: /* 使用会话根读取所有轮次，路由变化不换查询身份。 */ ({ signal }) =>
      getConversation(sessionId, signal),
    enabled: !!run.data,
    retry: false,
    refetchInterval: /* 准备、执行与快照占槽时更新继续条件，终态停止轮询。 */ (state) =>
      state.state.data?.active_run_ids.length || state.state.data?.blocked_code === 'run_busy'
        ? 1500
        : false,
  });
  const accessError = run.error || conversation.error;
  const deletedError = accessError as ApiError | null;
  if (deletedError?.details?.session_id && deletedError.code === 'conversation_deleted')
    return <ConversationRecovery id={deletedError.details.session_id} />;
  if (run.isPending || (run.data && conversation.isPending))
    return (
      <div className="session-loading">
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  if (!run.data || !conversation.data)
    return (
      <div className="session-loading">
        <Link to="/runs">返回任务列表</Link>
        <Alert
          type="error"
          title="无法打开会话"
          description={errorMessage(run.error || conversation.error)}
        />
      </div>
    );
  return (
    <SessionWorkspace
      key={sessionId}
      routeRun={run.data}
      conversation={conversation.data}
      refreshError={run.error || conversation.error}
    />
  );
}

/** 以会话组织消息，所选运行只影响检查器；所有追加仍发送给最新轮。 */
function SessionWorkspace({
  routeRun,
  conversation,
  refreshError,
}: {
  routeRun: RunView;
  conversation: ConversationView;
  refreshError: unknown;
}) {
  const cache = useQueryClient();
  const navigate = useNavigate();
  const { key: locationKey, state: routeState } = useLocation();
  const returnTo = conversationListFrom(routeState);
  const [params, setParams] = useSearchParams();
  const narrow = useNarrowWorkspace();
  const requestedSelection = params.get('inspect') || routeRun.run_id;
  const selectedId = conversation.turns.some(
    /* URL只允许选择当前会话中已经接纳的轮次。 */ (turn) => turn.run_id === requestedSelection,
  )
    ? requestedSelection
    : conversation.latest_run_id;
  const tab = inspectorTab(params.get('tab'));
  const panelOpen = !params.has('decision') && (params.has('tab') || params.has('inspect')); // URL 同时用于直达和关闭后刷新。
  const wide = !narrow && panelOpen && tab === 'files' && params.get('wide') === '1'; // 宽屏展开复用同一个差异视图。
  const inspectorHeading = useRef<HTMLDivElement>(null); // 桌面侧栏从标题开始键盘阅读。
  const inspectorToggle = useRef<HTMLButtonElement>(null); // 直接打开深链接时的关闭焦点回退。
  const inspectorTrigger = useRef<HTMLElement | null>(null); // 用户打开详情的入口，用于关闭后恢复焦点。
  const decisionToggle = useRef<HTMLButtonElement>(null); // 队列直达审批关闭时的焦点回退。
  const decisionTrigger = useRef<HTMLElement | null>(null); // 会话内实际打开审批的按钮。
  const [searchOpen, setSearchOpen] = useState(false); // 查找属于当前会话，不进入运行详情路由。
  const searchId = useId(); // 与查找开关建立可访问关联。
  const searchToggle = useRef<HTMLButtonElement>(null); // 关闭后恢复顶栏焦点。
  const confirmedFrom = useRef<string | null>(null); // 已发起新轮跳转的位置键，用于跳过旧地址的异步清理。
  const focusAfterClose = useRef<{ target: HTMLElement; surface: 'detail' | 'decision' } | null>(
    null,
  ); // 只有退出的面板可以恢复自己的焦点。
  const query = useQuery({
    queryKey: ['run', conversation.latest_run_id],
    queryFn: /* 最新轮快照决定运行状态，查看旧轮时继续跟踪最新执行。 */ ({ signal }) =>
      getRun(conversation.latest_run_id, signal),
    initialData: routeRun.run_id === conversation.latest_run_id ? routeRun : undefined,
    retry: false,
    refetchInterval: /* 活动阶段刷新状态与计数；SSE继续负责工具事件。 */ (state) =>
      state.state.data?.active ? 1500 : false,
  });
  const latest = query.data;
  const decisionLabel = pendingDecisionLabel(latest);
  const decisionOpen = Boolean(decisionLabel && params.get('decision') === latest?.run_id);
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 只读当前可选技能，不依据目录重建历史快照。 */ ({ signal }) => getOptions(signal),
    retry: false,
  });
  const events = useRunEvents(
    conversation.latest_run_id,
    latest?.active ?? false,
    /* 终态后核对执行、验收与快照事实。 */ () => {
      void cache.invalidateQueries({ queryKey: ['run', conversation.latest_run_id] });
      void cache.invalidateQueries({ queryKey: ['conversation', conversation.session_id] });

      void cache.invalidateQueries({ queryKey: ['conversations'] });
      void cache.invalidateQueries({ queryKey: ['options'] });
    },
  );
  const cancellation = useMutation({
    mutationFn: /* 只由明确取消操作触发。 */ (id: string) => cancelRun(id),
    onSuccess: /* 取消可能仍在清理，保持后台真实快照。 */ (view) =>
      cache.setQueryData(['run', view.run_id], view),
  });
  useEffect(
    /* 过期队列链接回到会话；确认跳转会自行清理原地址，避免两个 replace 竞争。 */ () => {
      if (
        latest &&
        params.has('decision') &&
        !decisionOpen &&
        confirmedFrom.current !== locationKey
      ) {
        setParams(clearInspection(params), { replace: true, state: routeState });
      }
    },
    [latest, decisionOpen, locationKey, params, routeState, setParams],
  );

  /** 查看 runId 的分类 value；commandCallId 只用于该轮输出定位，不重建对话。 */
  function inspect(runId: string, value = 'result', commandCallId?: string): void {
    if (!panelOpen && document.activeElement instanceof HTMLElement) {
      inspectorTrigger.current = document.activeElement;
    }
    const next = new URLSearchParams(params);
    next.delete('decision');
    if (value !== 'files') next.delete('wide');
    if (runId !== selectedId) next.delete('integration');
    next.set('inspect', runId);
    next.set('tab', value);
    if (value === 'output' && commandCallId) next.set('command', commandCallId);
    else next.delete('command');
    setParams(next, { replace: true, state: routeState });
  }

  /** 关闭详情并清除深链接，随后把焦点还给消息入口。 */
  function closePanel(): void {
    const target = inspectorTrigger.current || inspectorToggle.current;
    focusAfterClose.current = target ? { target, surface: 'detail' } : null;
    inspectorTrigger.current = null;
    setParams(clearInspection(params), { replace: true, state: routeState });
  }

  /** 打开当前运行的决定；trigger 缺省时使用顶栏入口作为关闭后的焦点。 */
  function showDecision(trigger?: HTMLElement): void {
    if (!latest || !decisionLabel) return;
    decisionTrigger.current = trigger ?? null;
    const next = clearInspection(params);
    next.set('decision', latest.run_id);
    setParams(next, { replace: true, state: routeState });
  }

  /** 关闭审批阅读，恢复原入口，保留尚未发送的会话草稿。 */
  function closeDecision(): void {
    const target = decisionTrigger.current || decisionToggle.current;
    focusAfterClose.current = target ? { target, surface: 'decision' } : null;
    decisionTrigger.current = null;
    setParams(clearInspection(params), { replace: true, state: routeState });
  }

  /** 关闭会话内查找并恢复开关焦点，保留命中消息的阅读位置。 */
  function closeSearch(): void {
    setSearchOpen(false);
    searchToggle.current?.focus({ preventScroll: true });
  }

  /** surface 关闭后恢复其焦点；目标已被新一轮移除时无需处理。 */
  function restoreFocus(surface: 'detail' | 'decision'): void {
    const pending = focusAfterClose.current;
    if (pending?.surface !== surface) return;
    focusAfterClose.current = null;
    if (pending.target.isConnected) pending.target.focus({ preventScroll: true });
  }

  useLayoutEffect(
    /* 桌面侧栏即时卸载；窄屏抽屉等退出完成再移动焦点。 */ () => {
      if (narrow) return;
      if (panelOpen) inspectorHeading.current?.focus({ preventScroll: true });
      else restoreFocus('detail');
    },
    [narrow, panelOpen],
  );

  /** 用POST已确认的真实视图补入会话后再刷新；保留同一DOM与输入状态。 */
  function confirmed(view: RunView): void {
    confirmedFrom.current = locationKey;
    cache.setQueryData(['run', view.run_id], view);
    cache.setQueryData<ConversationView>(
      ['conversation', conversation.session_id],
      /* 已确认的新运行并入对应用户轮，重复确认不重复插入。 */ (current) =>
        current ? mergeConfirmedRun(current, view) : current,
    );
    void cache.invalidateQueries({ queryKey: ['conversation', conversation.session_id] });

    void cache.invalidateQueries({ queryKey: ['conversations'] });
    void cache.invalidateQueries({ queryKey: ['options'] });
    void navigate('/runs/' + view.run_id, { replace: true, state: routeState });
  }
  const selectedIndex = conversation.turns.findIndex(
    /* 标题使用所选轮次，旧测试和新运行分别展示。 */ (turn) => turn.run_id === selectedId,
  );
  const selectedTurn = conversation.turns[selectedIndex];
  const detailTitle = '第 ' + (selectedTurn?.turn_index || '—') + ' 轮详情';
  const previous = conversation.turns[selectedIndex - 1];
  const next = selectedIndex < 0 ? undefined : conversation.turns[selectedIndex + 1];
  const turnNavigation = conversation.turns.length > 1 && (
    <div className="inspector-turn-navigation" aria-label="切换查看轮次">
      <Button
        type="text"
        size="small"
        icon={<LeftOutlined />}
        aria-label="上一轮"
        title="上一轮"
        disabled={!previous}
        onClick={
          /* 保持所选分类，清除旧轮的命令定位。 */ () => previous && inspect(previous.run_id, tab)
        }
      />
      <Button
        type="text"
        size="small"
        icon={<RightOutlined />}
        aria-label="下一轮"
        title="下一轮"
        disabled={!next}
        onClick={/* 只移动详情，不改变执行目标。 */ () => next && inspect(next.run_id, tab)}
      />
    </div>
  );
  const title = conversation.title; // 所有入口共享后端有效标题，改名不改原消息。
  const inspector = latest ? (
    <RunInspector
      selectedId={selectedId}
      latest={latest}
      liveEvents={events}
      tab={tab}
      commandCallId={tab === 'output' ? params.get('command') || undefined : undefined}
      onTab={/* 分类切换保持所选轮与会话。 */ (value) => inspect(selectedId, value)}
      readOnly={conversation.management.state !== 'active'}
      onShowDecision={/* 打开同一个决定抽屉，详情视图退出。 */ () => showDecision()}
    />
  ) : null;
  return (
    <div className="session-workspace">
      <header className="session-header">
        <Link
          to={
            returnTo ||
            (conversation.management.assigned_project?.available
              ? '/projects/' + encodeURIComponent(conversation.management.assigned_project.id)
              : '/runs?view=history')
          }
          className="session-back"
          aria-label={
            (
              returnTo
                ? returnTo.startsWith('/projects/')
                : conversation.management.assigned_project?.available
            )
              ? '返回项目会话'
              : '返回全部会话'
          }
        >
          <ArrowLeftOutlined />
        </Link>
        <div className="session-heading">
          <h1 title={title}>{title}</h1>
          {latest?.automation && <Link to="/automations">来自自动化 · 查看规则与触发历史</Link>}
          <p>
            {conversation.management.assigned_project?.label || '未分组'}
            <span>·</span>
            {conversation.turns.length} 轮对话
          </p>
          {conversation.management.assigned_project_id !== conversation.source?.id &&
            conversation.source && (
              <p className="session-directory">
                执行来源：{conversation.source.label}，继续发送仍使用原工作目录和记忆。
              </p>
            )}
        </div>
        <div className="session-header-actions">
          <Button
            ref={searchToggle}
            type="text"
            size="small"
            icon={<SearchOutlined />}
            title="查找会话消息"
            aria-label="查找会话消息"
            aria-expanded={searchOpen}
            aria-controls={searchOpen ? searchId : undefined}
            onClick={
              /* 打开查找后自动聚焦输入，关闭时回到开关。 */ () =>
                searchOpen ? closeSearch() : setSearchOpen(true)
            }
          />
          <ConversationActions
            value={conversation.management}
            onChanged={
              /* 当前对话删除后离开消息页；移动及命名保留草稿。 */ (updated) => {
                if (['trashed', 'purging', 'purged'].includes(updated.state))
                  void navigate('/runs?view=history&state=trashed');
              }
            }
          />
          {decisionLabel ? (
            <Button
              ref={decisionToggle}
              size="small"
              icon={<AuditOutlined />}
              onClick={
                /* 打开阅读区后还需要用户明确做决定。 */ (event) =>
                  showDecision(event.currentTarget)
              }
            >
              {decisionLabel}
            </Button>
          ) : (
            latest && <RunStatus run={latest} />
          )}
          <Button
            ref={inspectorToggle}
            type="text"
            aria-label={panelOpen ? '收起详情' : '查看详情'}
            title={panelOpen ? '收起详情' : '查看详情'}
            aria-expanded={panelOpen}
            onClick={
              /* 查看区域独立于会话输入与执行。 */ (event) => {
                if (panelOpen) closePanel();
                else {
                  inspectorTrigger.current = event.currentTarget;
                  inspect(selectedId);
                }
              }
            }
            icon={<LayoutOutlined />}
          />
        </div>
      </header>
      {conversation.management.state === 'archived' && (
        <Alert type="info" title="对话已归档；取消归档后可继续发送或处理计划。" />
      )}
      {(refreshError || query.error || cancellation.error) && (
        <Alert
          className="session-refresh-warning"
          type="warning"
          showIcon
          title={cancellation.error ? '取消请求未完成' : '暂时无法刷新，保留已读取内容'}
          description={errorMessage(cancellation.error || refreshError || query.error)}
        />
      )}
      <div
        className={
          'session-body' +
          (!narrow && panelOpen ? ' with-inspector' : '') +
          (wide ? ' with-wide-inspector' : '')
        }
      >
        <ConversationPanel
          run={latest}
          conversation={conversation}
          events={events.timeline}
          skills={options.data?.skills || []}
          mcpTools={options.data?.mcp_tools || []}
          onInspect={inspect}
          decisionOpen={decisionOpen}
          onShowDecision={showDecision}
          onCloseDecision={closeDecision}
          onDecisionClosed={/* 审批退出后回到它的入口。 */ () => restoreFocus('decision')}
          searchId={searchOpen ? searchId : undefined}
          onCloseSearch={closeSearch}
          onConfirmed={confirmed}
          onStop={
            /* 用户明确停止当前轮，草稿与已有输出保持可读。 */ () =>
              latest && cancellation.mutate(latest.run_id)
          }
          stopping={Boolean(latest?.cancelling || cancellation.isPending)}
        />
        {!narrow && panelOpen && (
          <aside
            className="session-inspector"
            aria-label="运行详情"
            onKeyDown={
              /* 子菜单可先消费 Escape，侧栏自身再响应。 */ (event) => {
                if (event.key === 'Escape' && !event.defaultPrevented) closePanel();
              }
            }
          >
            <div className="inspector-heading" ref={inspectorHeading} tabIndex={-1}>
              <strong>{detailTitle}</strong>
              {turnNavigation}
              {tab === 'files' && (
                <Button
                  type="text"
                  size="small"
                  aria-label={wide ? '收起宽差异视图' : '展开宽差异视图'}
                  title={wide ? '收起宽差异视图' : '展开宽差异视图'}
                  aria-pressed={wide}
                  icon={wide ? <CompressOutlined /> : <ExpandOutlined />}
                  onClick={
                    /* 保留同一文件选择和聊天草稿，只调整详情宽度。 */ () => {
                      const next = new URLSearchParams(params);
                      if (wide) next.delete('wide');
                      else next.set('wide', '1');
                      setParams(next, { replace: true, state: routeState });
                    }
                  }
                />
              )}
              <button
                type="button"
                aria-label="关闭详情"
                onClick={/* 收起检查器不影响消息阅读。 */ () => closePanel()}
              >
                <CloseOutlined />
              </button>
            </div>
            {inspector}
          </aside>
        )}
      </div>
      {narrow && (
        <Drawer
          title={
            <div className="inspector-drawer-heading">
              <span>{detailTitle}</span>
              {turnNavigation}
            </div>
          }
          className="session-drawer"
          open={panelOpen}
          focusable={{ focusTriggerAfterClose: false }}
          onClose={/* 关闭抽屉回到原消息位置。 */ () => closePanel()}
          afterOpenChange={
            /* 等待遮罩释放焦点后再定位对话。 */ (open) => {
              if (!open) restoreFocus('detail');
            }
          }
          styles={{
            wrapper: { width: tab === 'files' ? '100vw' : 'min(100vw, 420px)' },
            body: { padding: 0 },
          }}
        >
          {inspector}
        </Drawer>
      )}
    </div>
  );
}
