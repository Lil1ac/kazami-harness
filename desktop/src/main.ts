import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  screen,
  session,
  shell,
} from "electron";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, appendFileSync } from "node:fs";
import path from "node:path";
import { BackendProcess } from "./backend";
import { NativeService, externalUrl } from "./native";
import {
  loadWindowState,
  saveWindowState,
  visibleWindow,
} from "./window-state";

const repository = path.resolve(__dirname, "../.."); // 开发源码根目录，打包版不依赖它定位后台。
const fixture = !app.isPackaged && process.argv.includes("--fixture"); // 仅开发模式可显式启用离线验收模块。

/** 读取命令行中的 name 绝对目录或文件；缺省返回 undefined，路径按启动终端解释。 */
function argument(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  if (!process.argv[index + 1] || process.argv[index + 1].startsWith("--"))
    throw new Error(`缺少 ${name} 参数`);
  return path.resolve(process.argv[index + 1]);
}

const dataDirectory =
  argument("--data-dir") ??
  (app.isPackaged
    ? existsSync("D:/")
      ? "D:/Codex/AgentHarness"
      : path.join(app.getPath("userData"), "AgentHarness")
    : path.join(
        repository,
        ".runtime",
        fixture ? "electron-fixture" : "desktop",
      )); // 稳定数据目录，开发缓存和发布版数据优先在 D 盘。
mkdirSync(dataDirectory, { recursive: true });
app.setPath("userData", dataDirectory);
mkdirSync(path.join(dataDirectory, "chromium"), { recursive: true });
mkdirSync(path.join(dataDirectory, "crash-dumps"), { recursive: true });
app.setPath("sessionData", path.join(dataDirectory, "chromium"));
app.setPath("crashDumps", path.join(dataDirectory, "crash-dumps"));
app.commandLine.appendSwitch(
  "disk-cache-dir",
  path.join(dataDirectory, "cache"),
);
const locked = app.requestSingleInstanceLock(); // 由 Electron 保证同一数据目录只有一个应用实例。
let window: BrowserWindow | undefined; // 唯一工作台窗口，关闭前始终保留以展示清理状态。
let backend: BackendProcess | undefined; // 当前窗口管理的后台进程。
let quitting = false; // 避免 close/before-quit/菜单重复触发确认和取消。
let readyToQuit = false; // 只有后台已退出且窗口状态保存后才允许原生退出。

/** 正常退出：运行中先确认，再等待后台完整收尾与窗口状态保存，最后退出应用。 */
async function requestQuit(): Promise<void> {
  if (quitting) return;
  quitting = true;
  try {
    const state = backend
      ? await backend
          .read<{ active_run_ids: string[]; queue_pending: number; scheduler: { active_jobs: string[] } }>(
            "/api/options",
          )
          .catch(/* 后台已停止时仍允许退出窗口。 */ () => null)
      : null;
    if ((state?.active_run_ids.length || state?.queue_pending || state?.scheduler.active_jobs.length) && window) {
      const answer = await dialog.showMessageBox(window, {
        type: "question",
        title: "退出 Agent Harness",
        message: "仍有运行或排队工作",
        detail:
          "退出将停止出队并取消活动运行，等待清理和记录保存；未执行请求保留，重启后按后台工作中的恢复设置处理。",
        buttons: ["继续运行", "取消任务并退出"],
        defaultId: 0,
        cancelId: 0,
      });
      if (answer.response !== 1) {
        quitting = false;
        return;
      }
    }
    window?.setTitle("Agent Harness · 正在保存并退出…");
    await backend?.stop();
    if (window && !window.isDestroyed())
      await saveWindowState(path.join(dataDirectory, "window.json"), {
        bounds: window.getNormalBounds(),
        maximized: window.isMaximized(),
      });
    readyToQuit = true;
    app.quit();
  } catch {
    quitting = false;
    dialog.showErrorBox(
      "退出未完成",
      "后台记录或窗口设置尚未完成保存，请检查数据目录权限后重试。",
    );
  }
}

/** 启动后台、建立私有 Cookie 并打开工作台；主进程管理原生能力，页面保持沙箱隔离。 */
async function start(): Promise<void> {
  const native = new NativeService(
    dataDirectory,
    /* 只在原生操作时取得当前窗口，不传给后台。 */ () => window,
  );
  const token = randomBytes(32).toString("hex");
  const runs =
    argument("--runs-dir") ??
    (!app.isPackaged && !fixture && !argument("--data-dir")
      ? path.join(repository, ".runs")
      : path.join(dataDirectory, "runs"));
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, "backend", "harness-backend.exe")
    : path.join(repository, ".venv", "Scripts", "python.exe");
  const args = app.isPackaged
    ? []
    : [
        "-X",
        "utf8",
        "-m",
        fixture
          ? "tests.helpers.desktop_fixture"
          : "agent_harness.desktop.server",
      ];
  args.push("--data-dir", dataDirectory, "--runs-dir", runs);
  const config = argument("--config");
  if (config) args.push("--config", config);
  const webConfig = argument("--web-config"); // 宿主调度容量与串行资源组；页面不能覆盖。
  if (webConfig) args.push("--web-config", webConfig);
  backend = new BackendProcess({
    executable,
    args,
    cwd: app.isPackaged ? dataDirectory : repository,
    token,
    logFile: path.join(dataDirectory, "backend.log"),
    native: /* 显式离线验收固定选择测试项目；正式应用始终打开系统对话框。 */ (
      request,
    ) =>
      fixture && request.method === "folder.choose"
        ? Promise.resolve(path.join(dataDirectory, "project"))
        : native.handle(request),
    onUnexpectedExit:
      /* 服务崩溃不自动重放任务，窗口保留历史和明确的失败提示。 */ () => {
        window?.webContents.send(
          "desktop:service-error",
          "本地执行服务已停止，请重新打开应用。已有任务不会自动重放。",
        );
      },
  });
  const origin = await backend.start();
  await session.defaultSession.cookies.set({
    url: origin,
    name: "harness_desktop",
    value: token,
    httpOnly: true,
    sameSite: "strict",
  });
  session.defaultSession.setPermissionRequestHandler(
    /* 工作台不需要摄像头、位置或其他浏览器权限。 */ (
      _contents,
      _permission,
      callback,
    ) => callback(false),
  );
  session.defaultSession.on(
    "will-download",
    /* 附件统一由受控宿主保存，取消浏览器隐式下载。 */ (event) =>
      event.preventDefault(),
  );
  const displays = [
    screen.getPrimaryDisplay(),
    ...screen
      .getAllDisplays()
      .filter(
        /* 优先使用主显示器，同时支持恢复到仍然存在的副屏。 */ (display) =>
          display.id !== screen.getPrimaryDisplay().id,
      ),
  ].map(
    /* 使用可用区域，避免标题栏被任务栏或屏幕边界遮住。 */ (display) =>
      display.workArea,
  );
  const preferences = visibleWindow(
    await loadWindowState(path.join(dataDirectory, "window.json")),
    displays,
  );
  window = new BrowserWindow({
    title: "Agent Harness",
    ...preferences.bounds,
    minWidth: Math.min(900, displays[0].width),
    minHeight: Math.min(620, displays[0].height),
    show: false,
    backgroundColor: "#f5f3ec",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  });
  window.once(
    "ready-to-show",
    /* 页面就绪后再显示，避免空白窗口闪烁。 */ () => {
      if (preferences.maximized) window?.maximize();
      window?.show();
    },
  );
  window.on(
    "close",
    /* 原生关闭必须进入统一的确认与后台收尾流程。 */ (event) => {
      if (!readyToQuit) {
        event.preventDefault();
        void requestQuit();
      }
    },
  );
  window.webContents.on(
    "will-navigate",
    /* 当前窗口只允许本机工作台；外部链接交给默认浏览器。 */ (event, url) => {
      if (new URL(url).origin !== origin) event.preventDefault();
    },
  );
  window.webContents.setWindowOpenHandler(
    /* 不创建具有本地会话的外部窗口。 */ ({ url }) => {
      try {
        void shell
          .openExternal(externalUrl(url))
          .catch(
            /* 默认应用无法打开时保留当前工作台并给出明确说明。 */ () =>
              dialog.showErrorBox(
                "链接未打开",
                "系统默认应用无法打开此链接，请检查默认浏览器设置。",
              ),
          );
      } catch {
        /* 非网页/邮件地址直接拒绝。 */
      }
      return { action: "deny" };
    },
  );
  ipcMain.on(
    "desktop:quit",
    /* 只接受当前窗口主框架的退出请求，不接受子框架或其他页面。 */ (event) => {
      if (
        event.sender === window?.webContents &&
        event.senderFrame?.url.startsWith(origin + "/")
      )
        void requestQuit();
    },
  );
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      {
        label: "文件",
        submenu: [
          {
            label: "退出",
            accelerator: "Alt+F4",
            click: /* 菜单退出也必须走同一清理流程。 */ () =>
              void requestQuit(),
          },
        ],
      },
      {
        label: "编辑",
        submenu: [
          { role: "undo" },
          { role: "redo" },
          { type: "separator" },
          { role: "cut" },
          { role: "copy" },
          { role: "paste" },
          { role: "selectAll" },
        ],
      },
      {
        label: "视图",
        submenu: [
          { role: "reload" },
          { role: "resetZoom" },
          { role: "zoomIn" },
          { role: "zoomOut" },
        ],
      },
    ]),
  );
  await window.loadURL(origin + "/runs");
}

if (!locked) {
  app.quit();
} else {
  app.on(
    "second-instance",
    /* 重复启动只聚焦当前窗口，不启动第二个任务运行器。 */ () => {
      if (window?.isMinimized()) window.restore();
      window?.show();
      window?.focus();
    },
  );
  app.on(
    "before-quit",
    /* 系统退出与原生窗口关闭使用同一后端清理路径。 */ (event) => {
      if (!readyToQuit) {
        event.preventDefault();
        void requestQuit();
      }
    },
  );
  app.on(
    "window-all-closed",
    /* 程序不在无窗口状态下默默继续执行任务。 */ () => {
      if (readyToQuit) app.quit();
    },
  );
  void app
    .whenReady()
    .then(start)
    .catch(
      /* 启动失败展示固定中文提示，原始信息只写不含凭据的失败标记。 */ async () => {
        appendFileSync(
          path.join(dataDirectory, "desktop.log"),
          "桌面启动失败，请检查后台日志、安装文件和设置权限。\n",
        );
        await backend?.stop();
        dialog.showErrorBox(
          "Agent Harness · 启动失败",
          "无法启动桌面工作台，请检查安装文件与应用数据目录下的 backend.log。",
        );
        readyToQuit = true;
        app.quit();
      },
    );
}
