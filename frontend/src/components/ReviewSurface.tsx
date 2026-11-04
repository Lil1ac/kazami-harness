import type { ReactNode } from 'react';
import { SafetyCertificateOutlined } from '@ant-design/icons';
import './ReviewSurface.css';
import { parseDiff } from '../diff';

/** 将argv转换为只读命令文案；保留引号和空参数边界，返回字符串不用于真实执行。 */
export function readableCommand(argv: unknown): string {
  return Array.isArray(argv)
    ? argv
        .map(
          /* JSON明确表示含空白或控制字符的单个参数。 */ (value) =>
            /^[\w./:\\=-]+$/.test(String(value)) ? String(value) : JSON.stringify(value),
        )
        .join(' ')
    : '未提供命令参数';
}

/** 展示宿主已有的unified补丁content；label标识固定证据，解析失败仍按原始文本保留。 */
export function ReviewPatch({ content, label }: { content: string; label: string }) {
  if (content.split('\n').length > 1200) return <pre aria-label={label}>{content}</pre>;
  return (
    <div className="review-diff" aria-label={label} tabIndex={0}>
      {parseDiff(content)
        .flatMap(/* 保留文件头和元数据，不将正文解释为HTML。 */ (file) => file.lines)
        .map(
          /* 行号来自宿主补丁，不推断新版本内容。 */ (line, index) => (
            <div
              key={index}
              className={`review-diff-line review-diff-${line.kind === 'add' ? 'added' : line.kind === 'remove' ? 'removed' : 'context'}`}
            >
              <span className="review-line-number">{line.oldNumber}</span>
              <span className="review-line-number">{line.newNumber}</span>
              <code className="review-patch-line">{line.content || ' '}</code>
            </div>
          ),
        )}
    </div>
  );
}

/** 审阅标题；title为动作名称，status是服务端事实的阅读文案，children为必要目标信息。 */
export function ReviewHeading({
  title,
  status,
  children,
}: {
  title: string; // 本次要确认的具体动作。
  status: string; // 授权状态，不代表操作成功。
  children?: ReactNode; // 可选版本或来源信息，不包含操作按钮。
}) {
  return (
    <header className="review-heading">
      <div className="review-heading-title">
        <SafetyCertificateOutlined aria-hidden />
        <strong>{title}</strong>
        {children}
      </div>
      <span className="review-status" role="status">
        {status}
      </span>
    </header>
  );
}

/** 统一审批页脚；children为领域自己的决定按钮，note只解释本次决定的作用，不执行请求。 */
export function ReviewFooter({
  children,
  note,
}: {
  children: ReactNode; // 已绑定原请求的明确操作，组件不生成授权。
  note?: string; // 可选范围或失效提示。
}) {
  return (
    <footer className="review-footer">
      {note && <p className="review-note">{note}</p>}
      <div className="review-buttons">{children}</div>
    </footer>
  );
}

/** 按原始文本生成有界行差异；before=null表示新文件，label用于辅助技术标识阅读区域。
 * 采用共同首尾+中间替换的线性算法，保持全部内容，不声称是最小编辑序列；大文本回退完整原文，避免海量DOM。
 */
export function ReviewDiff({
  before,
  after,
  label = '待写入内容',
}: {
  before: string | null; // 审批快照中的原文，null仅表示新文件。
  after: string; // 本次固定的新文本或替换片段。
  label?: string; // 当前上下文的可访问名称。
}) {
  const oldLines = before === null ? [] : before.split('\n'); // 保留末尾空行，区分末尾换行。
  const newLines = after.split('\n'); // 空字符串也是合法文件内容。
  let prefix = 0; // 不变首段的行数。
  let suffix = 0; // 不变尾段的行数，与首段不重叠。
  while (
    prefix < Math.min(oldLines.length, newLines.length) &&
    oldLines[prefix] === newLines[prefix]
  )
    prefix++;
  while (
    suffix < Math.min(oldLines.length, newLines.length) - prefix &&
    oldLines.at(-1 - suffix) === newLines.at(-1 - suffix)
  )
    suffix++;
  if (oldLines.length + newLines.length > 1200)
    return (
      <div className="review-large-text" aria-label={label}>
        <p className="review-note">内容较长，按完整原文展示。</p>
        {before !== null && (
          <>
            <h4>修改前</h4>
            <pre>{before}</pre>
          </>
        )}
        <h4>修改后</h4>
        <pre>{after}</pre>
      </div>
    );
  const rows: { text: string; old?: number; next?: number; kind: string }[] = []; // 原行号、新行号和变更类型共同保证颜色之外仍可辨认。
  for (let i = 0; i < prefix; i++)
    rows.push({ text: oldLines[i], old: i + 1, next: i + 1, kind: 'context' });
  for (let i = prefix; i < oldLines.length - suffix; i++)
    rows.push({ text: oldLines[i], old: i + 1, kind: 'removed' });
  for (let i = prefix; i < newLines.length - suffix; i++)
    rows.push({ text: newLines[i], next: i + 1, kind: 'added' });
  for (let i = 0; i < suffix; i++)
    rows.push({
      text: newLines[newLines.length - suffix + i],
      old: oldLines.length - suffix + i + 1,
      next: newLines.length - suffix + i + 1,
      kind: 'context',
    });
  return (
    <div className="review-diff" aria-label={label} tabIndex={0}>
      <div className="review-diff-legend">
        <span>修改前 / 修改后</span>
        <span>− 删除　+ 新增</span>
      </div>
      {rows.map(
        /* 编号按快照位置固定，仅展示文本，不解释代码。 */ (row, index) => (
          <div className={`review-diff-line review-diff-${row.kind}`} key={index}>
            <span
              className="review-line-number"
              aria-label={row.old ? `原第${row.old}行` : undefined}
            >
              {row.old}
            </span>
            <span
              className="review-line-number"
              aria-label={row.next ? `新第${row.next}行` : undefined}
            >
              {row.next}
            </span>
            <span aria-hidden>
              {row.kind === 'removed' ? '−' : row.kind === 'added' ? '+' : ' '}
            </span>
            <code>{row.text || ' '}</code>
          </div>
        ),
      )}
    </div>
  );
}
