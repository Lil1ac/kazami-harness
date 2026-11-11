import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { Alert, Button, Popconfirm, Tag } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage, getProjectReview, reviewRun } from '../api';
import type { ProjectReview, RunView } from '../types';
import { reviewLabels } from '../format';
import { FileChangesPanel } from './FileChangesPanel';
import { IntegrationPanel } from './IntegrationPanel';

/** 审阅已保存的代码证据；active=true 或快照未完成时禁止改变工作树，失败保留已知状态。 */
export function ProjectReviewPanel({
  project,
  active,
  parentRunId,
  canManage,
}: {
  project: ProjectReview; // 宿主保存的项目、提交、文件清单与人工决定。
  active: boolean; // 后台任务仍执行或正在整理补丁时为 true。
  canManage: boolean; // 仅会话最新轮可管理共享目录；历史轮只读取固定证据。
  parentRunId?: string | null; // 追加轮来源，用于说明累计差异并链接前轮证据。
}) {
  const cache = useQueryClient();
  const [showIntegration, setShowIntegration] = useState(
    new URLSearchParams(window.location.search).has('integration'),
  ); // 后台作业深链接只展开固定记录，不重新投递。
  const snapshotQuery = useQuery({
    queryKey: ['project-review', project.run_id],
    queryFn: /* 仅对未活动的遗留 pending 读取宿主恢复接口，不重新执行模型或项目测试。 */ ({
      signal,
    }) => getProjectReview(project.run_id, signal),
    enabled: canManage && !active && project.snapshot_status === 'pending',
    staleTime: Infinity,
    retry: false,
  });
  useEffect(
    /* 将宿主补捕获的真实证据同步到详情，ready/error 状态不请求重新拍摄。 */ () => {
      if (!snapshotQuery.data) return;
      cache.setQueryData<RunView>(
        ['run', project.run_id],
        /* 恢复只替换项目证据，不能把中断执行改为已完成。 */ (current) =>
          current ? { ...current, project: snapshotQuery.data } : current,
      );

      // 快照恢复与审阅决定都会改变可继续条件，同步刷新会话中的追加入口。
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
    [cache, project.run_id, snapshotQuery.data],
  );
  const decision = useMutation({
    mutationFn: /* 人工操作才发送决定，后端仍独立检查活动状态与工作树归属。 */ (
      value: 'keep' | 'discard',
    ) => reviewRun(project.run_id, value),
    onSuccess: /* 更新当前事实后刷新详情和列表，失败时不会提前显示已保留或已丢弃。 */ (review) => {
      cache.setQueryData<RunView>(
        ['run', project.run_id],
        /* 保留执行结果等字段，仅替换服务端返回的审阅记录。 */ (current) =>
          current ? { ...current, project: review } : current,
      );
      void cache.invalidateQueries({ queryKey: ['run', project.run_id] });

      // 快照恢复与审阅决定都会改变可继续条件，同步刷新会话中的追加入口。
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
  });
  const locked = !canManage || active || project.snapshot_status !== 'ready' || decision.isPending;
  return (
    <section className="project-review" aria-labelledby="project-review-heading">
      <div className="section-heading">
        <div>
          <h2 id="project-review-heading">代码改动</h2>
        </div>
        <Tag color={project.status === 'pending' ? 'warning' : undefined}>
          {reviewLabels[project.status]}
        </Tag>
      </div>
      <details className="project-review-details">
        <summary>独立工作树与提交基线</summary>
        <dl className="compact-dl review-baseline">
          <div>
            <dt>项目</dt>
            <dd>{project.project_label}</dd>
          </div>
          <div>
            <dt>原始仓库</dt>
            <dd>{project.repository}</dd>
          </div>
          <div>
            <dt>提交基线</dt>
            <dd>
              <code>
                {project.base_ref === project.base_commit
                  ? project.base_commit
                  : project.base_ref + ' · ' + project.base_commit}
              </code>
            </dd>
          </div>
          <div>
            <dt>会话工作树</dt>
            <dd>{project.worktree}</dd>
          </div>
        </dl>
        <p className="muted">
          {parentRunId ? (
            <>
              本轮复用会话目录，<Link to={`/runs/${parentRunId}?tab=files`}>前轮证据</Link>保持只读
              ，以下为相对会话原始基线的累计差异。测试与审阅结果属于本轮。
            </>
          ) : (
            '本轮差异以该提交为基线。'
          )}
        </p>
      </details>
      {project.source_dirty && (
        <Alert showIcon type="info" title="建立任务时原目录有未提交改动，未带入本次工作树。" />
      )}
      {project.warnings.map(
        /* 快照与回收警告来自受控后端，但仍只按安全文本渲染。 */ (warning, index) => (
          <Alert key={index} showIcon type="warning" title={warning} />
        ),
      )}
      {project.snapshot_status === 'pending' && (
        <Alert
          showIcon
          type="info"
          title={
            active
              ? '运行结束后整理代码改动；当前不能保留或丢弃。'
              : '代码快照尚未完成，暂时不能做审阅决定。'
          }
        />
      )}
      {project.snapshot_status === 'error' && (
        <Alert
          showIcon
          type="error"
          title="代码快照整理失败，暂时不能做审阅决定。"
          description="请检查运行记录与上方提示，已有执行和测试证据仍可查看。"
        />
      )}
      {snapshotQuery.error && (
        <Alert
          showIcon
          type="error"
          title="无法恢复代码审阅快照"
          description={errorMessage(snapshotQuery.error)}
        />
      )}
      {decision.error && (
        <Alert
          showIcon
          type="error"
          title="审阅操作未完成"
          description={errorMessage(decision.error)}
        />
      )}
      <FileChangesPanel
        runId={project.run_id}
        files={project.files}
        ready={project.snapshot_status === 'ready'}
        patchAvailable={project.patch_available}
        active={active}
        label="代码补丁预览"
        downloadLabel="下载完整补丁"
        emptyLabel="相对基线没有代码改动"
      />
      {!canManage && (
        <Alert showIcon type="info" title="历史轮次只读，请在最新一轮管理会话工作树。" />
      )}
      {project.status === 'discarded' ? (
        <Alert
          showIcon
          type="info"
          title="本任务工作树已丢弃。"
          description="运行历史、测试输出、补丁和已保存的各轮产物仍可查看。"
        />
      ) : canManage ? (
        <div className="review-actions">
          <Button
            type="primary"
            disabled={locked || project.status === 'kept'}
            loading={decision.isPending && decision.variables === 'keep'}
            onClick={/* 保留独立工作树，不创建提交或合并回原分支。 */ () => decision.mutate('keep')}
          >
            {project.status === 'kept' ? '已保留工作树' : '保留工作树'}
          </Button>
          <Popconfirm
            title="丢弃整个会话工作树？"
            description="移除该会话共享的工作树，之后不能继续修改。原仓库、各轮补丁和已保存历史产物保留。"
            okText="确认丢弃"
            cancelText="继续审阅"
            disabled={locked}
            onConfirm={
              /* 明确确认后才请求受控工作树回收，不删除原仓库。 */ () => decision.mutate('discard')
            }
          >
            <Button
              danger
              disabled={locked}
              loading={decision.isPending && decision.variables === 'discard'}
            >
              丢弃工作树
            </Button>
          </Popconfirm>
          <span className="muted">保留只保存工作树与补丁，不会自动提交或合并。</span>
        </div>
      ) : null}
      <Button
        disabled={active || project.snapshot_status !== 'ready'}
        onClick={
          /* 展开独立采纳流程，保留/丢弃的目录职责不变。 */ () =>
            setShowIntegration(!showIntegration)
        }
      >
        {showIntegration ? '收起成果采纳' : '成果采纳与历史'}
      </Button>
      {showIntegration && (
        <IntegrationPanel
          runId={project.run_id}
          sourceTest={
            cache.getQueryData<RunView>(['run', project.run_id])?.result.validation_status ||
            'unverified'
          }
        />
      )}
    </section>
  );
}
