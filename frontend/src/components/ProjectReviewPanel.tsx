import { useEffect } from 'react';
import { Alert, Button, Empty, Popconfirm, Spin, Tag } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage, getDiff, getProjectReview, patchUrl, reviewRun } from '../api';
import type { ProjectReview, RunView } from '../types';
import { reviewLabels } from '../format';

/** Git 文件状态的中文说明；路径和补丁均作为普通文本展示。 */
const fileLabels: Record<ProjectReview['files'][number]['status'], string> = {
  added: '新增',
  modified: '修改',
  deleted: '删除',
  renamed: '重命名',
};

/** 审阅已保存的代码证据；active=true 或快照未完成时禁止改变工作树，失败保留已知状态。 */
export function ProjectReviewPanel({
  project,
  active,
}: {
  project: ProjectReview; // 宿主保存的项目、提交、文件清单与人工决定。
  active: boolean; // 后台任务仍执行或正在整理补丁时为 true。
}) {
  const cache = useQueryClient();
  const recovery = useQuery({
    queryKey: ['project-review', project.run_id],
    queryFn: /* 仅对未活动的遗留 pending 读取宿主恢复接口，不重新执行模型或项目测试。 */ ({
      signal,
    }) => getProjectReview(project.run_id, signal),
    enabled: !active && project.snapshot_status === 'pending',
    staleTime: Infinity,
    retry: false,
  });
  useEffect(
    /* 将宿主补捕获的真实证据同步到详情，ready/error 状态不请求重新拍摄。 */ () => {
      if (!recovery.data) return;
      cache.setQueryData<RunView>(
        ['run', project.run_id],
        /* 恢复只替换项目证据，不能把中断执行改为已完成。 */ (current) =>
          current ? { ...current, project: recovery.data } : current,
      );
      void cache.invalidateQueries({ queryKey: ['runs'] });
    },
    [cache, project.run_id, recovery.data],
  );
  const diff = useQuery({
    queryKey: ['diff', project.run_id],
    queryFn: /* 仅读取运行记录内的快照，不访问任意宿主目录。 */ ({ signal }) =>
      getDiff(project.run_id, signal),
    enabled: project.snapshot_status === 'ready' && project.patch_available,
    retry: false,
  });
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
      void cache.invalidateQueries({ queryKey: ['runs'] });
    },
  });
  const locked = active || project.snapshot_status !== 'ready' || decision.isPending;
  return (
    <section className="project-review" aria-labelledby="project-review-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">CODE REVIEW</span>
          <h2 id="project-review-heading">代码改动</h2>
        </div>
        <Tag color={project.status === 'pending' ? 'warning' : undefined}>
          {reviewLabels[project.status]}
        </Tag>
      </div>
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
              {project.base_ref} · {project.base_commit}
            </code>
          </dd>
        </div>
        <div>
          <dt>独立工作树</dt>
          <dd>{project.worktree}</dd>
        </div>
      </dl>
      <p className="muted">
        任务从该提交开始，原目录中的未提交改动不带入任务。测试结果见“项目测试”，审阅决定由你完成。
      </p>
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
      {recovery.error && (
        <Alert
          showIcon
          type="error"
          title="无法恢复代码审阅快照"
          description={errorMessage(recovery.error)}
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
      {project.status === 'discarded' ? (
        <Alert
          showIcon
          type="info"
          title="本任务工作树已丢弃。"
          description="运行历史、测试输出和已保存补丁仍保留；工作树中的产物不再提供访问。"
        />
      ) : (
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
            title="丢弃本任务工作树？"
            description="只移除当前任务的独立工作树。原仓库不受影响，运行历史与已保存补丁仍保留。"
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
      )}
      <div className="section-heading changed-files-heading">
        <h3>
          变更文件 <small>{project.files.length}</small>
        </h3>
        {project.patch_available && (
          <Button href={patchUrl(project.run_id)} icon={<DownloadOutlined />} download>
            下载完整补丁
          </Button>
        )}
      </div>
      {project.snapshot_status === 'ready' && project.files.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="相对基线没有代码改动" />
      ) : (
        <ul className="changed-files">
          {project.files.map(
            /* 列表包含新文件和二进制文件，不能只用文本补丁推断改动数量。 */ (file) => (
              <li key={`${file.status}:${file.path}`}>
                <Tag>{fileLabels[file.status]}</Tag>
                <code>{file.path}</code>
                {file.binary && <span className="muted">二进制文件</span>}
              </li>
            ),
          )}
        </ul>
      )}
      {diff.isFetching && <Spin tip="读取代码补丁" />}
      {diff.error && (
        <Alert
          type="error"
          showIcon
          title="无法读取补丁预览"
          description={errorMessage(diff.error)}
        />
      )}
      {diff.data && (
        <>
          <p className="muted">保存的补丁 · {diff.data.size_bytes.toLocaleString()} 字节</p>
          {diff.data.truncated && (
            <Alert type="warning" showIcon title="补丁预览已截断，请下载完整补丁查看全部改动。" />
          )}
          {diff.data.content && (
            <pre className="diff-preview" aria-label="代码补丁预览">
              {diff.data.content}
            </pre>
          )}
        </>
      )}
    </section>
  );
}
