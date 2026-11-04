# Windows 桌面端

Agent Harness 以 Electron 桌面工作台为主要入口，React/TypeScript 提供界面，Python/FastAPI/Runner 提供统一任务执行核心。CLI 适合脚本调用，独立网页服务保留为开发与调试入口。

## 启动

发布版通过安装程序创建桌面和开始菜单快捷方式，也可以解压便携 ZIP 后运行 `Agent Harness.exe`。主进程自动启动随包提供的 Python 后台，等待就绪后打开窗口。使用工作台本身不需要安装 Python、uv、Node.js 或 WebView2；项目任务需要的 Git、解释器和项目依赖仍由项目环境提供。

源码安装与首次构建按根 README 执行，之后运行：

```powershell
npm.cmd --prefix desktop run dev
```

修改前端后运行 `npm.cmd --prefix frontend run build`，再重新打开桌面端。主进程和预加载 TypeScript 会由开发命令自动编译。

可在命令后追加 `-- --data-dir D:\data\harness --runs-dir D:\data\runs --config D:\config\harness.toml`。路径始终在启动时固定，任务的执行目录由页面另行选择。相同数据目录重复启动只聚焦已有窗口。

## 架构与生命周期

```text
Electron 主进程 ── 标准输入/输出 ── Python 后台
    │ 原生窗口、目录选择、加密凭据           │ FastAPI / SSE / Runner
    └─ 沙箱预加载 + React ── 本机同源 HTTP ─┘
```

后台只绑定 `127.0.0.1`，由系统分配端口并持有监听 socket。每次启动生成独立令牌，通过标准输入提供给后台，页面使用主进程设置的 HttpOnly Cookie；令牌和密钥不进入命令行、URL、日志或运行记录。桌面模式不开放 Vite 的跨源修改入口。渲染层启用沙箱和上下文隔离、关闭 Node 集成，并限制页面导航、外链和浏览器权限。

原生能力只有目录选择、凭据保存、下载、文件定位和外链打开。后台先核对产物编号、快照路径与下载引用，再请求主进程操作，页面不能直接调用任意文件或命令 API。任务和验收子进程不继承父子控制输入管道；Windows 辅助控制台隐藏，原始输出照常保存。

运行中关闭窗口先确认“继续运行”或“取消任务并退出”。选择退出后，后台取消任务、清理命令进程并保存记录，完成后窗口退出。Electron 异常终止造成管道 EOF 时，后台也会主动收尾。服务中断不会自动重新提交任务；重新打开后读取既有记录，用户可按现有规则继续。

## 设置、凭据与文件

设置页保存模型、工具、总体预算和下载目录，后续新任务使用新默认值，活动任务保持原快照。继续旧会话时仍遵循该会话的配置继承规则，可按现有交互覆盖。

密钥使用 Electron `safeStorage` 的异步 API，通过操作系统密码体系加密后保存为 `credential.enc`；Windows 使用 DPAPI。这是本机用户级保护，不是对同一用户下恶意程序的隔离。系统加密不可用时明确失败，不退回明文。接口只返回是否已配置；CLI/独立网页继续使用各自进程的环境凭据。

| 内容 | 源码默认位置 | Windows 发布版默认位置 |
| --- | --- | --- |
| 公开设置、窗口状态、加密凭据 | `.runtime/desktop` | `D:\Codex\AgentHarness`，无 D 盘时使用用户应用数据目录 |
| 运行记录与最近项目 | 仓库 `.runs` | 应用数据目录下 `runs` |
| Chromium 缓存、后台日志 | 桌面数据目录 | 桌面数据目录 |
| 下载 | `D:\Codex\Downloads`，无 D 盘时为用户下载目录 | 同左，可在设置页修改 |

显式指定 `--data-dir` 后，默认记录目录为该目录下的 `runs`；也可用 `--runs-dir` 单独指定。下载重名时追加编号，保留已有文件。“在文件夹中显示”定位该轮固定产物快照，不执行文件。

## 打包与检查

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
npm.cmd --prefix desktop test
uv --cache-dir .uv-cache run python -m pytest tests/desktop -q
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
```

构建先生成前端和独立 Python 后台，再由 electron-builder 生成 Windows x64 安装程序和 ZIP。输出在 `dist/desktop`，后台和 Electron 下载、构建缓存位于仓库 `.runtime`。发布目录中的 `win-unpacked` 可直接运行；分发时应保留整个目录或使用 ZIP/安装程序。当前构建没有发布者代码签名或自动更新服务。

显式离线窗口验收使用 `npm.cmd --prefix desktop run dev -- --fixture`，数据位于 `.runtime/electron-fixture`，模型和目录选择只指向专属测试项目，文件、命令、项目测试、规划批准及运行记录仍真实执行。该入口仅开发模式有效，不代表真实模型的任务成功率。

## 本轮验收记录（2026-10-30）

| 检查 | 执行结果 |
| --- | --- |
| Python 全套基线 | 695 通过、10 跳过；跳过项为当前 Windows 的符号链接或 POSIX 能力限制 |
| 前端全套回归 | 28 个文件、127 项通过；TypeScript 类型检查和生产构建通过 |
| Electron 父子进程与基础规则 | 5 项通过，贯通真实 Python 后台、规划批准、文件/命令工具、项目测试、下载和重启历史 |
| Python 桌面接口与生命周期 | 7 项通过，覆盖认证、公开设置、原生协议、管道失联、活动任务取消落盘和打包宿主解释器边界 |
| 真实 Windows 系统加密 | Electron 中保存、解密重读、清除虚构密钥均通过，落盘不含明文 |
| 真实 Electron 窗口 | 重启恢复待批准会话；批准前不生成结果，批准后写文件并运行检查命令，文件差异和下载成功 |
| 设置与正常退出 | 设置页保存测试下载目录并写盘；关闭窗口后后台完整退出，窗口状态保存 |
| Windows 发布包 | 从非源码工作目录启动随包后台和界面；重复启动未创建第二个后台；系统目录选择取消后项目数仍为0，关闭后后台进程退出 |

窗口验收使用专属离线项目，首轮 `665139b1-0970-4017-b450-e0fbe19b6bc3` 提交方案后等待批准，重启后在界面批准，第二轮 `dceefb83-3178-497e-a38f-6fa10cb658c0` 生成 `result.txt`。该窗口场景执行了真实文件内容检查命令，没有配置独立项目测试，界面正确显示“完成 · 未测试”；独立项目测试通过的链路由 Electron 集成测试单独覆盖。没有调用真实模型或使用用户真实密钥。

实际窗口发现并修复了首次历史已含 `run_finished`、会话缓存仍显示忙碌的竞态；历史补齐现在主动刷新会话，新回归用例验证不依赖已错过的 SSE 结束通知。

发布版首次启动的缺少密钥提示直接链接“运行配置”，保存后即可使用，不要求终端环境变量或重启。修正后首页相关2项测试及类型检查通过，重新生成发布包。安装程序与便携 ZIP 已构建；本轮实际启动验收使用同一构建的 `win-unpacked` 可执行程序，没有执行安装或卸载流程。

打包复核修正了模型系统提示中的 Python 来源：提示现在读取本轮项目执行快照，未找到时明确别名不可用，不再把 Harness 后台 EXE 当成项目 Python。桌面、上下文、预算、真实命令和压缩相关回归97项通过、3项平台跳过；另有11项上下文/压缩 API 测试及5项 Electron 测试通过。该后端修正没有改变 HTTP 字段或页面交互；前端项目设置与执行快照展示已经使用相同的项目解释器。预算测试辅助函数也按 Runner 固定解释器后计量，保留原有精确边界断言。

本机证据（生成目录不纳入 Git）：`.runtime/electron-backend-tests.log`、`.runtime/electron-frontend-final.log`、`.runtime/electron-onboarding-tests.log`、`.runtime/electron-interpreter-tests.log`、`.runtime/electron-context-api-tests.log`、`.runtime/electron-host-tests.log`、`.runtime/electron-package-final.log`；截图为 `output/playwright/electron-approved-result.png`、`output/playwright/electron-file-download.png` 与 `output/playwright/electron-packaged-start.png`。

## 提交前整体验证（2026-10-30）

本次在主动规划、上下文投影与 Markdown 导出已合入后，检查全部剩余桌面及相关改动，并重新验证组合后的实现：

- 后端全量：709 项通过、10 项因 Windows 符号链接或 POSIX 能力限制跳过，零失败。
- 前端串行全量：29 个文件、136 项通过；类型检查和生产构建通过。保留既有 Vite 大包提示及 jsdom 的 CSS、伪元素和页面导航提示。
- Electron 主进程及真实 Python 后台集成：5 项通过，覆盖规划批准、真实工具与项目测试、下载和重启历史。
- 真实 Electron 系统加密：虚构密钥保存、解密重读、清除通过；不读取用户凭据。
- Python 锁文件离线一致性校验通过。当前源码的 PyInstaller 后台、Windows x64 安装程序和便携 ZIP 均重新构建成功。
- 刚打包的后台从非源码临时目录启动，未认证请求返回403，认证后能力与界面资源正常，空凭据可查看工作台，正常退出码为0；没有创建模型任务。

本次未执行安装或卸载。代码、测试、公开设置与锁文件一并提交；依赖、缓存、运行记录、测试日志和发布二进制仍按仓库忽略规则保存。原始日志为 `.runtime/desktop-commit-backend-full.txt`、`.runtime/desktop-commit-frontend-full.txt`、`.runtime/desktop-commit-electron-tests.txt`、`.runtime/desktop-commit-native-smoke.txt`、`.runtime/desktop-commit-backend-package.txt`、`.runtime/desktop-commit-package.txt`；独立打包启动检查见 `.runtime/desktop-commit-20261030/packaged-smoke.json`。

## 任务和队列接入（2026-10-31）

桌面与网页复用统一作业接纳和任务管理 API。主进程退出核对活动运行及未执行队列，先停止出队，再等待取消、进程清理和记录保存；未执行请求持久保留，重启后从后台中心明确恢复。任务完成和检查结果分别展示，任务/作业状态通过事务事件及只读轮询观察。相关协议及验收见[任务说明](TASKS.md)。


项目记忆 Markdown 导出沿用统一 `/api/desktop/download` 入口，宿主重新核验项目登记与受保护缓存后交给原生下载，使用已配置目录。跨源地址、附加查询及目录穿越拒绝；记忆的来源和运行快照协议见[项目记忆](MEMORY.md)。本轮只做宿主协议和组件联动回归，没有把这些证据写成安装包验收。


## 2026-11-04 隔离开发联动

开发子项的报告、固定补丁、文件产物与命令输出沿用 Web/桌面共用界面。原生保存通过已登记附件入口携带 parent_run_id 核验归属，不新增 IPC 或任意文件读取；联合候选采纳复用 C13。详细语义见[写入型子 Agent 与成果集成](DEVELOPMENT_SUBAGENTS.md)。
