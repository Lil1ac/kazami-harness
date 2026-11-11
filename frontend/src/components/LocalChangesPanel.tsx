import { Alert } from 'antd';
import { FileChangesPanel } from './FileChangesPanel';
import type { LocalChangeView } from '../types';

/** 展示已保存的本轮差异；修改已在原目录生效，无工作树保留/丢弃动作。 */
export function LocalChangesPanel({
  runId,
  workspace,
  changes,
  active,
}: {
  runId: string; // 当前查看轮的唯一运行编号。
  workspace: string; // 文件、命令与测试实际运行的目录。
  changes: LocalChangeView; // 本轮单独固化的变化状态。
  active: boolean; // 执行和差异收尾尚未结束时为true。
}) {
  return (
    <section className="project-review" aria-labelledby="local-changes-heading">
      <h3 id="local-changes-heading">
        {changes.status === 'ready' && changes.files.length > 0
          ? '改动已在原目录生效'
          : '原目录执行'}
      </h3>
      <details className="project-review-details">
        <summary>执行目录与差异范围</summary>
        <code>{workspace}</code>
        <p className="muted">相对本轮开始时的文件。</p>
      </details>
      {changes.warnings.map(
        /* 明确差异采集失败，不把执行完成当作完整证据。 */ (warning, index) => (
          <Alert key={index} type="warning" title={warning} />
        ),
      )}
      {changes.status === 'pending' && (
        <p>{active ? '正在执行或保存本轮差异。' : '本轮差异尚未完整保存，当前目录仍可继续。'}</p>
      )}
      <FileChangesPanel
        runId={runId}
        files={changes.files}
        ready={changes.status === 'ready'}
        patchAvailable={changes.patch_available}
        active={active}
        label="代码差异预览"
        downloadLabel="下载本轮差异"
        emptyLabel="本轮没有文件变化"
      />
    </section>
  );
}
