import { Alert, Checkbox, Empty, Skeleton, Tag } from 'antd';
import { PushpinOutlined } from '@ant-design/icons';
import { Link } from 'react-router';
import type { ConversationSummary } from '../types';
import { dateTime } from '../format';
import { RunStatus } from './RunStatus';
import { ConversationActions } from './ConversationActions';

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
  selected,
  onSelect,
}: {
  items: ConversationSummary[]; // 后端已分页且每会话唯一的摘要。
  loading?: boolean; // 首次读取时展示占位，不隐藏后续刷新时已有条目。
  error?: string | null; // 刷新失败保留已有列表并显示原因。
  showProject?: boolean; // 项目页省略重复项目名，全部会话显示当前整理归属。
  selected?: Set<string>; // 当前页批量选择；未提供时不显示复选框。
  onSelect?: (id: string, checked: boolean) => void; // 只改变选择，不发起任务或管理请求。
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
              <li key={item.session_id} className="conversation-list-row">
                {onSelect && (
                  <Checkbox
                    aria-label={'选择对话：' + item.title}
                    checked={selected?.has(item.session_id)}
                    disabled={item.management.state === 'purging'}
                    onChange={
                      /* 选择严格限定为当前会话。 */ (event) =>
                        onSelect(item.session_id, event.target.checked)
                    }
                  />
                )}
                <Link
                  to={
                    ['trashed', 'purging'].includes(item.management.state)
                      ? '/runs?view=history&state=trashed'
                      : '/runs/' + item.entry_run_id
                  }
                  aria-label={'继续对话：' + item.title}
                  title={item.error || undefined}
                >
                  <div className="start-run-title">
                    <strong>
                      {item.management.pinned_at && <PushpinOutlined aria-label="已置顶" />}{' '}
                      {item.title}
                    </strong>
                    <small>
                      {showProject && <>{item.management.assigned_project?.label || '未分组'} · </>}
                      {item.turn_count === null ? '轮次待核对' : item.turn_count + ' 轮对话'}
                    </small>
                  </div>
                  <div className="conversation-row-meta">
                    <ConversationStatus item={item} />
                    <time dateTime={item.updated_at}>{dateTime(item.updated_at)}</time>
                    {item.management.deleted_at && (
                      <small>删除于 {dateTime(item.management.deleted_at)}</small>
                    )}
                  </div>
                </Link>
                <ConversationActions value={item.management} />
              </li>
            ),
          )}
        </ul>
      )}
    </div>
  );
}
