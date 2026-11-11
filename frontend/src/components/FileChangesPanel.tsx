import { useQuery } from '@tanstack/react-query';
import { useMemo, useRef, useState } from 'react';
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
  const [selectedPath, setSelectedPath] = useState<string>(); // 默认打开首个文本文件，宽度变化保留当前选择。
  const grid = useRef<HTMLDivElement>(null); // 用实际面板宽度判断文件列表是否与差异并排。
  const preview = useRef<HTMLDivElement>(null); // 单列布局点击文件后定位到差异正文。
  const sections = useMemo(
    /* 仅在收到新的固定补丁时解析。 */ () => (diff.data ? parseDiff(diff.data.content) : []),
    [diff.data],
  );
  const selected =
    files.find(/* 宿主清单是可选择文件的依据。 */ (file) => file.path === selectedPath) ??
    files.find(/* 优先提供可阅读的文本差异。 */ (file) => !file.binary) ??
    files[0];
  const selectedSections = sections.filter(
    /* 同一路径的多个分段保持宿主顺序。 */ (section) => section.path === selected?.path,
  );
  const shownSections = files.length > 0 ? selectedSections : sections;

  /** 选中清单中的 path；并排视图保留列表焦点，单列视图直接跳到正文。 */
  function selectFile(path: string): void {
    setSelectedPath(path);
    if (grid.current && grid.current.clientWidth < 560) {
      preview.current?.focus({ preventScroll: true });
      preview.current?.scrollIntoView?.({ block: 'start' });
    }
  }

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
      {ready && files.length === 0 && sections.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyLabel} />
      ) : null}
      {diff.data?.truncated && (
        <Alert type="warning" showIcon title="差异预览已截断，请下载完整补丁。" />
      )}
      <div
        className={'file-changes-layout' + (files.length > 0 ? ' has-file-list' : '')}
        ref={grid}
      >
        {files.length > 0 && (
          <ul className="changed-files" aria-label="变更文件">
            {files.map(
              /* 清单独立于文本补丁，二进制和纯重命名仍能选择。 */ (file) => (
                <li key={file.path}>
                  <button
                    type="button"
                    aria-current={file.path === selected?.path ? 'true' : undefined}
                    onClick={/* 只定位固定补丁中的文件。 */ () => selectFile(file.path)}
                  >
                    <FileOutlined aria-hidden />
                    <code>{file.path}</code>
                    <span className={'file-status file-' + file.status}>
                      {fileLabels[file.status]}
                    </span>
                    {file.binary && <span className="file-status">二进制文件</span>}
                  </button>
                </li>
              ),
            )}
          </ul>
        )}
        <div className="diff-preview" role="region" aria-label={label} ref={preview} tabIndex={-1}>
          {diff.isFetching && <Spin description="读取代码差异" />}
          {diff.error && (
            <Alert type="error" title="无法读取差异" description={errorMessage(diff.error)} />
          )}
          {selected && (
            <h4 className="diff-file-heading">
              <code>{selected.path}</code>
            </h4>
          )}
          {shownSections.map(
            /* 保留该文件补丁次序；只按文本渲染。 */ (file, index) => (
              <div className="diff-file" key={file.path + ':' + index}>
                <div
                  className="diff-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label={'文件差异：' + file.path}
                >
                  <pre>
                    {file.lines
                      .filter(
                        /* 文件名已在标题展示；完整下载仍保留原始路径头与 Git 索引行。 */ (line) =>
                          !selected ||
                          line.kind !== 'meta' ||
                          !/^(?:diff --git |index |--- |\+\+\+ )/.test(line.content),
                      )
                      .map(
                        /* 使用文本节点展示补丁内容。 */ (line, ordinal) => (
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
              </div>
            ),
          )}
          {selected && !shownSections.length && !diff.isFetching && !diff.error && (
            <>
              <p className="muted">
                {selected.binary ? '二进制文件。' : '当前没有该文件的文本差异预览。'}
              </p>
              {diff.data?.content && !selected.binary && (
                <details>
                  <summary>查看补丁原文</summary>
                  <pre>{diff.data.content}</pre>
                </details>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
