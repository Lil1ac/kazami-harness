/** 沙箱预加载公开的最小桌面接口；不提供原生路径、令牌、密钥或任意 IPC 调用。 */
interface AgentHarnessDesktopBridge {
  /** 请求正常退出；活动任务须由主进程确认取消后完成清理。 */
  quit(): void;
  /** 订阅固定的服务中断提示；callback 仅接收中文文案，返回取消订阅函数。 */
  onServiceError(callback: (message: string) => void): () => void;
  /** 订阅可信自动化通知点击；参数仅含通知/作业编号，返回解除订阅函数。 */
  onAutomationNotification?(callback: (value: { id: string; jobId: string | null }) => void): () => void;
}

interface Window {
  agentHarnessDesktop?: AgentHarnessDesktopBridge; // 只有 Electron 桌面窗口提供，独立浏览器缺省。
}
