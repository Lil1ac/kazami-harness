import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Empty, Spin } from 'antd';
import { DownloadOutlined, FileOutlined } from '@ant-design/icons';
import { errorMessage, getDiff, patchUrl } from '../api';
import { parseDiff } from '../diff';
import type { ProjectReview } from '../types';

/** 两种执行方式共享的文件清单输入；完整清单独立于可能被截断的文本补丁。 */
interface FileChangesProps {
  runId: string; // 保存差异的独立运行编号。
  files: ProjectReview['files']; // 已固化的新增、修改、删除、重命名及二进制文件。
  ready: boolean; // 采集成功才允许显示无变更。
  patchAvailable: boolean; // 是否存在可下载的完整补丁。
  active: boolean; // 运行或收尾期间禁止提前读取可能返回 409 的补丁。
  label: string; // 整个补丁阅读区域的可访问名称。
  downloadLabel: string; // 区分本轮原目录差异与隔离累计补丁。
  emptyLabel: string; // 根据相对本轮/原始基线给出准确的空状态。
}

/** 文件状态的统一中文名称；二进制标记仍单独展示。 */
const fileLabels = { added: '新增', modified: '修改', deleted: '删除', renamed: '重命名' };

/** 按 props 展示固定文件清单和分文件补丁；只读已保存证据，无编辑、回滚或审阅副作用。 */
export function FileChangesPanel({
  runId,
  files,
  ready,
  patchAvailable,
  active,
  label,
  downloadLabel,
  emptyLabel,
}: FileChangesProps) {
  const diff = useQuery({
    queryKey: ['diff', runId],
    queryFn: /* 仅读取指定轮的固定证据，不扫描当前目录。 */ ({ signal }) => getDiff(runId, signal),
    enabled: !active && ready && patchAvailable,
    retry: false,
  });
  const sections = diff.data ? parseDiff(diff.data.content) : [];
  return (
    <div className="file-changes">
      <div className="section-heading changed-files-heading">
        <h3>
          变更文件 <small>{files.length}</small>
        </h3>
        {patchAvailable && (
          <Button
            size="small"
            href={patchUrl(runId)}
            icon={<DownloadOutlined aria-hidden />}
            download
          >
            {downloadLabel}
          </Button>
        )}
      </div>
      {ready && files.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyLabel} />
      ) : (
        <ul className="changed-files">
          {files.map(
            /* 清单不从文本 diff 推断，确保二进制和纯重命名仍可见。 */ (file) => (
              <li key={file.path}>
                <FileOutlined aria-hidden />
                <code>{file.path}</code>
                <span className={'file-status file-' + file.status}>{fileLabels[file.status]}</span>
                {file.binary && <span className="muted">二进制文件</span>}
              </li>
            ),
          )}
        </ul>
      )}
      {diff.isFetching && <Spin description="读取代码差异" />}
      {diff.error && (
        <Alert type="error" title="无法读取差异" description={errorMessage(diff.error)} />
      )}
      {diff.data?.truncated && <Alert type="warning" title="差异预览已截断，请下载完整补丁。" />}
      {sections.length > 0 && (
        <div className="diff-preview" aria-label={label}>
          {sections.map(
            /* 保留原始补丁次序；同路径多段也不会覆盖。 */ (file, index) => (
              <details className="diff-file" key={index} open>
                <summary>
                  <FileOutlined aria-hidden /> <code>{file.path}</code>
                </summary>
                <div
                  className="diff-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label={'文件差异：' + file.path}
                >
                  <pre>
                    {file.lines.map(
                      /* 使用文本节点展示不可信代码，避免 HTML 注入。 */ (line, ordinal) => (
                        <span key={ordinal} className={'diff-line diff-' + line.kind}>
                          <span className="diff-number" aria-hidden>
                            {line.oldNumber}
                          </span>
                          <span className="diff-number" aria-hidden>
                            {line.newNumber}
                          </span>
                          <span className="diff-content">{line.content || ' '}</span>
                          {'\n'}
                        </span>
                      ),
                    )}
                  </pre>
                </div>
              </details>
            ),
          )}
        </div>
      )}
    </div>
  );
}
