import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Empty, Pagination, Select, Skeleton } from 'antd';
import { ArrowUpOutlined, ReloadOutlined } from '@ant-design/icons';
import { Link, useSearchParams } from 'react-router';
import { errorMessage, getOptions, getRuns } from '../api';
import { dateTime } from '../format';
import { RunStatus } from '../components/RunStatus';
import { TaskForm } from '../components/TaskForm';

/** 任务历史与新任务入口；分页/筛选保留在 URL，刷新后可以恢复视图。 */
export function RunsPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get('status') || '';
  const page = Math.max(1, Number(params.get('page')) || 1);
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* Query 的 signal 只取消读取。 */ ({ signal }) => getOptions(signal),
    staleTime: 15000,
  });
  const runs = useQuery({
    queryKey: ['runs', page, status],
    queryFn: /* 只读取当前页，不将所有历史放入浏览器。 */ ({ signal }) =>
      getRuns((page - 1) * 12, status, signal),
    refetchInterval: 4000,
  });

  /** 更新 URL 查询项；切换筛选时调用方把分页重置到第一页。 */
  function changeView(nextPage: number, nextStatus: string): void {
    const next = new URLSearchParams();
    if (nextPage > 1) next.set('page', String(nextPage));
    if (nextStatus) next.set('status', nextStatus);
    setParams(next);
  }

  return (
    <>
      <header className="page-heading">
        <div>
          <span className="eyebrow">WORKSPACE / RUNS</span>
          <h1>
            任务工作台
            <span className="title-dot" />
          </h1>
          <p className="muted">提交任务，沿着真实执行轨迹检查结果。</p>
        </div>
        <Button
          icon={<ReloadOutlined />}
          onClick={
            /* 同步刷新列表与宿主槽位。 */ () => {
              void runs.refetch();
              void options.refetch();
            }
          }
        >
          刷新
        </Button>
      </header>
      <div className="runs-layout">
        <section className="runs-history" aria-labelledby="history-heading">
          <div className="section-heading history-toolbar">
            <h2 id="history-heading">
              运行记录 <small>{runs.data?.total ?? '—'}</small>
            </h2>
            <Select
              aria-label="筛选执行状态"
              value={status}
              style={{ width: 146 }}
              onChange={/* 过滤变化回到第一页，防止空页。 */ (value) => changeView(1, value)}
              options={[
                { value: '', label: '全部执行状态' },
                { value: 'running', label: '执行中' },
                { value: 'completed', label: '执行完成' },
                { value: 'failed', label: '执行失败' },
                { value: 'cancelled', label: '已取消' },
                { value: 'timed_out', label: '已超时' },
                { value: 'limit_exceeded', label: '预算耗尽' },
              ]}
            />
          </div>
          {runs.error && (
            <Alert
              type="error"
              showIcon
              title="无法读取运行记录"
              description={errorMessage(runs.error)}
            />
          )}
          {runs.isPending ? (
            <Skeleton active paragraph={{ rows: 7 }} />
          ) : !runs.data?.items.length ? (
            <div className="history-empty">
              <Empty description="还没有运行记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              <p>从右侧提交第一个任务，或填充日志样例。</p>
            </div>
          ) : (
            <>
              <div className="run-table-head">
                <span>任务 / 工作区</span>
                <span>结果与状态</span>
                <span>调用</span>
              </div>
              <ul className="run-list">
                {runs.data.items.map(
                  /* 整行是可聚焦链接，支持键盘与新标签打开。 */ (run) => (
                    <li key={run.run_id}>
                      <Link to={`/runs/${run.run_id}`} className="run-row">
                        <div className="run-description">
                          <strong>{run.prompt}</strong>
                          <span className="run-id">
                            {run.run_id.slice(0, 8)} <span>·</span> {dateTime(run.created_at)}
                          </span>
                          <span className="run-workspace" title={run.workspace}>
                            {run.workspace}
                          </span>
                        </div>
                        <div className="run-row-state">
                          <RunStatus run={run} />
                          {run.result.validation_status === 'unverified' && run.active && (
                            <span className="muted">尚未验收</span>
                          )}
                        </div>
                        <div className="run-count">
                          <strong>
                            {run.result.model_turns}
                            <span> / {run.result.tool_calls}</span>
                          </strong>
                          <span>{run.interrupted ? '已记录模型 / 工具' : '模型 / 工具'}</span>
                          <ArrowUpOutlined className="row-arrow" />
                        </div>
                      </Link>
                    </li>
                  ),
                )}
              </ul>
              <Pagination
                current={page}
                pageSize={12}
                total={runs.data.total}
                showSizeChanger={false}
                onChange={/* 页码写入 URL，后退可以恢复。 */ (value) => changeView(value, status)}
                size="small"
              />
            </>
          )}
        </section>
        <aside className="composer-column">
          {options.error ? (
            <Alert
              type="error"
              title="无法读取运行选项"
              description={errorMessage(options.error)}
            />
          ) : options.data ? (
            <TaskForm
              options={options.data}
              busyId={runs.data ? runs.data.busy_run_id : options.data.busy_run_id}
            />
          ) : (
            <Skeleton active />
          )}
        </aside>
      </div>
    </>
  );
}
