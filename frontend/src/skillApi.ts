import { request } from './api';
import type { SkillOption, SkillResourceSnapshot, SkillSnapshot } from './types';

/** 浏览器显式选中的技能包和过滤出的受支持文件。 */
export interface SkillImport {
  name: string; // 选中的技能根目录名，后端核对正文 frontmatter。
  files: { path: string; content: string }[]; // 有界 UTF-8 原文，路径相对技能目录。
  ignored: number; // 位于首版资源目录之外的文件数量。
}

/** 读取 identifier 当前正文与资源名，返回预览快照。 */
export function previewSkill(identifier: string): Promise<SkillSnapshot> {
  return request(`/api/skills/${encodeURIComponent(identifier)}`);
}

/** 读取 identifier 下的相对 path，宿主再次核验资源范围。 */
export function previewSkillResource(identifier: string, path: string): Promise<SkillResourceSnapshot> {
  return request(`/api/skills/${encodeURIComponent(identifier)}/resource?${new URLSearchParams({ path })}`);
}

/** 保存 packageInput 的受管副本，revision 非空时明确替换当时已审阅的版本。 */
export function importSkill(packageInput: SkillImport, revision: string | null): Promise<SkillOption> {
  return request('/api/skills/import', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: packageInput.name, files: packageInput.files, revision }),
  });
}

/** 对 item 当前管理版本执行 action，版本变化由宿主拒绝。 */
export function changeSkill(item: SkillOption, action: 'enable' | 'disable' | 'remove'): Promise<{ ok: boolean }> {
  return request(`/api/skills/${encodeURIComponent(item.id || '')}/actions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: item.revision, action }),
  });
}

/** 读取 folder 中用户选择的文件，保留受支持文本并在过大或无正文时给出受控错误。 */
export async function readSkillFolder(folder: FileList): Promise<SkillImport> {
  if (!folder.length || folder.length > 512) throw new Error('请选一个文件数不超过 512 的技能目录。');
  const all = Array.from(folder);
  const name = all[0].webkitRelativePath.split('/')[0];
  const selected = all.flatMap((file) => {
    const [root, ...parts] = file.webkitRelativePath.split('/');
    if (root !== name || !parts.length) throw new Error('请选择单个技能目录。');
    const path = parts.join('/');
    return path === 'SKILL.md' || ['references', 'assets'].includes(parts[0]) ? [{ file, path }] : [];
  });
  if (!selected.some((item) => item.path === 'SKILL.md')) throw new Error('所选目录缺少 SKILL.md。');
  if (selected.length > 64 || selected.some(({ file }) => file.size > 64 * 1024) ||
      selected.reduce((bytes, { file }) => bytes + file.size, 0) > 512 * 1024) {
    throw new Error('技能包最多 64 个文件，单文件 64 KiB，总计 512 KiB。');
  }
  const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
  try {
    const files = await Promise.all(selected.map(async ({ file, path }) => ({ path, content: decoder.decode(await file.arrayBuffer()) })));
    return { name, files, ignored: all.length - selected.length };
  } catch {
    throw new Error('技能正文及资料必须是可读取的 UTF-8 文本。');
  }
}
