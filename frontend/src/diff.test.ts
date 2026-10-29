import { expect, it } from 'vitest';
import { parseDiff } from './diff';

it('普通补丁按中文文件分组并保留增删两侧行号和删除文件名', () => {
  // 覆盖原目录的 difflib 输出及删除文件，避免只支持 Git 补丁。
  const sections = parseDiff(
    '--- a/目录/配置.py\n+++ b/目录/配置.py\n@@ -2,2 +2,2 @@\n keep\n-old\n+new\n--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n',
  );
  expect(sections.map(/* 顺序必须与保存证据一致。 */ (file) => file.path)).toEqual([
    '目录/配置.py',
    'old.txt',
  ]);
  expect(
    sections[0].lines.find(/* 定位实际新增正文。 */ (line) => line.kind === 'add'),
  ).toMatchObject({ content: '+new', oldNumber: null, newNumber: 3 });
  expect(sections[1].lines.at(-1)).toMatchObject({ kind: 'remove', oldNumber: 1, newNumber: null });
});

it('正文中的文件头样式、Git 重命名和截断补丁不会丢失内容', () => {
  // hunk 计数避免将实际代码中的 --- 和 +++ 误认为下一文件。
  const content =
    'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n--- tricky\n+++ tricky\ndiff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\n';
  const files = parseDiff(content);
  expect(files).toHaveLength(2);
  expect(files[0].lines.at(-2)?.kind).toBe('remove');
  expect(files[0].lines.at(-1)?.kind).toBe('add');
  expect(files[1].lines.at(-1)?.content).toBe('rename to new');
  expect(parseDiff('@@ -1 +1 @@\n-old')).toHaveLength(1);
  expect(parseDiff('')).toEqual([]);
});
