import { Alert, Empty, Skeleton, Tag } from 'antd';
import { Link } from 'react-router';
import type { ConversationSummary } from '../types';
import { dateTime } from '../format';
import { RunStatus } from './RunStatus';

/** 由最新轮精简状态复用已有展示规则，关联异常时不把旧轮成功当成当前成功。 */
export function ConversationStatus({ item }: { item: ConversationSummary }) {
  if (item.error || !item.latest) return <Tag color="warning">记录待核对</Tag>;
  const state = item.latest;
  return <RunStatus run={{ ...state, project: item.source?.kind === 'project', result: state }} />;
}

/** 每会话一个可进入的条目；只渲染后端聚合结果，不对当前页运行做本地去重。 */
export function ConversationList({
  items,
  loading = false,
  error = null,
  showProject = true,
}: {
  items: ConversationSummary[]; // 后端已分页且每会话唯一的摘要。
  loading?: boolean; // 首次读取时展示占位，不隐藏后续刷新时已有条目。
  error?: string | null; // 刷新失败保留已有列表并显示原因。
  showProject?: boolean; // 项目页省略重复项目名，全部会话显示来源。
}) {
  return (
    <div className="conversation-list">
      {error && <Alert type="error" title="无法刷新会话" description={error} />}
      {loading && !items.length ? (
        <Skeleton active />
      ) : !items.length ? (
        <Empty description="暂无会话" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <ul className="start-run-list">
          {items.map(
            /* 稳定会话键使追加轮次不会多出导航条目。 */ (item) => (
              <li key={item.session_id}>
                <Link
                  to={'/runs/' + item.entry_run_id}
                  aria-label={'继续对话：' + item.title}
                  title={item.error || undefined}
                >
                  <div className="start-run-title">
                    <strong>{item.title}</strong>
                    <small>
                      {showProject && <>{item.source?.label || '未关联项目'} · </>}
                      {item.turn_count === null ? '轮次待核对' : item.turn_count + ' 轮对话'}
                    </small>
                  </div>
                  <div className="conversation-row-meta">
                    <ConversationStatus item={item} />
                    <time dateTime={item.updated_at}>{dateTime(item.updated_at)}</time>
                  </div>
                </Link>
              </li>
            ),
          )}
        </ul>
      )}
    </div>
  );
}
