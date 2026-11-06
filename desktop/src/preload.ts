import { contextBridge, ipcRenderer } from "electron";

// 沙箱预加载只暴露固定动作，不向页面提供 ipcRenderer、文件系统、进程或原生参数入口。
contextBridge.exposeInMainWorld("agentHarnessDesktop", {
  /** 请求主进程的正常退出流程，活动任务仍必须经过用户确认和清理。 */
  quit: (): void => ipcRenderer.send("desktop:quit"),
  /** 订阅已核对的自动化通知点击；只传通知和作业UUID，返回解除订阅函数。 */
  onAutomationNotification: (callback: (value: { id: string; jobId: string | null }) => void): (() => void) => {
    /** 丢弃原生事件，仅允许当前后台生成的固定记录引用。 */
    const listener = (_event: Electron.IpcRendererEvent, value: unknown): void => {
      if (!value || typeof value !== "object") return;
      const item = value as { id: unknown; jobId: unknown };
      if (typeof item.id === "string" && /^[0-9a-f-]{36}$/.test(item.id) && (item.jobId === null || typeof item.jobId === "string" && /^[0-9a-f-]{36}$/.test(item.jobId)))
        callback({ id: item.id, jobId: item.jobId });
    };
    ipcRenderer.on("desktop:automation-notification", listener);
    return /* 页面卸载不保留旧监听。 */ () => ipcRenderer.removeListener("desktop:automation-notification", listener);
  },
  /** 订阅服务中断的固定中文提示；callback 只接收文案，返回解除订阅函数。 */
  onServiceError: (callback: (message: string) => void): (() => void) => {
    /** 丢弃原始 Electron 事件对象，只把已检查的 message 传入渲染层。 */
    const listener = (
      _event: Electron.IpcRendererEvent,
      message: unknown,
    ): void => {
      if (typeof message === "string") callback(message);
    };
    ipcRenderer.on("desktop:service-error", listener);
    return /* 页面卸载时撤销当前监听，避免刷新后重复通知。 */ () =>
      ipcRenderer.removeListener("desktop:service-error", listener);
  },
});
