import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Drawer, Skeleton } from 'antd';
import {
  ArrowLeftOutlined,
  CloseOutlined,
  LayoutOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import type { ApiError } from '../api';
import { cancelRun, errorMessage, getConversation, getOptions, getRun } from '../api';
import { ConversationActions } from '../components/ConversationActions';
import { ConversationRecovery } from '../components/ConversationRecovery';
import { useRunEvents } from '../hooks/useRunEvents';
import { mergeConfirmedRun } from '../conversation';
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
    ['tasks', 'plan', 'agents', 'result', 'output', 'files', 'diagnostics'].includes(value)
    ? value
    : 'result';
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
  const [params, setParams] = useSearchParams();
  const narrow = useNarrowWorkspace();
  const requestedSelection = params.get('inspect') || routeRun.run_id;
  const selectedId = conversation.turns.some(
    /* URL只允许选择当前会话中已经接纳的轮次。 */ (turn) => turn.run_id === requestedSelection,
  )
    ? requestedSelection
    : conversation.latest_run_id;
  const tab = inspectorTab(params.get('tab'));
  const [panelOpen, setPanelOpen] = useState(params.has('tab') || params.has('inspect')); // 普通入口优先阅读消息；显式详情深链接保留查看意图。
  const previousNarrow = useRef(narrow); // 只在真正跨断点时收起详情，不覆盖首屏深链接。
  const inspectorHeading = useRef<HTMLDivElement>(null); // 桌面侧栏从标题开始键盘阅读。
  const inspectorToggle = useRef<HTMLButtonElement>(null); // 直接打开深链接时的关闭焦点回退。
  const inspectorTrigger = useRef<HTMLElement | null>(null); // 用户打开详情的入口，用于关闭后恢复焦点。
  const decision = useRef<HTMLDivElement>(null); // 当前轮待批准或待回答的唯一操作区。
  const focusAfterClose = useRef<{ target: HTMLElement; reveal: boolean } | null>(null); // 抽屉退出动画后再恢复或定位。
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
    /* 跨屏幕断点收起详情，让消息优先；初次挂载保留URL的详情意图。 */ () => {
      if (previousNarrow.current === narrow) return;
      previousNarrow.current = narrow;
      setPanelOpen(false);
    },
    [narrow],
  );

  /** 查看 runId 的分类 value；commandCallId 只用于该轮输出定位，不重建对话。 */
  function inspect(runId: string, value = 'result', commandCallId?: string): void {
    if (!panelOpen && document.activeElement instanceof HTMLElement) {
      inspectorTrigger.current = document.activeElement;
    }
    const next = new URLSearchParams(params);
    next.set('inspect', runId);
    next.set('tab', value);
    if (value === 'output' && commandCallId) next.set('command', commandCallId);
    else next.delete('command');
    setParams(next, { replace: true });
    setPanelOpen(true);
  }

  /** 关闭详情并记录下一焦点；审批跳转显式传入对话目标，其余关闭返回触发按钮。 */
  function closePanel(
    target = inspectorTrigger.current || inspectorToggle.current,
    reveal = false,
  ): void {
    focusAfterClose.current = target ? { target, reveal } : null;
    setPanelOpen(false);
  }

  /** 关闭动画结束后恢复焦点；目标已被新一轮移除时无需处理。 */
  function restoreFocus(): void {
    const pending = focusAfterClose.current;
    focusAfterClose.current = null;
    if (!pending?.target.isConnected) return;
    pending.target.focus({ preventScroll: true });
    if (pending.reveal) pending.target.scrollIntoView?.({ block: 'nearest' });
  }

  useLayoutEffect(
    /* 桌面侧栏即时卸载；窄屏抽屉等退出完成再移动焦点。 */ () => {
      if (narrow) return;
      if (panelOpen) inspectorHeading.current?.focus({ preventScroll: true });
      else restoreFocus();
    },
    [narrow, panelOpen],
  );

  /** 用POST已确认的真实视图补入会话后再刷新；保留同一DOM与输入状态。 */
  function confirmed(view: RunView): void {
    cache.setQueryData(['run', view.run_id], view);
    cache.setQueryData<ConversationView>(
      ['conversation', conversation.session_id],
      /* 已确认的新运行并入对应用户轮，重复确认不重复插入。 */ (current) =>
        current ? mergeConfirmedRun(current, view) : current,
    );
    void cache.invalidateQueries({ queryKey: ['conversation', conversation.session_id] });

    void cache.invalidateQueries({ queryKey: ['conversations'] });
    void cache.invalidateQueries({ queryKey: ['options'] });
    void navigate(`/runs/${view.run_id}`, { replace: true });
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
      onShowDecision={
        /* 跳到对话中当前待处理决定，关闭窄屏遮罩。 */ () => closePanel(decision.current, true)
      }
    />
  ) : null;
  return (
    <div className="session-workspace">
      <header className="session-header">
        <Link
          to={
            conversation.management.assigned_project?.available
              ? '/projects/' + encodeURIComponent(conversation.management.assigned_project.id)
              : '/runs?view=history'
          }
          className="session-back"
          aria-label={
            conversation.management.assigned_project?.available ? '返回项目会话' : '返回全部会话'
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
          {latest?.execution_mode && (
            <p className="session-directory" title={latest.workspace}>
              <span>{latest.execution_mode === 'local' ? '所选目录' : '独立 Git 工作树'}</span>
              <code>{latest.workspace}</code>
            </p>
          )}
          {conversation.management.assigned_project_id !== conversation.source?.id &&
            conversation.source && (
              <p className="session-directory">
                执行来源：{conversation.source.label}，继续发送仍使用原工作目录和记忆。
              </p>
            )}
        </div>
        <div className="session-header-actions">
          <ConversationActions
            value={conversation.management}
            onChanged={
              /* 当前对话删除后离开消息页；移动及命名保留草稿。 */ (updated) => {
                if (['trashed', 'purging', 'purged'].includes(updated.state))
                  void navigate('/runs?view=history&state=trashed');
              }
            }
          />
          {latest && <RunStatus run={latest} />}
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
                  setPanelOpen(true);
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
      <div className={`session-body ${!narrow && panelOpen ? 'with-inspector' : ''}`}>
        <ConversationPanel
          run={latest}
          conversation={conversation}
          events={events.timeline}
          skills={options.data?.skills || []}
          mcpTools={options.data?.mcp_tools || []}
          onInspect={inspect}
          decisionRef={decision}
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
              if (!open) restoreFocus();
            }
          }
          styles={{ wrapper: { width: 'min(100vw, 420px)' }, body: { padding: 0 } }}
        >
          {inspector}
        </Drawer>
      )}
    </div>
  );
}
