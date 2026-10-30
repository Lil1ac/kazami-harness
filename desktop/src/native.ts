import { dialog, safeStorage, shell, BrowserWindow } from "electron";
import path from "node:path";
import {
  atomicWrite,
  downloadFile,
  readOptional,
  removeOptional,
} from "./files";
import { NativeRequest, textParameter } from "./protocol";

/** 验证 url 是可交给默认应用的网页或邮件地址；返回规范 URL，拒绝本地文件和脚本协议。 */
export function externalUrl(url: string): string {
  if (/[\r\n\0]/.test(url)) throw new Error("外部链接无效");
  const parsed = new URL(url);
  if (
    !["https:", "http:", "mailto:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password
  )
    throw new Error("只支持网页和邮件链接");
  return parsed.href;
}

/** Electron 主进程的原生能力；所有文件引用均来自后台已经核对的运行证据。 */
export class NativeService {
  private readonly credentialFile: string; // 仅保存 safeStorage 密文，不进入公开配置和运行记录。
  private credentialWrite: Promise<void> = Promise.resolve(); // 串行保存，避免并发替换同一密文文件。

  /** 固定 dataDirectory 与当前窗口读取器 window；构造不创建对话框或读取密钥。 */
  constructor(
    dataDirectory: string,
    private readonly window: () => BrowserWindow | undefined,
  ) {
    this.credentialFile = path.join(dataDirectory, "credential.enc");
  }

  /** 解密当前用户保存的密钥；未配置返回空字符串，系统加密不可用时明确失败，不退回明文。 */
  async readCredential(): Promise<string> {
    const encrypted = await readOptional(this.credentialFile);
    if (!encrypted) return "";
    if (!(await safeStorage.isAsyncEncryptionAvailable()))
      throw new Error("本机凭据加密服务不可用");
    const result = await safeStorage.decryptStringAsync(encrypted);
    if (result.shouldReEncrypt)
      await atomicWrite(
        this.credentialFile,
        await safeStorage.encryptStringAsync(result.result),
      );
    return result.result;
  }

  /** 保存或清除 secret；密钥只以系统加密后的字节落盘，失败时保留原密文。 */
  async writeCredential(secret: string): Promise<void> {
    const operation = this.credentialWrite.then(
      /* 同一时刻只允许一个密文保存或删除操作。 */ async () => {
        if (!secret) {
          await removeOptional(this.credentialFile);
          return;
        }
        if (!(await safeStorage.isAsyncEncryptionAvailable()))
          throw new Error("本机凭据加密服务不可用");
        await atomicWrite(
          this.credentialFile,
          await safeStorage.encryptStringAsync(secret),
        );
      },
    );
    this.credentialWrite = operation.catch(
      /* 本次失败不使后续明确重试永久失败。 */ () => {},
    );
    await operation;
  }

  /** 执行固定白名单 request，返回 JSON 结果；不会把原生对象或任意文件 API 传给页面。 */
  async handle(request: NativeRequest): Promise<unknown> {
    const parameters = request.parameters;
    switch (request.method) {
      case "folder.choose": {
        const window = this.window();
        if (!window) throw new Error("桌面窗口尚未就绪");
        const selected = await dialog.showOpenDialog(window, {
          title: "选择项目文件夹",
          properties: ["openDirectory"],
        });
        return selected.canceled ? null : selected.filePaths[0];
      }
      case "credential.write":
        await this.writeCredential(textParameter(parameters, "secret", 2560));
        return null;
      case "file.download":
        return await downloadFile(
          textParameter(parameters, "source", 4096),
          textParameter(parameters, "directory", 4096),
        );
      case "file.reveal": {
        const target = textParameter(parameters, "path", 4096);
        if (!path.isAbsolute(target)) throw new Error("文件定位路径无效");
        shell.showItemInFolder(target);
        return null;
      }
      case "external.open":
        await shell.openExternal(externalUrl(textParameter(parameters, "url")));
        return null;
    }
  }
}
