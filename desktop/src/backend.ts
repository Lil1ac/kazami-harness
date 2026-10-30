import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { NativeHandler, nativeRequest } from "./protocol";

/** 后台启动输入；凭据只进入标准输入，不能拼进命令行、日志或公开设置。 */
export interface BackendOptions {
  executable: string; // 开发 Python 或打包后台可执行文件的绝对路径。
  args: string[]; // 固定模块及数据路径参数，不含任何密钥。
  cwd: string; // 与用户选择的任务目录无关的稳定后台启动目录。
  token: string; // 当前桌面实例的随机会话值。
  apiKey: string; // 主进程刚解密的模型凭据，可为空。
  logFile: string; // 后台标准错误输出的本机日志位置。
  native: NativeHandler; // 主进程的受控原生请求处理器。
  onUnexpectedExit: () => void; // 仅在非正常退出时通知窗口，不自动重放任务。
}

/** Electron 唯一后台进程；父进程通过关闭标准输入使后台按 Runner 生命周期退出。 */
export class BackendProcess {
  private child?: ChildProcessWithoutNullStreams; // spawn 成功后绑定，始终只管理自己的子进程。
  private originValue = ""; // 后台就绪后固定的本机随机端口地址。
  private stopping = false; // 主动退出期间不报告“后台异常”。
  private ended = false; // 子进程关闭后不再发送原生结果。
  private resolveExit!: () => void; // 退出完成通知，保证关闭窗口前可以等待。
  private readonly exited: Promise<void>; // 等待标准流关闭与子进程结束。

  /** 保存 options 并建立退出通知；构造不启动进程、不读取或记录密钥。 */
  constructor(private readonly options: BackendOptions) {
    this.exited = new Promise<void>(
      /* 保存一次性退出完成句柄，供 close 事件调用。 */ (resolve) => {
        this.resolveExit = resolve;
      },
    );
  }

  /** 返回就绪后的本机 origin；后台尚未就绪时抛固定错误，不能返回未经验证的地址。 */
  get origin(): string {
    if (!this.originValue) throw new Error("后台服务尚未就绪");
    return this.originValue;
  }

  /** 启动后台并最多等待20秒就绪；失败关闭输入使子进程清理，返回已验证的 origin。 */
  async start(): Promise<string> {
    if (this.child) throw new Error("后台已经启动");
    const environment: NodeJS.ProcessEnv = {
      ...process.env,
      PYTHONUTF8: "1",
      PYTHONUNBUFFERED: "1",
    };
    delete environment.DEEPSEEK_API_KEY;
    delete environment.ELECTRON_RUN_AS_NODE;
    this.child = spawn(this.options.executable, this.options.args, {
      cwd: this.options.cwd,
      windowsHide: true,
      stdio: "pipe",
      env: environment,
    });
    const child = this.child;
    const ready = new Promise<string>(
      /* 对就绪、错误和退出进行一次性配对，避免半启动窗口。 */ (
        resolve,
        reject,
      ) => {
        const timeout = setTimeout(
          /* 启动超时关闭输入，后台检测 EOF 后自行收尾。 */ () => {
            child.stdin.end();
            reject(new Error("后台启动超时，请检查配置和应用日志"));
          },
          20000,
        );
        const lines = createInterface({ input: child.stdout });
        lines.on(
          "line",
          /* 标准输出只接受受控协议，不打印可能携带密钥的原生参数。 */ (
            line,
          ) => {
            try {
              if (line.length > 65536) throw new Error("后台协议超过限制");
              const value = JSON.parse(line);
              if (
                value.type === "ready" &&
                Number.isInteger(value.port) &&
                value.port > 0 &&
                value.port <= 65535 &&
                !this.originValue
              ) {
                this.originValue = `http://127.0.0.1:${value.port}`;
                clearTimeout(timeout);
                resolve(this.originValue);
              } else if (value.type === "native") {
                void this.handleNative(nativeRequest(value));
              } else if (value.type === "error") {
                clearTimeout(timeout);
                reject(new Error("后台启动失败，请检查应用配置和日志"));
              } else {
                throw new Error("后台协议无效");
              }
            } catch {
              clearTimeout(timeout);
              child.stdin.end();
              reject(new Error("后台通信失败，请检查应用日志"));
            }
          },
        );
        child.once(
          "error",
          /* 入口缺失或无法启动时给出固定提示，不展示完整进程环境。 */ () => {
            clearTimeout(timeout);
            reject(new Error("无法启动 Python 后台，请检查安装文件和依赖"));
          },
        );
        child.once(
          "close",
          /* 等待输出关闭后完成退出，只有意外终止才通知当前桌面窗口。 */ () => {
            clearTimeout(timeout);
            this.ended = true;
            this.resolveExit();
            reject(new Error("后台在就绪前退出，请检查应用日志"));
            if (this.originValue && !this.stopping)
              this.options.onUnexpectedExit();
          },
        );
      },
    );
    child.stderr.on(
      "data",
      /* 后台日志与协议分离；HTTP 访问日志关闭，不记录会话令牌。 */ (
        chunk: Buffer,
      ) => {
        appendFileSync(this.options.logFile, chunk);
      },
    );
    child.stdin.on(
      "error",
      /* 后台提前退出时管道写入可能失败，统一由 close 处理。 */ () => {},
    );
    child.stdin.write(
      JSON.stringify({
        token: this.options.token,
        api_key: this.options.apiKey,
      }) + "\n",
    );
    this.options.apiKey = ""; // 传输后撤销启动选项中的明文副本。
    try {
      return await ready;
    } catch (error) {
      await this.stop();
      throw error;
    }
  }

  /** 执行后台已核对的 request，再回传同编号结果；失败只回固定错误码，避免泄漏参数。 */
  private async handleNative(
    request: ReturnType<typeof nativeRequest>,
  ): Promise<void> {
    let response: object;
    try {
      response = {
        type: "result",
        id: request.id,
        result: await this.options.native(request),
      };
    } catch {
      response = {
        type: "result",
        id: request.id,
        error: "native_operation_failed",
      };
    }
    if (!this.ended && !this.child?.stdin.destroyed && !this.child?.stdin.writableEnded)
      this.child?.stdin.write(JSON.stringify(response) + "\n");
  }

  /** 读取后台 path 的公开状态；请求使用主进程私有令牌，超时5秒，不向渲染层暴露令牌。 */
  async read<T>(path: string): Promise<T> {
    const response = await fetch(this.origin + path, {
      headers: { Authorization: `Bearer ${this.options.token}` },
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error("无法读取后台状态");
    return (await response.json()) as T;
  }

  /** 请求后台停止并等待任务取消、子进程清理及记录落盘；不会用强杀代替验收完成。 */
  async stop(): Promise<void> {
    if (!this.child || this.ended) return;
    this.stopping = true;
    if (!this.child.stdin.destroyed && !this.child.stdin.writableEnded)
      this.child.stdin.end(JSON.stringify({ type: "shutdown" }) + "\n");
    await this.exited;
  }
}
