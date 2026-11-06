import type { CreateRunRequest, RunConfig } from './types';

/** 持久规则的时间或本机事件条件；星期数字遵循APScheduler周一为0，推荐名称。 */
export interface AutomationSchedule {
  kind: 'once' | 'interval' | 'cron' | 'event'; // 互斥触发类型。
  timezone: string; // IANA时区，默认Asia/Shanghai。
  at: string | null; // 一次性带偏移时间，其他类型为空。
  interval_minutes: number | null; // 固定间隔分钟，至少一分钟。
  cron: string | null; // 五段Cron，推荐mon-fri。
  event: 'completed' | 'verification_failed' | null; // 同项目普通主运行终态。
  catch_up: boolean; // 是否在窗口内只补最新一次。
  catch_up_minutes: number; // 补跑窗口分钟，默认60，上限10080。
}

/** 经用户确认的自动化模板；单次触发不复用前次会话上下文。 */
export interface AutomationDefinition {
  name: string; // 用户可辨识名称。
  task: CreateRunRequest; // 既有公开运行请求，不允许宿主配置或凭据。
  schedule: AutomationSchedule; // 时间或事件触发。
  overlap: 'skip' | 'coalesce'; // 同规则已有活动执行时跳过或合并一个请求。
}

/** 自动化列表与详情共用的公开规则视图。 */
export interface AutomationRule {
  id: string; // 稳定规则UUID。
  revision: number; // 编辑及人工操作绑定的版本。
  definition: AutomationDefinition; // 当前模板。
  enabled: boolean; // 是否允许后续触发。
  invalid_reason: string | null; // 来源或配置失效原因。
  has_grant: boolean; // 是否已保存持续授权，仍受宿主上限约束。
  grant_generation: number; // 撤销或重新审核后递增，旧执行不能沿用新许可。
  next_at: string | null; // 最近一次尚未处理的计划时间，事件为空。
  next_times: string[]; // 未来最多五次带时区时间。
  history_versions: { revision: number; definition: AutomationDefinition; updated_at: string }[]; // 旧定义只供审阅。
}

/** 一次触发的持久事实；跳过、合并及失败也保存，不以模型回答推断成功。 */
export interface AutomationFire {
  id: string; // 触发UUID，也是唯一首次作业请求编号。
  status: string; // 触发状态或真实作业状态。
  scheduled_at: string; // 原计划时间。
  reason: string | null; // 跳过、合并或故障原因。
  job_id: string | null; // 未接纳前为空。
  run_id: string | null; // 对应当前最新审批接续运行。
  validation_status?: string; // 未建立运行前不猜测验收结论。
  source_run_id: string | null; // 本机事件来源，定时及手动为空。
}

/** 持久通知与桌面弹窗共用的可信结果引用。 */
export interface AutomationNotification {
  id: string; // 唯一通知编号。
  rule_id: string; // 对应规则。
  name: string; // 规则名称。
  state: string; // 完成、需要处理、失败或恢复。
  message: string; // 宿主固定状态说明，不采用模型生成的操作链接。
  read: boolean; // 用户明确读取状态。
  count: number; // 合并的重复故障次数。
  job_id: string | null; // 可定位后台作业。
  run_id: string | null; // 已建立时可定位结果。
}

/** 授权审核快照；完整内容不含模型凭据。 */
export interface AutomationReview {
  revision: number; // 审核绑定的版本。
  fingerprint: string; // 定义、配置、项目和入口内容摘要。
  path: string; // 原登记项目目录。
  config: RunConfig; // 将使用的完整有效配置。
  verifier: { argv: string[]; cwd: string } | null; // 固定验收入口。
  hooks: {
    key: string;
    trusted: boolean;
    enabled: boolean;
    fingerprint: string;
    rule: { argv: string[] };
  }[]; // 可审核的Hook定义。
  grant: {
    commands: { argv: string[]; cwd: string }[];
    verification: boolean;
    hooks: string[];
  } | null; // 当前已保存的范围。
}

/** 页面保存的精确授权；命令必须是argv数组，不能用shell前缀匹配。 */
export interface AutomationGrant {
  revision: number; // 当前版本。
  fingerprint: string; // 当前审核指纹。
  commands: { argv: string[]; cwd: string }[]; // 参数、目录及实际入口身份共同绑定。
  verification: boolean; // 只允许当前固定验收器。
  hooks: string[]; // 已审核且启用的Hook编号。
}

/** 自动化管理页的列表响应。 */
export interface AutomationList {
  items: AutomationRule[]; // 全部规则，最多一百条。
  error: string | null; // 触发服务故障；不伪装为正常空列表。
  grants_allowed: boolean; // 宿主是否显式开启持续授权。
  queue_paused: boolean; // 原队列暂停时自动化也不能绕过。
}
