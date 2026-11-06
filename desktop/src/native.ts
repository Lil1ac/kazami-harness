import { dialog, safeStorage, shell, BrowserWindow, Notification } from "electron";
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
  private readonly mcpCredentialDirectory: string; // 每个连接独立密文，不与模型密钥混用。
  private credentialWrite: Promise<void> = Promise.resolve(); // 串行保存，避免并发替换同一密文文件。
  private readonly notifications = new Map<string, Notification>(); // 保持活动弹窗引用，关闭后释放，防止原生对象提前回收。

  /** 固定 dataDirectory 与当前窗口读取器 window；构造不创建对话框或读取密钥。 */
  constructor(
    dataDirectory: string,
    private readonly window: () => BrowserWindow | undefined,
  ) {
    this.credentialFile = path.join(dataDirectory, "credential.enc");
    this.mcpCredentialDirectory = path.join(dataDirectory, "mcp-credentials");
  }

  /** 将可选 key 转为固定密文路径；undefined 使用模型密钥，其他值须为 MCP 标识，不允许路径穿越。 */
  private credentialPath(key?: string): string {
    if (key === undefined) return this.credentialFile;
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(key)) throw new Error("外部凭据标识无效");
    return path.join(this.mcpCredentialDirectory, key + ".enc");
  }

  /** 解密可选 key 分区；undefined 读取模型密钥，其他值为连接标识。未配置返回空字符串，加密不可用抛错。 */
  async readCredential(key?: string): Promise<string> {
    const file = this.credentialPath(key); // key 缺省读取模型，非空读取独立外部连接。
    const encrypted = await readOptional(file);
    if (!encrypted) return "";
    if (!(await safeStorage.isAsyncEncryptionAvailable()))
      throw new Error("本机凭据加密服务不可用");
    const result = await safeStorage.decryptStringAsync(encrypted);
    if (result.shouldReEncrypt)
      await atomicWrite(
        file,
        await safeStorage.encryptStringAsync(result.result),
      );
    return result.result;
  }

  /** 保存 secret 到可选 key 分区；undefined 为模型，空 secret 清除该分区。仅写密文，失败保留原值。 */
  async writeCredential(secret: string, key?: string): Promise<void> {
    const file = this.credentialPath(key); // 空 secret 清除此分区，其他分区保持。
    const operation = this.credentialWrite.then(
      /* 同一时刻只允许一个密文保存或删除操作。 */ async () => {
        if (!secret) {
          await removeOptional(file);
          return;
        }
        if (!(await safeStorage.isAsyncEncryptionAvailable()))
          throw new Error("本机凭据加密服务不可用");
        await atomicWrite(
          file,
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
      case "notification.show": {
        const id = textParameter(parameters, "id", 36);
        const jobId = parameters.job_id == null ? null : textParameter(parameters, "job_id", 36);
        if (!/^[0-9a-f-]{36}$/.test(id) || (jobId !== null && !/^[0-9a-f-]{36}$/.test(jobId)))
          throw new Error("通知引用无效");
        if (!Notification.isSupported()) return { supported: false };
        const notice = new Notification({ title: textParameter(parameters, "title", 100), body: textParameter(parameters, "body", 500) });
        this.notifications.set(id, notice);
        notice.on("click", /* 点击仅传固定记录编号，渲染层不能从通知执行任意链接。 */ () => {
          const window = this.window();
          window?.show();
          window?.focus();
          window?.webContents.send("desktop:automation-notification", { id, jobId });
        });
        notice.on("close", /* 仅清理本次引用，较新同编号弹窗不受影响。 */ () => {
          if (this.notifications.get(id) === notice) this.notifications.delete(id);
        });
        notice.show();
        return { supported: true };
      }
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
      case "mcp.credential.read":
        return await this.readCredential(textParameter(parameters, "key", 32));
      case "mcp.credential.write":
        await this.writeCredential(textParameter(parameters, "secret", 2560), textParameter(parameters, "key", 32));
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
