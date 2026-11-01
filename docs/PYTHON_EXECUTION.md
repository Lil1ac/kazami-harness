# 当轮 Python 源码导入

模型命令和宿主验收使用当轮固定的项目源码目录，避免在副本修改代码、却导入 Harness 环境中原仓库的 editable 安装。实际工作目录与 Python 导入路径是两项独立信息。

## 1. 执行逻辑

任务接纳并确定实际目录后，生成 `TaskSpec.python_execution`，保存实际工作区、默认解释器与源码目录。Runner 在创建记录前核验；模型命令和宿主验收都将该轮项目源码目录加入各自子进程的 `PYTHONPATH`，不修改父进程环境。

默认把工作区根目录放在首位，并加入已存在的 `src`。模型执行普通 `python -m pytest` 即可使用该源码范围。命令的 `cwd` 可以切换到工作区子目录，源码范围仍以当轮工作区为准。

项目 python_executable 为显式解释器，空白按 PATH 查找；模型命令和项目验收的裸 python/python3 共用当轮选择，明确程序路径及 uv 等其他入口原样执行。相对路径按实际工作区解释，不自动创建环境或安装依赖。 PATH 未找到 Python 时 executable=null，允许其他语言任务；实际调用裸 Python 才明确失败。

## 2. 宿主项目配置

项目配置使用 `python_paths`，相对每轮实际工作区解析：

- 省略：根目录和已存在的 `src`。
- `python_paths = []`：仅工作区根目录。
- `python_paths = ["lib", "src"]`：根目录优先，随后加入显式目录，重复项去重；这些目录必须已经存在且位于工作区内。

该字段已替换原验收专用配置名，配置、目录登记和样例统一使用新字段，不提供旧字段映射。当前页面继续通过既有项目入口提交任务，源码路径属于宿主配置，网页不接受任意环境变量或宿主路径。

~~~toml
[[projects]]
id = "backend"
label = "后端项目"
path = "D:/prog/my-backend"
# 可省略这一行以自动选择根目录及已有 src。
python_paths = ["src"]
python_executable = "D:/prog/my-backend/.venv/Scripts/python.exe"
test_argv = ["D:/prog/my-backend/.venv/Scripts/python.exe", "-m", "pytest", "-q"]
~~~

CLI、普通文件夹及示例任务默认使用相同的根目录/src 规则，是否配置验收不影响模型命令的源码选择。CLI `show --json` 可查看落盘快照。

## 3. 多轮和路径边界

原目录会话根据同一实际目录固定新一轮配置；隔离会话复用同一工作树，新轮按该目录和当前项目配置固定路径；手动修改不会被旧补丁覆盖。项目配置改变只影响新轮，旧轮的清单和详情不会重新推断当前环境。

显式目录缺失或越界时，在调用模型前拒绝。每次启动子进程也重新核验已保存目录及解释器；目录消失、路径变化或越界时明确失败，不回退到另一份已安装源码。源码目录结构改变后，通过新一轮重新固定实际范围。

独立验收程序自身的 `VerifierSpec.python_paths` 继续限于验收工作目录，仅用于其私有依赖。它与共享项目源码分开校验、只追加到验收子进程，模型命令拿不到这组目录或验收规则。已有凭据过滤、命令超时、预算和清理机制继续生效。

## 4. 页面与记录

任务详情的“概况”显示“默认 Python”和“源码导入目录”。API `RunView.python_execution` 直接来自该轮持久记录；未准备的记录为 null，页面不使用当前服务的值补造历史。

该快照包括：

| 字段 | 含义 |
| --- | --- |
| `workspace` | 当轮实际工作区及源码边界 |
| `executable` | 项目设置或 PATH 解析的解释器；null 表示未找到 |
| `python_paths` | 有序的项目源码目录，根目录优先 |

实现主线：[执行快照](../src/agent_harness/execution/python.py) → [Runner](../src/agent_harness/runtime/runner.py) → [命令](../src/agent_harness/tools/command.py) / [验收](../src/agent_harness/runtime/verifier.py)。路径快照的结构校验不访问文件系统；执行前核验由执行器负责。

## 5. 本轮验收（2026-10-27）

先用选中副本的同名包复现了根目录及子目录命令误导入原仓库的问题；修复后，模型命令与宿主验收共用当轮源码快照。另覆盖宿主预先注入执行器时，清单中显式的 `lib` 目录必须覆盖执行器默认 `src`；执行器保存深副本，不能反向修改运行清单。

- 后端最终完整回归：638 项通过、10 项平台跳过，319.95 秒；跳过来自 Windows 符号链接权限及 POSIX FIFO。新增 21 项用例覆盖默认/显式源码范围、无验收运行、子目录命令、私有验收依赖、目录失效、原目录/隔离工作树的两轮接续及旧记录保持。
- 前端覆盖 21 个文件、99 项，以各文件最近一次有效结果统计均通过。首次两个工作进程全量中 6 项超时，其余 93 项通过；随后只重跑涉及的 2 个文件，19 项全部通过，未修改断言或超时上限。TypeScript 检查和生产构建通过，保留既有包体积提示。
- 真实页面提交使用 DeepSeek、正常命令工具和宿主项目验收，没有注入手工 PYTHONPATH、sys.path 或 pytest 导入覆盖参数。模型实际输出的 `agent_harness.__file__` 位于选中的验收副本 src 内；普通 `python -m pytest` 与宿主同名命令分别得到 125 passed、3 skipped。
- 运行 f8a64b52-f087-4515-bab6-5443dc8c10a7 最终 completed/passed，2 次模型轮次、3 次 HTTP 尝试（含一次重试）、2 次工具调用。该运行只核对导入与测试，未修改验收副本源码。重启专属服务后，原运行和执行快照保持，未追加模型任务。
- 页面“概况”展示当轮解释器和源码目录；旧轮深链接仍显示旧轮值。真实浏览器控制台 0 错误、0 警告。

原始记录：[后端完整回归](../.runtime/python-execution-20261027/backend-final.txt)、[前端首次全量](../.runtime/python-execution-20261027/frontend-full.txt)、[超时文件复验](../.runtime/python-execution-20261027/frontend-timeout-recheck.txt)、[真实运行事实](../.runtime/python-execution-20261027/live/proof.json)、[页面截图](../output/playwright/python-execution-20261027.png)。这些生成证据保留在本地忽略目录，不随 Git 自动提交。

本机项目登记文件已作一次性字段同步，原字节备份在 [.runtime/python-execution-20261027/main-catalog.before.json](../.runtime/python-execution-20261027/main-catalog.before.json)；项目编号、目录、测试入口及历史运行不变。产品代码不保留旧配置别名或迁移分支。已有其他配置文件中的旧字段需改为 `python_paths`，重启后端使新代码生效。

本次只证明同一工作区源码的执行与验收链路一致；前次真实任务的失败和人工接续仍完整保留在 [真实代码任务验收](REAL_TASK_ACCEPTANCE.md)。验收副本中的搜索工具补丁尚未合入原产品，本轮不声称复杂任务成功率或效率提升。

## 6. 项目解释器设置更新（2026-10-27）

所有真实项目均可在项目页“项目设置”保存 Python 解释器；保存只校验，不执行程序。相对路径以实际执行目录为准，隔离工作树不会自动复制被忽略的 .venv，缺失入口明确报错。需要共用已准备环境时可明确配置绝对路径。

CLI 可使用 --python-executable；验收清单中的裸 Python 与模型命令统一解析，私有验收依赖仍只进入验收子进程。本节更新先前宿主解释器默认策略，第5节保留当时真实验证记录。

本轮执行配置与会话工作树的最终验收于2026-10-28完成：后端652项通过、10项平台跳过，前端101项通过，真实页面三轮同目录、独立解释器和丢弃后历史读取均核对。完整事实见 [控制台验证记录](WEB_CONSOLE.md)。
