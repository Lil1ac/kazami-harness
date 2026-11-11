/** 补丁的一行；行号来自 hunk，仅用于阅读，元数据行和新增/删除另一侧行号为 null。 */
export interface DiffLine {
  content: string; // 保留前导 +/- 和原始正文，渲染时始终作为文本。
  kind: 'add' | 'remove' | 'context' | 'meta' | 'hunk'; // 控制差异着色与行号布局。
  oldNumber: number | null; // 原文件行号，从 1 开始。
  newNumber: number | null; // 修改后文件行号，从 1 开始。
}

/** 单文件补丁分组；无法识别文件名时保留“补丁”标题，不丢弃任何正文。 */
export interface DiffFile {
  path: string; // +++ 目标路径；删除文件取 --- 来源路径。
  lines: DiffLine[]; // 包含头部和正文的原始顺序。
}

/** 将 Git 的带引号路径还原为 UTF-8 文件名；普通路径保持原样。 */
function unquotePath(path: string): string {
  if (!path.startsWith('"') || !path.endsWith('"')) return path;
  return path.slice(1, -1).replace(
    /(?:\\[0-7]{3})+|\\./g,
    /* 解码连续的八进制 UTF-8 字节和 Git 的控制字符转义。 */ (escaped) => {
      if (escaped.length > 2) {
        const bytes = escaped
          .match(/[0-7]{3}/g)!
          .map(/* 单组始终是一个八进制字节。 */ (value) => Number.parseInt(value, 8));
        return new TextDecoder().decode(Uint8Array.from(bytes));
      }
      const control: Record<string, string> = {
        a: '\x07',
        b: '\b',
        f: '\f',
        n: '\n',
        r: '\r',
        t: '\t',
        v: '\v',
      };
      return control[escaped[1]] ?? escaped[1];
    },
  );
}

/** 解析 Git 或普通 unified diff 文本 content，返回按文件分组的有界预览；不执行正文、不访问文件。 */
export function parseDiff(content: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | undefined; // 遇到新文件头才创建下一组。
  let oldNumber = 0;
  let newNumber = 0;
  let oldRemaining = 0; // 当前 hunk 两侧剩余行数，防止把正文中的 ---/+++ 误判为文件头。
  let newRemaining = 0;
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  if (lines.at(-1) === '') lines.pop();
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const gitHeader = line.startsWith('diff --git ');
    const fileHeader =
      oldRemaining === 0 &&
      newRemaining === 0 &&
      line.startsWith('--- ') &&
      lines[index + 1]?.startsWith('+++ ');
    if (
      gitHeader ||
      (fileHeader &&
        current?.lines.some(
          /* 仅已读取正文的普通补丁需要切分；Git 的 --- 属于刚建好的文件组。 */ (item) =>
            item.kind === 'hunk',
        ))
    )
      current = undefined;
    if (!current) {
      const target = /^diff --git (?:"(?:[^"\\]|\\.)*"|a\/.+?) ("(?:[^"\\]|\\.)*"|b\/.+)$/.exec(
        line,
      )?.[1];
      current = { path: target ? unquotePath(target).replace(/^b\//, '') : '补丁', lines: [] };
      files.push(current);
      oldRemaining = newRemaining = 0;
    }
    if (fileHeader) {
      const target = unquotePath(lines[index + 1].slice(4).split('\t')[0]);
      current.path = (
        target === '/dev/null' ? unquotePath(line.slice(4).split('\t')[0]) : target
      ).replace(/^[ab]\//, '');
    }
    if (!oldRemaining && !newRemaining && line.startsWith('rename to ')) {
      current.path = unquotePath(line.slice('rename to '.length));
    }
    const hunk = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line);
    const item: DiffLine = { content: line, kind: 'meta', oldNumber: null, newNumber: null };
    if (hunk) {
      oldNumber = Number(hunk[1]);
      newNumber = Number(hunk[3]);
      oldRemaining = Number(hunk[2] ?? 1);
      newRemaining = Number(hunk[4] ?? 1);
      item.kind = 'hunk';
    } else if (oldRemaining > 0 || newRemaining > 0) {
      if (line.startsWith('+')) {
        item.kind = 'add';
        item.newNumber = newNumber++;
        newRemaining -= 1;
      } else if (line.startsWith('-')) {
        item.kind = 'remove';
        item.oldNumber = oldNumber++;
        oldRemaining -= 1;
      } else if (line.startsWith(' ')) {
        item.kind = 'context';
        item.oldNumber = oldNumber++;
        item.newNumber = newNumber++;
        oldRemaining -= 1;
        newRemaining -= 1;
      }
    }
    current.lines.push(item);
  }
  return files;
}
