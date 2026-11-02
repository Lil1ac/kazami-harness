import { useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Skeleton } from 'antd';
import { Link, useParams } from 'react-router';
import { errorMessage, getConversation, getConversationManagement, getRun } from '../api';
import { ConversationActions } from './ConversationActions';

/** 已删除会话的旧链接入口；id来自后端受控错误，恢复后重读原运行，不自动发送消息。 */
export function ConversationRecovery({ id }: { id: string }) {
  const cache = useQueryClient();
  const { runId } = useParams(); // 恢复原链接所指轮次，不把旧轮链接替换为最新轮。
  const query = useQuery({
    queryKey: ['conversation-management', id],
    queryFn: /* 只读最小管理信息。 */ () => getConversationManagement(id),
    refetchInterval: 5000,
  });
  const reopen = useMutation({
    mutationFn: /* 恢复后显式重读两份正文，旧410错误不能阻止被禁用查询重新启用。 */ async () => {
      const [run, conversation] = await Promise.all([getRun(runId || id), getConversation(id)]);
      return { run, conversation };
    },
    onSuccess: /* 两份可信结果到齐后清除各自旧错误；不创建或重放任何运行。 */ ({
      run,
      conversation,
    }) => {
      cache.setQueryData(['conversation', id], conversation);
      cache.setQueryData(['run', runId || id], run);
    },
  });
  useEffect(
    /* 同窗口或其他窗口恢复都重新读取原链接；状态不变时不重复请求。 */ () => {
      if (query.data?.state === 'active' || query.data?.state === 'archived') reopen.mutate();
    },
    [query.data?.state, id, runId],
  );
  if (query.isPending) return <Skeleton active />;
  return (
    <div className="session-loading">
      <Link to="/runs?view=history&state=trashed">返回回收站</Link>
      {query.error && <Alert type="error" title={errorMessage(query.error)} />}
      {reopen.error && (
        <Alert
          type="error"
          title={errorMessage(reopen.error)}
          action={
            <Button onClick={/* 读取失败允许明确重试，不重复恢复操作。 */ () => reopen.mutate()}>
              重新读取
            </Button>
          }
        />
      )}
      {query.data && (
        <>
          <h1>{query.data.title}</h1>
          <Alert
            type="info"
            title={
              query.data.state === 'purged'
                ? '对话已彻底删除'
                : query.data.state === 'active' || query.data.state === 'archived'
                  ? '已恢复，正在重新读取对话'
                  : query.data.state === 'purging'
                    ? '对话正在清理，记录暂不可访问'
                    : '对话已在回收站'
            }
            description={
              query.data.state === 'purged'
                ? '会话记录已清理；另存项目记忆仍由项目独立管理。'
                : (query.data.state === 'trashed' || query.data.state === 'purging'
                    ? query.data.purge_blocked_reason
                    : null) || '恢复后可重新查看消息，并按原执行来源核验是否可以继续。'
            }
          />
          {['trashed', 'purging'].includes(query.data.state) && (
            <ConversationActions value={query.data} />
          )}
        </>
      )}
    </div>
  );
}
