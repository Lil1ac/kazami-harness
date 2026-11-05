import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Skeleton } from 'antd';
import { ReloadOutlined, PlusOutlined } from '@ant-design/icons';
import { Link, useSearchParams } from 'react-router';
import { errorMessage, getOptions, getConversations } from '../api';
import { TaskForm } from '../components/TaskForm';
import { ConversationList } from '../components/ConversationList';
import { ConversationHistory } from '../components/ConversationHistory';

/** 新建与历史分别展示；最近入口按会话而非每轮运行组织。 */
export function RunsPage() {
  const [params] = useSearchParams();
  const cache = useQueryClient(); // 刷新当前可见会话查询，覆盖全局与项目列表。
  const history = params.get('view') === 'history'; // 原历史URL继续可访问，但内容按会话聚合。
  const projectId = params.get('project') || undefined; // 项目页新建入口固定来源，覆盖上次浏览器选择。
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 读取不会打开窗口或执行任务。 */ ({ signal }) => getOptions(signal),
    staleTime: 15000,
  });
  const recent = useQuery({
    queryKey: ['conversations', { limit: 5 }],
    queryFn: /* 会话完整聚合后取最近五条。 */ ({ signal }) =>
      getConversations({ limit: 5 }, signal),
    refetchInterval: 5000,
    enabled: !history,
  });
  /** 刷新项目与所有可见会话；无参数和返回值，不改写输入或发起执行。 */
  function refresh(): void {
    void options.refetch();
    void cache.invalidateQueries({ queryKey: ['conversations'] });
  }
  return (
    <div className={history ? 'history-workspace' : 'start-workspace'}>
      {history ? (
        <>
          <header className="project-workspace-header">
            <h1>全部会话</h1>
            <div className="workspace-actions">
              <Button
                type="text"
                aria-label="刷新工作台"
                title="刷新"
                icon={<ReloadOutlined />}
                onClick={refresh}
              />
              <Link to="/runs?new=1" className="new-conversation-link" aria-label="新建对话">
                <PlusOutlined /> 新建对话
              </Link>
            </div>
          </header>
          <ConversationHistory />
        </>
      ) : (
        <>
          {options.error && (
            <Alert
              type="error"
              title={options.data ? '项目列表刷新失败，已保留当前输入' : '无法读取项目'}
              description={errorMessage(options.error)}
            />
          )}
          {options.data ? (
            <TaskForm
              key={projectId || 'new'}
              options={options.data}
              activeRunIds={recent.data ? recent.data.active_run_ids : options.data.active_run_ids}
              initialProjectId={projectId}
            />
          ) : !options.error ? (
            <Skeleton active className="start-loading" />
          ) : null}
          <section
            className="task-history recent-history"
            aria-labelledby="recent-conversations-heading"
          >
            <div className="section-heading">
              <h2 id="recent-conversations-heading">最近会话</h2>
              <div className="workspace-actions">
                <Button
                  type="text"
                  size="small"
                  aria-label="刷新工作台"
                  title="刷新"
                  icon={<ReloadOutlined />}
                  onClick={refresh}
                />
                <Link to="/runs?view=history">查看全部</Link>
              </div>
            </div>
            <ConversationList
              items={recent.data?.items || []}
              loading={recent.isPending}
              error={recent.error ? errorMessage(recent.error) : null}
            />
          </section>
        </>
      )}
    </div>
  );
}
