import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Drawer, Popconfirm, Skeleton } from 'antd';
import { ArrowLeftOutlined, CloseOutlined, LayoutOutlined, StopOutlined } from '@ant-design/icons';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { cancelRun, errorMessage, getConversation, getRun } from '../api';
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

/** 兼容旧详情标签；对话始终保留，分类只用于右侧检查器。 */
function inspectorTab(value: string | null): string {
  return value && ['code', 'trace', 'context', 'artifacts'].includes(value) ? value : 'overview';
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
      state.state.data?.busy_run_id || state.state.data?.blocked_code === 'run_busy' ? 1500 : false,
  });
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
  const [panelOpen, setPanelOpen] = useState(
    !narrow || ['code', 'trace', 'context', 'artifacts'].includes(params.get('tab') || ''),
  ); // 手机普通入口收起，详情深链接直接打开抽屉。
  const previousNarrow = useRef(narrow); // 只在真正跨断点时收起抽屉，不覆盖首屏深链接。
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
  const events = useRunEvents(
    conversation.latest_run_id,
    latest?.active ?? false,
    /* 终态后核对执行、验收与快照事实。 */ () => {
      void cache.invalidateQueries({ queryKey: ['run', conversation.latest_run_id] });
      void cache.invalidateQueries({ queryKey: ['conversation', conversation.session_id] });
      void cache.invalidateQueries({ queryKey: ['runs'] });
      void cache.invalidateQueries({ queryKey: ['options'] });
    },
  );
  const cancellation = useMutation({
    mutationFn: /* 只由明确取消操作触发。 */ (id: string) => cancelRun(id),
    onSuccess: /* 取消可能仍在清理，保持后台真实快照。 */ (view) =>
      cache.setQueryData(['run', view.run_id], view),
  });
  useEffect(
    /* 跨屏幕断点才调整面板，初次挂载保留URL的详情意图。 */ () => {
      if (previousNarrow.current === narrow) return;
      previousNarrow.current = narrow;
      setPanelOpen(!narrow);
    },
    [narrow],
  );

  /** 查看目标轮和分类，只更新URL检查状态，不切换或重建对话。 */
  function inspect(runId: string, value = 'overview'): void {
    const next = new URLSearchParams(params);
    next.set('inspect', runId);
    next.set('tab', value);
    setParams(next, { replace: true });
    setPanelOpen(true);
  }

  /** 用POST已确认的真实视图补入会话后再刷新；保留同一DOM与输入状态。 */
  function confirmed(view: RunView): void {
    cache.setQueryData(['run', view.run_id], view);
    cache.setQueryData<ConversationView>(
      ['conversation', conversation.session_id],
      /* 已确认的新运行并入对应用户轮，重复确认不重复插入。 */ (current) =>
        current ? mergeConfirmedRun(current, view) : current,
    );
    void cache.invalidateQueries({ queryKey: ['conversation', conversation.session_id] });
    void cache.invalidateQueries({ queryKey: ['runs'] });
    void cache.invalidateQueries({ queryKey: ['options'] });
    void navigate(`/runs/${view.run_id}`, { replace: true });
  }
  const selectedTurn = conversation.turns.find(
    /* 标题使用所选轮次，旧测试和新运行分别展示。 */ (turn) => turn.run_id === selectedId,
  );
  const detailTitle = '第 ' + (selectedTurn?.turn_index || '—') + ' 轮详情';
  const title = conversation.turns[0]?.prompt.replace(/\s+/g, ' ').trim() || '任务会话';
  const inspector = latest ? (
    <RunInspector
      selectedId={selectedId}
      latest={latest}
      liveEvents={events}
      tab={tab}
      onTab={/* 分类切换保持所选轮与会话。 */ (value) => inspect(selectedId, value)}
    />
  ) : null;
  return (
    <div className="session-workspace">
      <header className="session-header">
        <Link to="/runs" className="session-back" aria-label="返回任务列表">
          <ArrowLeftOutlined />
        </Link>
        <div className="session-heading">
          <h1 title={title}>{title}</h1>
          <p>
            {latest?.project?.project_label || '文件任务'}
            <span>·</span>
            {conversation.turns.length} 轮对话
          </p>
        </div>
        <div className="session-header-actions">
          {latest && <RunStatus run={latest} />}
          {latest?.active && (
            <Popconfirm
              title="停止当前执行？"
              description="已有修改与记录保留，后端会完成进程清理。"
              okText="停止执行"
              cancelText="继续运行"
              onConfirm={/* 明确确认才中断最新轮。 */ () => cancellation.mutate(latest.run_id)}
            >
              <Button
                danger
                aria-label="停止当前执行"
                icon={<StopOutlined />}
                loading={latest.cancelling || cancellation.isPending}
                disabled={latest.cancelling || cancellation.isPending}
              >
                停止
              </Button>
            </Popconfirm>
          )}
          <Button
            type="text"
            aria-label={panelOpen ? '收起详情' : '查看详情'}
            onClick={/* 查看区域独立于会话输入与执行。 */ () => setPanelOpen(!panelOpen)}
            icon={<LayoutOutlined />}
          />
        </div>
      </header>
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
          events={events.events}
          onInspect={inspect}
          onConfirmed={confirmed}
        />
        {!narrow && panelOpen && (
          <aside className="session-inspector" aria-label="运行详情">
            <div className="inspector-heading">
              <strong>{detailTitle}</strong>
              <button
                type="button"
                aria-label="关闭详情"
                onClick={/* 收起检查器不影响消息阅读。 */ () => setPanelOpen(false)}
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
          title={detailTitle}
          className="session-drawer"
          open={panelOpen}
          onClose={/* 关闭抽屉回到原消息位置。 */ () => setPanelOpen(false)}
          styles={{ wrapper: { width: 'min(100vw, 420px)' }, body: { padding: 0 } }}
        >
          {inspector}
        </Drawer>
      )}
    </div>
  );
}
