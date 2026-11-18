# 多轮需求变更题

四题取材于本机 Anaconda Python 3.11.5 随附的 CPython 标准库。首轮需求、末轮变更和判定条件为本轮制定，证据和调研材料取自安装目录中的原始源码。

| 模块 | 首轮需求 | 末轮变更 |
| --- | --- | --- |
| textwrap | 对第一条选中行指定单独前缀 | 遇到空白行按段重启，保留谓词及换行契约 |
| fnmatch | 从主匹配结果排除指定模式 | 用例外模式恢复原先符合主条件的名称 |
| heapq | 流式合并按排序键保留第一项 | 可选同键最后一项，保持尾段惰性 |
| configparser | 列出本节键并保留默认值插值 | 显示临时覆盖键，保留键顺序和规范化语义 |

`cases.py` 登记冻结题目和材料路径。`reference_solutions.py` 保存各题的参考改动，先核对修改目标在原文中唯一，再在内存应用替换。`precheck.py` 通过原版与参考版本分别运行同一套公开行为断言。原版的兼容行为应通过，首轮和末轮各自因缺少对应功能而失败。参考版本三组断言均应通过。报告另核验早期证据在已选材料中的唯一性及每题历史材料字节数。

在仓库根目录运行以下命令，可重新生成报告。

```powershell
python -B evals/context-checkpoint-20261117/multiturn/precheck.py --output evals/context-checkpoint-20261117/multiturn/precheck.json
```

报告包含原始文件 SHA-256、参考文本的 UTF-8 SHA-256 和逐组结果。预检在独立模块作用域编译原件和参考文本，全程不写入标准库文件，模型请求为零。参考解与断言保留在宿主侧。

## 对实际候选评分

传入当前试次的标准库副本根目录，其中应当含有题目对应的模块文件。批次可以使用 Python 接口 `multiturn.precheck.grade_candidate(case_id, candidate)`，也可以启动独立进程。

```powershell
python -B evals/context-checkpoint-20261117/multiturn/precheck.py --case multiturn-textwrap-indent --candidate <试次标准库根目录> --output <评分报告.json>
```

评分使用原有三组行为断言，仅加载候选文件。JSON 中的 `passed` 表示全部组是否通过。`status` 区分通过（`passed`）、断言组失败（`failed`）、候选文件或加载异常（`candidate_error`）与评分环境异常（`scorer_error`）。进程退出码分别为 0、1、1、2。可加载的候选会保存文件 SHA-256、逐组结果及通过数。
