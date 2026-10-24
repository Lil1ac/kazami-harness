import { Alert, Button, Empty, Spin, Tag } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { errorMessage, getDiff, patchUrl } from '../api';
import type { LocalChangeView } from '../types';

/** 原目录文件状态；相对本轮开始时的内容，不把已有未提交修改归给Agent。 */
const labels = { added: '新增', modified: '修改', deleted: '删除' };

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
  const diff = useQuery({
    queryKey: ['diff', runId],
    queryFn: /* 仅读取固定的本轮记录，不扫描当前目录。 */ ({ signal }) => getDiff(runId, signal),
    // 元数据可先于后台槽位释放写完；待终态再读取，避免409被缓存为最终差异错误。
    enabled: !active && changes.status === 'ready' && changes.patch_available,
    retry: false,
  });
  return (
    <section className="project-review" aria-labelledby="local-changes-heading">
      <h3 id="local-changes-heading">本轮文件变化</h3>
      <p>修改已在原目录生效。</p>
      <p className="local-execution-path">
        <code>{workspace}</code>
      </p>
      <p className="muted">
        相对本轮开始时的文件；Git 控制目录、依赖和缓存不在采集范围内。二进制文件保留快照并列出变化。
      </p>
      {changes.warnings.map(
        /* 明确差异采集失败，不把执行完成当作完整证据。 */ (warning, index) => (
          <Alert key={index} type="warning" title={warning} />
        ),
      )}
      {changes.status === 'pending' && (
        <p>{active ? '正在执行或保存本轮差异。' : '本轮差异尚未完整保存，当前目录仍可继续。'}</p>
      )}
      {changes.patch_available && (
        <Button href={patchUrl(runId)} icon={<DownloadOutlined aria-hidden />} download>
          下载本轮差异
        </Button>
      )}
      {changes.status === 'ready' && changes.files.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本轮没有文件变化" />
      ) : (
        <ul className="changed-files">
          {changes.files.map(
            /* 展示保存路径，而非当前项目即时状态。 */ (file) => (
              <li key={file.path}>
                <Tag>{labels[file.status]}</Tag>
                <code>{file.path}</code>
                {file.binary && <span className="muted">二进制文件</span>}
              </li>
            ),
          )}
        </ul>
      )}
      {diff.isFetching && <Spin description="读取本轮差异" />}
      {diff.error && (
        <Alert type="error" title="无法读取差异" description={errorMessage(diff.error)} />
      )}
      {diff.data && (
        <>
          {diff.data.truncated && <Alert type="warning" title="差异预览已截断，可下载完整记录。" />}
          {diff.data.content && (
            <pre className="diff-preview" aria-label="代码差异预览">
              {diff.data.content}
            </pre>
          )}
        </>
      )}
    </section>
  );
}
