# 任务技能

技能采用 [Agent Skills 的 SKILL.md 格式](https://agentskills.io/specification)，用于复用任务流程、参考资料和文本模板。

## 管理与导入

打开「运行配置 → Skills」。这里汇总宿主登记的目录与导入副本，显示用途、来源、启停和校验结果。点开详情可查看正文及附属资料。选择「导入技能目录」，预览后确认。新导入的副本初始停用，核对后点击启用即可在任务中选用。

重新选择同名目录可以替换已导入的副本。确认绑定页面预览时的版本，其他窗口先更新时需重新预览。移除仅作用于受管副本。宿主 TOML 登记的技能在同一列表中预览及启停，内容由宿主目录维护。同名的宿主来源不能通过导入覆盖。启停和导入内容保存在运行数据目录的 `.work/work.sqlite3`，桌面、独立 Web 与 CLI 指向同一运行目录时共享管理状态。

## 宿主配置

```toml
skill_roots = ["examples/skills"]
skills = ["code-delivery"]
skill_auto = false
```

相对根目录以 TOML 文件所在目录为基准。根目录直接包含技能子目录，子目录名须与 frontmatter 的 `name` 一致。`skills` 是显式选择，`skill_auto` 控制默认是否让主 Agent 按任务匹配技能。CLI 使用 `--runs-dir` 指向对应运行数据目录。独立 Web 启动时通过 `--config` 指定运行配置。

`SKILL.md` 是 UTF-8 文件，以 YAML frontmatter 开始。`name` 长 1 至 64 字符，允许小写字母、数字和单连字符，`description` 长 1 至 1024 字符。支持 `license`、`compatibility`、`metadata`、`allowed-tools` 和布尔 `disable-model-invocation`。设 `disable-model-invocation: true` 后仅供手动选择。`allowed-tools` 作为技能元数据保留，实际工具权限、预算和验收由宿主决定。

## 选择和执行

1. 在新任务高级设置或「下一轮设置」中手选技能，也可打开「自动使用技能」。手选正文先进入输入。开启自动时，模型额外看到有界的可用名称和描述，匹配任务后调用 `activate_skill`。重复启用同一技能复用当时正文。
2. 已启用的技能可通过 `read_skill_resource` 读取 `references/` 和 `assets/` 下的 UTF-8 文本。资源使用相对技能目录的路径。越界、链接、重解析点、超限或版本变化会拒绝读取。浏览器导入范围为 `SKILL.md` 和这两个目录内的 UTF-8 文件。技能内提出的写入、网络和命令操作仍走各自授权入口。
3. 正文和已读资料进入同一固定上下文，压缩后仍保留并计入输入预算。普通续轮继承手选和自动开关，重新评估本轮所需的自动技能。动作批准、计划批准和安全暂停接续沿用已加载的原正文及资料。自动化和子 Agent 继续使用显式选定的正文。
4. 在运行详情「诊断 → 上下文与指令 → 本轮技能快照」查看触发方式、来源、版本、正文及已读资料。管理页的更新、停用或移除作用于后续可用性，历史直接读取当时的持久快照。

## 接口与限额

- `GET /api/options` 和 `GET /api/skills` 提供统一目录。`GET /api/skills/{id}` 和 `GET /api/skills/{id}/resource?path=…` 用于人工预览。
- `POST /api/skills/import` 接收显式选择的有界文本，替换时提交原管理版本。`POST /api/skills/{id}/actions` 接收版本及启用、停用或移除操作。
- `context_prepared` 固定初始技能，`skill_activated` 与 `skill_resource_read` 保存本轮按需加载的证据。手选失败保留原因并在模型调用前结束，自动启用和资料读取失败通过工具错误返回。
- 宿主最多登记 8 个集合根，合计扫描 256 个直接目录项。单文件上限为 64 KiB，导入包最多 64 个文件及 512 KiB。单轮最多启用 8 个技能，正文合计 256 KiB，实际读取资料最多 16 个及 128 KiB。初始自动目录上限为 8 KiB，整体模型输入预算仍可更严格。

## 2026-11-08 验收

在 Windows 浏览器中从管理页导入并启用 `code-delivery`，预览它的交付检查清单。开启自动使用后，离线测试模型按实际输入依次启用技能、读取清单，并调用真实文件工具修复样例加法。宿主独立检查经页面批准后通过，运行 `cd9aa11a-aced-464d-be6f-2767af91aa30` 保存自动启用及资料快照。 [管理页](../output/playwright/skills-v2/management.png) · [验收与历史](../output/playwright/skills-v2/history.png)

页面替换并停用副本后，新任务未加载技能。重启保留停用状态，重新启用后手选任务使用新版，原验收仍展示旧版正文和清单。管理页在 390 像素视口核对无横向溢出。 [窄屏管理页](../output/playwright/skills-v2/management-mobile.png)

后端专项 161 项通过、4 项平台跳过，覆盖非法及同名导入、过期确认、资料越界、Windows 文件身份、默认关闭、仅手动技能、按需模型输入、压缩和审批接续。首次较广回归的工作树用例受到 Windows 临时路径长度影响，在短路径复验 19 项通过。前端全量 235 项和最终相关 44 项通过，类型检查与生产构建通过，桌面回归 6 项通过。真实文件工具及验收命令用于验证工程链路，自动匹配在固定测试模型下核对。
