import { useQuery } from '@tanstack/react-query';
import { Pagination, Select } from 'antd';
import { useSearchParams } from 'react-router';
import { errorMessage, getConversations } from '../api';
import { ConversationList } from './ConversationList';

/** 全局与项目共用会话分页；筛选在后端聚合后执行，URL保留页码与最新轮状态。 */
export function ConversationHistory({ projectId }: { projectId?: string }) {
  const [params, setParams] = useSearchParams();
  const parsed = Number(params.get('page'));
  const page = Number.isInteger(parsed) && parsed > 0 ? parsed : 1; // 非法页码回到第一页。
  const status = params.get('status') || ''; // 按最新轮执行状态筛选，不匹配任意旧轮。
  const query = useQuery({
    queryKey: ['conversations', { projectId, offset: (page - 1) * 12, limit: 12, status }],
    queryFn: /* 不创建会话，也不重放历史工具。 */ ({ signal }) =>
      getConversations({ projectId, offset: (page - 1) * 12, limit: 12, status }, signal),
    refetchInterval: 5000,
  });
  /** 保存下一页和状态；保留项目路由与view参数，不发起执行。 */
  function change(nextPage: number, nextStatus: string): void {
    const next = new URLSearchParams(params);
    next.delete('page');
    next.delete('status');
    if (nextPage > 1) next.set('page', String(nextPage));
    if (nextStatus) next.set('status', nextStatus);
    setParams(next);
  }
  return (
    <section
      className="conversation-history"
      aria-label={projectId ? '项目会话列表' : '全部会话列表'}
    >
      <div className="section-heading">
        <h2>
          会话 <small>{query.data?.total ?? '—'}</small>
        </h2>
        <Select
          aria-label="筛选会话状态"
          value={status}
          onChange={/* 状态切换从第一页开始。 */ (value) => change(1, value)}
          options={[
            { value: '', label: '全部状态' },
            { value: 'running', label: '执行中' },
            { value: 'waiting_input', label: '等待你的决定' },
            { value: 'completed', label: '执行完成' },
            { value: 'failed', label: '执行失败' },
            { value: 'cancelled', label: '已取消' },
            { value: 'timed_out', label: '已超时' },
            { value: 'limit_exceeded', label: '预算耗尽' },
          ]}
        />
      </div>
      <ConversationList
        items={query.data?.items || []}
        loading={query.isPending}
        error={query.error ? errorMessage(query.error) : null}
        showProject={!projectId}
      />
      {(query.data?.total || 0) > 12 && (
        <Pagination
          current={page}
          pageSize={12}
          total={query.data?.total || 0}
          showSizeChanger={false}
          onChange={/* 分页仍为只读导航。 */ (value) => change(value, status)}
        />
      )}
    </section>
  );
}
