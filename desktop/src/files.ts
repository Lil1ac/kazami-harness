import { constants } from "node:fs";
import {
  copyFile,
  mkdir,
  open,
  readFile,
  rename,
  unlink,
} from "node:fs/promises";
import path from "node:path";

/** 原子替换 target 的 data 内容；临时文件位于同一目录，避免部分配置或密文文件可见。 */
export async function atomicWrite(
  target: string,
  data: string | Buffer,
): Promise<void> {
  const temporary = target + ".tmp";
  await mkdir(path.dirname(target), { recursive: true });
  const handle = await open(temporary, "w", 0o600);
  try {
    await handle.writeFile(data);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(temporary, target);
}

/** 将已核对 source 复制到绝对 directory；同名自动追加序号，返回保存路径和文件名。 */
export async function downloadFile(
  source: string,
  directory: string,
): Promise<{ path: string; filename: string }> {
  if (!path.isAbsolute(source) || !path.isAbsolute(directory))
    throw new Error("下载路径必须为绝对路径");
  await mkdir(directory, { recursive: true });
  const parsed = path.parse(source);
  for (let index = 0; index < 10000; index++) {
    const filename =
      index === 0 ? parsed.base : `${parsed.name} (${index})${parsed.ext}`;
    const target = path.join(directory, filename);
    try {
      await copyFile(source, target, constants.COPYFILE_EXCL);
      return { path: target, filename };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST")
        throw new Error("产物保存失败，请检查目录权限");
    }
  }
  throw new Error("下载目录存在过多同名文件");
}

/** 读取 target 的密文字节；尚未配置时返回 undefined，其他读取错误继续交给调用方处理。 */
export async function readOptional(
  target: string,
): Promise<Buffer | undefined> {
  try {
    return await readFile(target);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

/** 删除本应用 target 文件；不存在视为完成，权限错误继续抛出，不递归删除目录。 */
export async function removeOptional(target: string): Promise<void> {
  try {
    await unlink(target);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}
