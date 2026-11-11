import { useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Empty, InputNumber, Modal, Spin } from 'antd';
import {
  ArrowLeftOutlined,
  CloseOutlined,
  FileTextOutlined,
  FolderOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import {
  errorMessage,
  getProjectFileReference,
  listProjectFiles,
  previewProjectFile,
} from '../api';
import type { ProjectFileRange, ProjectFileReference, ProjectFileTarget } from '../types';
import './ProjectReferences.css';

/** 以相对路径标明 ref；整文件省略行号，行段显示两个包含端点。 */
function referenceLabel(ref: ProjectFileRange): string {
  return ref.start_line === null ? ref.path : ref.path + ':' + ref.start_line + '–' + ref.end_line;
}

/** 将 file 展示为一个小标签；onRemove 为空时只允许打开固定预览。 */
function ReferenceChip({
  file,
  onOpen,
  onRemove,
}: {
  file: ProjectFileReference; // 已选或历史保存的项目引用。
  onOpen?: (event: MouseEvent<HTMLButtonElement>) => void; // 打开当前标签对应的预览；无法读取时为空。
  onRemove?: () => void; // 仅编辑草稿时提供移除。
}) {
  const label = referenceLabel(file);
  return (
    <span className="file-reference-chip">
      <button
        type="button"
        className="file-reference-name"
        title={label}
        onClick={onOpen}
        disabled={!onOpen}
      >
        <FileTextOutlined aria-hidden />
        <span>{label}</span>
      </button>
      {onRemove && (
        <button
          type="button"
          className="file-reference-remove"
          aria-label={'移除 ' + label}
          onClick={onRemove}
        >
          <CloseOutlined aria-hidden />
        </button>
      )}
    </span>
  );
}

/** 从 startLine 开始为 content 标行号，行号与可复制的正文各自独立。 */
function ReferenceText({
  content,
  startLine,
}: {
  content: string; // 保留快照中的原文，显示时统一换行。
  startLine: number; // 整文件从 1 开始，行段使用所选起始行。
}) {
  if (!content) return <p className="file-reference-empty">空文件</p>;
  const display = content.replace(/\r\n?/g, '\n');
  const count = display.split('\n').length - (display.endsWith('\n') ? 1 : 0);
  const numbers = Array.from(
    { length: count },
    /* 以文件行号标记所见范围。 */ (_, index) => startLine + index,
  ).join('\n');
  return (
    <div className="file-reference-code" tabIndex={0} role="region" aria-label="引用文件正文">
      <pre className="file-reference-line-numbers" aria-hidden>
        {numbers}
      </pre>
      <pre className="file-reference-text">{display}</pre>
    </div>
  );
}

/** 一次选择流程中的目录浏览、行段预览和显式添加；离开后重置临时浏览状态。 */
function ReferencePicker({
  target,
  existing,
  onSelect,
  onClose,
}: {
  target: ProjectFileTarget; // 与实际发送位置一致的宿主来源。
  existing?: ProjectFileReference; // 点已有标签时重新预览该路径和范围。
  onSelect: (ref: ProjectFileReference) => void; // 提交用户已看见且确认的范围版本。
  onClose: () => void; // 关闭临时选择器，无提交副作用。
}) {
  const [directory, setDirectory] = useState('.'); // 当前浏览目录，根为点。
  const [offset, setOffset] = useState(0); // 同层分页的零基偏移。
  const [selection, setSelection] = useState<ProjectFileRange | null>(
    existing
      ? { path: existing.path, start_line: existing.start_line, end_line: existing.end_line }
      : null,
  ); // 请求只包含路径及行段。
  const [startLine, setStartLine] = useState<number | null>(existing?.start_line ?? null); // 尚待预览的起始行。
  const [endLine, setEndLine] = useState<number | null>(existing?.end_line ?? null); // 尚待预览的结束行。
  const directoryQuery = useQuery({
    queryKey: ['reference-files', target, directory, offset],
    queryFn: /* 切换目录取消旧请求，不扫描宿主任意路径。 */ ({ signal }) =>
      listProjectFiles(target, directory, offset, signal),
    enabled: selection === null,
    retry: false,
  });
  const preview = useQuery({
    queryKey: ['reference-preview', target, selection],
    queryFn: /* 正文预览和版本由同一次宿主读取返回。 */ ({ signal }) =>
      previewProjectFile(target, selection!, signal),
    enabled: selection !== null,
    retry: false,
    gcTime: 0,
  });
  const snapshot = preview.data?.snapshot;
  const matchingRange = selection?.start_line === startLine && selection?.end_line === endLine;
  const wholeFile = startLine === null && endLine === null;
  const validRange =
    wholeFile ||
    (startLine !== null &&
      endLine !== null &&
      startLine <= endLine &&
      Number.isSafeInteger(startLine) &&
      Number.isSafeInteger(endLine) &&
      startLine > 0);

  /** 打开 path 对应文件的整份预览，清除前一个文件的行段输入。 */
  function openFile(path: string): void {
    setStartLine(null);
    setEndLine(null);
    setSelection({ path, start_line: null, end_line: null });
  }

  /** 切换到 path 目录的第一页，保留选择器焦点和外部任务草稿。 */
  function openDirectory(path: string): void {
    setOffset(0);
    setDirectory(path);
  }

  /** 请求用户填好的行段；两个行号为空时回到整文件。 */
  function previewRange(): void {
    if (selection && validRange)
      setSelection({ path: selection.path, start_line: startLine, end_line: endLine });
  }

  /** 添加已核对快照的标识；正文由宿主重新验证和固定。 */
  function confirm(): void {
    if (snapshot && !preview.isFetching && !preview.error && matchingRange) {
      const { path, start_line, end_line, version } = snapshot;
      onSelect({ path, start_line, end_line, version });
    }
  }

  return (
    <Modal
      open
      width={640}
      title={selection ? '预览项目文件' : '引用项目文件'}
      onCancel={onClose}
      footer={
        selection && (
          <Button
            type="primary"
            disabled={!snapshot || !matchingRange || preview.isFetching || Boolean(preview.error)}
            onClick={confirm}
          >
            {existing ? '更新引用' : '添加引用'}
          </Button>
        )
      }
      className="file-reference-modal"
    >
      {selection ? (
        <div className="file-reference-dialog">
          <div className="file-reference-location">
            <Button
              type="text"
              size="small"
              aria-label="返回文件列表"
              icon={<ArrowLeftOutlined />}
              onClick={/* 退出预览后返回当前目录和页码。 */ () => setSelection(null)}
            />
            <code title={selection.path}>{selection.path}</code>
          </div>
          <div className="file-reference-range">
            <InputNumber
              aria-label="起始行"
              placeholder="起始行"
              min={1}
              precision={0}
              value={startLine}
              onChange={setStartLine}
              size="small"
            />
            <span aria-hidden>–</span>
            <InputNumber
              aria-label="结束行"
              placeholder="结束行"
              min={1}
              precision={0}
              value={endLine}
              onChange={setEndLine}
              size="small"
            />
            <Button
              size="small"
              disabled={!validRange || matchingRange || preview.isFetching}
              onClick={previewRange}
            >
              {wholeFile ? '预览整文件' : '预览行段'}
            </Button>
            {preview.data && <small>共 {preview.data.line_count} 行</small>}
          </div>
          {preview.isFetching ? (
            <Spin description="读取项目文件" />
          ) : preview.error ? (
            <Alert type="error" title={errorMessage(preview.error)} />
          ) : (
            preview.data && (
              <>
                {!snapshot && <Alert type="info" title="整段超过 32 KiB，请选择较小的行段。" />}
                {snapshot && existing && snapshot.version !== existing.version && (
                  <Alert type="info" title="这是当前预览版本，点击更新引用后生效。" />
                )}
                {snapshot?.commit && (
                  <small title={snapshot.commit}>提交 {snapshot.commit.slice(0, 8)}</small>
                )}
                <ReferenceText
                  content={snapshot?.content ?? preview.data.excerpt}
                  startLine={selection.start_line ?? 1}
                />
              </>
            )
          )}
        </div>
      ) : (
        <div className="file-reference-dialog">
          <div className="file-reference-location">
            {directory !== '.' && (
              <Button
                type="text"
                size="small"
                aria-label="返回上级目录"
                icon={<ArrowLeftOutlined />}
                onClick={
                  /* 上级目录从第一页重新浏览。 */ () =>
                    openDirectory(directory.split('/').slice(0, -1).join('/') || '.')
                }
              />
            )}
            <code title={directory}>{directory === '.' ? '项目根目录' : directory}</code>
          </div>
          {directoryQuery.isPending ? (
            <Spin description="读取项目目录" />
          ) : directoryQuery.error ? (
            <Alert type="error" title={errorMessage(directoryQuery.error)} />
          ) : directoryQuery.data.entries.length ? (
            <ul className="file-reference-entries" aria-label="项目文件">
              {directoryQuery.data.entries.map(
                /* 项目相对路径作为唯一入口，不接受手工宿主路径。 */ ({ path, kind }) => (
                  <li key={path}>
                    <button
                      type="button"
                      title={path}
                      onClick={
                        /* 目录继续浏览，普通文件先打开预览。 */ () =>
                          kind === 'directory' ? openDirectory(path) : openFile(path)
                      }
                    >
                      {kind === 'directory' ? (
                        <FolderOutlined aria-hidden />
                      ) : (
                        <FileTextOutlined aria-hidden />
                      )}
                      <span>{path.split('/').at(-1)}</span>
                    </button>
                  </li>
                ),
              )}
            </ul>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此目录没有可引用的文件" />
          )}
          {(offset > 0 || directoryQuery.data?.next_offset != null) && (
            <div className="file-reference-pagination">
              <Button
                size="small"
                disabled={offset === 0}
                onClick={
                  /* 宿主每页 100 条，从零开始。 */ () => setOffset(Math.max(0, offset - 100))
                }
              >
                上一页
              </Button>
              <Button
                size="small"
                disabled={directoryQuery.data?.next_offset == null}
                onClick={
                  /* 下一页位置由宿主返回。 */ () =>
                    setOffset(directoryQuery.data?.next_offset ?? offset)
                }
              >
                下一页
              </Button>
            </div>
          )}
          {directoryQuery.data?.scan_limited && (
            <small>目录条目过多，列表只展示已扫描的部分。</small>
          )}
        </div>
      )}
    </Modal>
  );
}

/** 新建与续聊共用引用入口；所有选择通过 onChange 写回当前发送草稿。 */
export function ProjectReferences({
  target,
  scope,
  items,
  onChange,
  disabled = false,
}: {
  target?: ProjectFileTarget; // 未选来源时入口不可用。
  scope: string; // 打开后若执行位置变化则退出当前预览。
  items: ProjectFileReference[]; // 已加入草稿的引用，顺序与发送顺序一致。
  onChange: (items: ProjectFileReference[]) => void; // 更新可编辑引用，无网络提交。
  disabled?: boolean; // 作业待确认或当前运行无法提交普通消息。
}) {
  const [picker, setPicker] = useState<{ scope: string; index: number | null }>(); // null 索引添加，数字索引更新。
  const open = Boolean(target && !disabled && picker?.scope === scope);
  const addButton = useRef<HTMLButtonElement>(null); // 编辑标签被合并时的焦点回退。
  const opener = useRef<HTMLElement | null>(null); // 记录打开本次选择器的按钮。

  /** 以 trigger 打开 index 项引用；index 为空时从项目根选择新文件。 */
  function show(trigger: HTMLElement, index: number | null): void {
    opener.current = trigger;
    setPicker({ scope, index });
  }

  /** 关闭选择器，在 DOM 更新后把焦点还给原标签或添加入口。 */
  function close(): void {
    setPicker(undefined);
    requestAnimationFrame(
      /* 合并或移除标签后可能找不到原按钮。 */ () =>
        (opener.current?.isConnected ? opener.current : addButton.current)?.focus({
          preventScroll: true,
        }),
    );
  }

  /** 添加或替换预览结果 ref，同一文件同一行段只保留一次。 */
  function select(ref: ProjectFileReference): void {
    const existing = items.findIndex(
      /* 比较路径和行段，已存在的引用可更新其版本。 */ (item) =>
        item.path === ref.path &&
        item.start_line === ref.start_line &&
        item.end_line === ref.end_line,
    );
    const updated = [...items];
    if (picker?.index != null) {
      if (existing >= 0 && existing !== picker.index) {
        updated[existing] = ref;
        updated.splice(picker.index, 1);
      } else updated[picker.index] = ref;
    } else if (existing >= 0) updated[existing] = ref;
    else if (updated.length < 8) updated.push(ref);
    onChange(updated);
    close();
  }

  return (
    <>
      <div className="composer-file-references">
        <Button
          ref={addButton}
          type="text"
          size="small"
          aria-label="引用项目文件"
          title={items.length >= 8 ? '每轮最多引用 8 项' : '引用项目文件'}
          icon={<PlusOutlined />}
          disabled={!target || disabled || items.length >= 8}
          onClick={/* 从项目根目录打开选择器。 */ (event) => show(event.currentTarget, null)}
        >
          {items.length === 0 && '引用文件'}
        </Button>
        {items.map(
          /* 保留显式添加顺序，移除只影响当前草稿。 */ (item, index) => (
            <ReferenceChip
              key={referenceLabel(item)}
              file={item}
              onOpen={
                target && !disabled
                  ? /* 查看已有引用时打开相同范围。 */ (event) => show(event.currentTarget, index)
                  : undefined
              }
              onRemove={
                disabled
                  ? undefined
                  : /* 只删除当前标签。 */ () =>
                      onChange(
                        items.filter(/* 其他标签顺序保持。 */ (_, position) => position !== index),
                      )
              }
            />
          ),
        )}
      </div>
      {open && target && (
        <ReferencePicker
          target={target}
          existing={picker?.index == null ? undefined : items[picker.index]}
          onSelect={select}
          onClose={close}
        />
      )}
    </>
  );
}

/** 由 runId 和保存顺序读取历史引用，展示的正文与模型使用的快照来自同一轮。 */
export function HistoricalFileReferences({
  runId,
  items,
}: {
  runId: string; // 对应用户消息的独立运行。
  items: ProjectFileReference[]; // 后端消息摘要中的引用顺序。
}) {
  const [index, setIndex] = useState<number | null>(null); // 未打开时不读取正文。
  const snapshot = useQuery({
    queryKey: ['reference-snapshot', runId, index],
    queryFn: /* 只读取运行内证据，归档与删除权限由宿主核对。 */ ({ signal }) =>
      getProjectFileReference(runId, index!, signal),
    enabled: index !== null,
    retry: false,
    gcTime: 0,
  });
  return (
    <div className="historical-file-references" aria-label="本轮项目文件引用">
      {items.map(
        /* 只显示摘要，点击才请求冻结正文。 */ (item, position) => (
          <ReferenceChip
            key={referenceLabel(item)}
            file={item}
            onOpen={/* 按该轮保存顺序取引用。 */ () => setIndex(position)}
          />
        ),
      )}
      <Modal
        open={index !== null}
        width={640}
        title="本轮文件引用"
        footer={null}
        onCancel={/* 关闭后保留所在消息与滚动位置。 */ () => setIndex(null)}
        className="file-reference-modal"
      >
        {index !== null && (
          <div className="file-reference-dialog">
            <code>{referenceLabel(items[index])}</code>
            {snapshot.isPending ? (
              <Spin description="读取保存的引用" />
            ) : snapshot.error ? (
              <Alert type="error" title={errorMessage(snapshot.error)} />
            ) : (
              <>
                {snapshot.data.commit && (
                  <small title={snapshot.data.commit}>
                    提交 {snapshot.data.commit.slice(0, 8)}
                  </small>
                )}
                <ReferenceText
                  content={snapshot.data.content}
                  startLine={snapshot.data.start_line ?? 1}
                />
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
