import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createFollowup, errorMessage, getJob, getRun, getPermissions } from '../api';
import { useConfigDraft } from '../config';
import type {
  ActionDecision,
  PermissionMode,
  ConversationView,
  FollowupRequest,
  HumanDecision,
  RunView,
  JobView,
} from '../types';

/** 未确认的发送；父轮和完整载荷一起固定，重试只能确认同一请求。 */
export interface PendingSend {
  parentId: string; // 请求原本追加到的最新轮，即使之后发现新轮也不能改变。
  body: FollowupRequest; // 固定编号、正文及公开配置，不含模型凭据。
}

/** 对最新父轮的手动技能覆盖；新轮确认后默认沿用其真实配置。 */
interface SkillChoice {
  parentId: string; // 用户编辑时对应的最新轮编号。
  names: string[]; // 明确选择；空列表也代表一次有效覆盖。
}

/** 最新父轮下的修复选择；未编辑时继承服务端固定配置。 */
interface RepairChoice {
  parentId: string; // 选择只归属于当前最新轮，查看旧轮不改变它。
  enabled: boolean; // 显式开启或关闭，原请求重试始终使用冻结值。
}

/** 当前标签页的会话草稿；未知发送状态刷新后只供用户确认，不自动执行。 */
interface SavedDraft {
  text: string; // 尚未发送或等待确认的可编辑文本。
  pending: PendingSend | null; // 无待确认请求时为空。
}

/** 读取 sessionId 对应的会话草稿；存储损坏返回空草稿，不触发提交。 */
function readDraft(sessionId: string): SavedDraft {
  try {
    const value = JSON.parse(sessionStorage.getItem(`harness-chat:${sessionId}`) || 'null');
    if (value && typeof value.text === 'string') {
      const pending = value.pending;
      return {
        text: value.text.slice(0, 65536),
        pending:
          pending &&
          typeof pending.parentId === 'string' &&
          typeof pending.body?.request_id === 'string' &&
          typeof pending.body?.prompt === 'string'
            ? pending
            : null,
      };
    }
  } catch {
    // 浏览器存储不可用时仍允许内存中的正常会话操作。
  }
  return { text: '', pending: null };
}

/** 保存公开草稿；不把存储异常传播到交互，不修改已有服务端记录。 */
function writeDraft(sessionId: string, value: SavedDraft): void {
  try {
    if (value.text || value.pending)
      sessionStorage.setItem(`harness-chat:${sessionId}`, JSON.stringify(value));
    else sessionStorage.removeItem(`harness-chat:${sessionId}`);
  } catch {
    // 固定请求仍保留在内存；后端还会独立校验编号和会话最新轮。
  }
}

/** 在稳定 sessionId 下管理输入与幂等发送；run 为最新轮，确认后回调交付真实 RunView。 */
export function useConversationSend(
  sessionId: string,
  run: RunView | undefined,
  conversation: ConversationView,
  onConfirmed: (view: RunView) => void,
) {
  const { draft } = useConfigDraft();
  const cache = useQueryClient();
  const permissionRequest = run?.result.permissions.pending;
  const permissionStatus = useQuery({
    queryKey: ['permissions', run?.run_id, false, 0],
    queryFn: /* 只核对当前决定，防止取消排队后页面仍被旧快照阻塞。 */ () =>
      getPermissions(run!.run_id),
    enabled: Boolean(permissionRequest),
    refetchInterval: permissionRequest ? 2000 : false,
  });
  const currentPermission = permissionStatus.data?.items.find(
    /* 必须匹配同一动作，不用其他历史决定解锁。 */ (item) => item.id === permissionRequest?.id,
  );
  const permissionBlocked = Boolean(
    permissionRequest &&
    (!currentPermission ||
      ['pending', 'approved', 'executing', 'unknown'].includes(currentPermission.status) ||
      (currentPermission.status === 'expired' && !currentPermission.decision)),
  );
  const [initial] = useState(
    /* 首次挂载才读取存储，切换检查轮次不重置会话草稿。 */ () => readDraft(sessionId),
  );
  const [text, setText] = useState(initial.text); // 同一会话内连续编辑，允许执行中预写下一条。
  const [pending, setPending] = useState<PendingSend | null>(initial.pending); // 只在明确确认后清除。
  const [mode, setMode] = useState<'auto' | 'plan'>('auto'); // 只决定下一轮入口，不解除已存在的规划限制。
  const [permissionChoice, setPermissionChoice] = useState<{
    parentId: string;
    mode: PermissionMode;
  } | null>(null); // 选择只影响下一条普通消息。
  const permissionMode =
    permissionChoice?.parentId === conversation.latest_run_id
      ? permissionChoice.mode
      : run?.config.permission_mode || 'workspace_edit';
  const setPermissionMode = useCallback(
    /* 模式回调只随最新轮变化，输入文字不重建选择器。 */ (value: PermissionMode) =>
      setPermissionChoice({ parentId: conversation.latest_run_id, mode: value }),
    [conversation.latest_run_id],
  );
  const [skillChoice, setSkillChoice] = useState<SkillChoice | null>(null); // null 自动继承；[] 明确不加载。
  const [repairChoice, setRepairChoice] = useState<RepairChoice | null>(null); // null继承本轮，不读取全局设置草稿。
  const [memoryChoice, setMemoryChoice] = useState<{
    parentId: string;
    use: boolean;
    capture: boolean;
  } | null>(null); // 选择绑定最新父轮，重试保留已发送载荷。
  const memory =
    memoryChoice?.parentId === conversation.latest_run_id
      ? { use: memoryChoice.use, capture: memoryChoice.capture }
      : { use: run?.config.memory.use ?? true, capture: run?.config.memory.capture ?? true };
  const autoRepair =
    repairChoice?.parentId === conversation.latest_run_id
      ? repairChoice.enabled
      : run?.config.auto_repair;
  // 续轮默认读取最新真实配置；首轮页面的全局 draft.skills 不能覆盖继承结果。
  const skills =
    skillChoice?.parentId === conversation.latest_run_id ? skillChoice.names : run?.config.skills;
  const inFlight = useRef(false); // 同一事件循环中的连续 Enter 也只能开始一次发送。
  const [queuedJob, setQueuedJob] = useState<JobView | null>(null); // 已接纳作业，等待真正建立运行。
  useEffect(
    /* 持久化当前标签页草稿；此 effect 从不发送网络请求。 */ () => {
      writeDraft(sessionId, { text, pending });
    },
    [sessionId, text, pending],
  );

  const mutation = useMutation({
    mutationFn: /* 只投递一次；排队期间只读状态，刷新重试沿用原请求编号。 */ async (
      submission: PendingSend,
    ) => {
      let job = await createFollowup(submission.parentId, submission.body);
      setQueuedJob(job);
      while (job.status === 'queued' || job.status === 'preparing') {
        await new Promise<void>(
          /* 状态轮询不创建新作业。 */ (resolve) => setTimeout(resolve, 1000),
        );
        job = await getJob(job.id);
        setQueuedJob(job);
      }
      if (job.status === 'needs_review' || job.status === 'cancelled')
        throw new Error(job.error || '作业已取消。');
      if (job.status === 'failed' && job.execution_status === null)
        throw Object.assign(new Error(job.error || '动作尚未启动，请核对审批与配置。'), {
          status: 409,
          code: 'job_not_started',
        });
      return getRun(job.run_id);
    },
    onSuccess: /* 只有收到真实运行后才交付新轮；发送期间预写的新要求继续保留。 */ (
      view,
      submission,
    ) => {
      setPending(null);
      setQueuedJob(null);
      setPermissionChoice(
        /* 发送期间的新选择留给下一条，不能改变原请求或被新轮覆盖。 */ (choice) =>
          choice && choice.mode !== submission.body.config?.permission_mode
            ? { parentId: view.run_id, mode: choice.mode }
            : null,
      );
      setRepairChoice(
        /* 发送期间编辑的新选择留给下一条，重试确认不改变原授权。 */ (choice) =>
          choice && choice.enabled !== submission.body.config?.auto_repair
            ? { parentId: view.run_id, enabled: choice.enabled }
            : null,
      );
      setSkillChoice(
        /* 发送期间若已预写不同技能选择，将其留给下一条；否则继承已确认新轮。 */ (choice) =>
          choice && JSON.stringify(choice.names) !== JSON.stringify(submission.body.config?.skills)
            ? { parentId: view.run_id, names: choice.names }
            : null,
      );
      setText(
        /* 清空已发送正文，保留发送期间编辑的不同草稿。 */ (current) =>
          current.trim() === submission.body.prompt ? '' : current,
      );
      onConfirmed(view);
    },
    onError: /* 确定未接纳的4xx清除待确认编号；网络/不确定结果保留原编号供明确重试。 */ (error) => {
      const detail = error as { status?: number; code?: string };
      if (
        detail.status &&
        detail.status >= 400 &&
        detail.status < 500 &&
        detail.code !== 'request_uncertain'
      ) {
        setPending(null);
      }
      void cache.invalidateQueries({ queryKey: ['conversation', sessionId] });
      void cache.invalidateQueries({ queryKey: ['permissions'] });
    },
    onSettled: /* 无论响应成功或失败，都允许用户下一次明确操作。 */ () => {
      inFlight.current = false;
    },
  });
  const confirming = pending !== null && !mutation.isPending;
  const canSend =
    !mutation.isPending &&
    (confirming ||
      Boolean(
        conversation.can_continue &&
        run &&
        run.run_id === conversation.latest_run_id &&
        !run.active &&
        run.result.stop_reason !== 'user_paused' &&
        !permissionBlocked &&
        text.trim(),
      ));
  const canDecide = Boolean(
    !inFlight.current &&
    !pending &&
    !mutation.isPending &&
    conversation.can_continue &&
    run &&
    run.run_id === conversation.latest_run_id &&
    !run.active,
  );

  /** 发送新要求或确认原请求；返回 void，忙碌和空输入直接忽略，不制造新编号。 */
  function send(): void {
    if (inFlight.current || !canSend) return;
    const {
      skills: _previousTaskSkills,
      auto_repair: _previousRepairChoice,
      memory: _previousMemoryChoice,
      ...overrides
    } = draft;
    const submission = pending || {
      parentId: conversation.latest_run_id,
      body: {
        request_id: crypto.randomUUID(),
        prompt: text.trim(),
        ...(mode === 'plan' ? { mode } : {}),
        // 冻结完整配置，后续修改草稿或技能时不改变幂等重试的原请求。
        config: structuredClone({
          ...overrides,
          permission_mode: permissionMode,
          memory,
          ...(autoRepair !== undefined ? { auto_repair: autoRepair } : {}),
          ...(skills !== undefined ? { skills } : {}),
        }),
      },
    };
    inFlight.current = true;
    setPending(submission);
    mutation.mutate(submission);
  }

  /** 将用户action和回答answer绑定当前请求发送；无授权或已有不确定请求时返回而不发新编号。 */
  function decide(action: HumanDecision['action'], answer: string): void {
    const interaction = run?.result.planning.pending;
    if (inFlight.current || !canDecide || !interaction) return;
    const submission: PendingSend = {
      parentId: conversation.latest_run_id,
      body: {
        request_id: crypto.randomUUID(),
        prompt: answer,
        decision: {
          interaction_id: interaction.id,
          action,
          plan_version: interaction.plan_version,
        },
      },
    };
    inFlight.current = true;
    setPending(submission);
    mutation.mutate(submission);
  }

  /** 对最新等待动作提交action；只传固定编号和指纹，返回void，不允许同时修改配置或参数。 */
  function decideAction(action: ActionDecision['action']): void {
    const current = run?.result.permissions.pending;
    if (inFlight.current || !canDecide || !current || conversation.management.state !== 'active')
      return;
    const submission: PendingSend = {
      parentId: conversation.latest_run_id,
      body: {
        request_id: crypto.randomUUID(),
        prompt:
          action === 'approve'
            ? '批准本次动作'
            : action === 'reject'
              ? '拒绝本次动作'
              : '取消动作等待',
        action_decision: { action_id: current.id, fingerprint: current.fingerprint, action },
      },
    };
    inFlight.current = true;
    setPending(submission);
    mutation.mutate(submission);
  }

  /** 明确恢复安全暂停；创建新段且不提交配置覆盖，返回 void，未就绪时忽略。 */
  function resume(): void {
    if (
      inFlight.current ||
      pending ||
      !run ||
      run.active ||
      run.result.stop_reason !== 'user_paused' ||
      !conversation.can_continue
    )
      return;
    const submission: PendingSend = {
      parentId: conversation.latest_run_id,
      body: {
        request_id: crypto.randomUUID(),
        prompt:
          '恢复暂停前的工作。先核对当前代码、未完成任务及已保存执行证据；不重放旧工具，继续沿用原授权和剩余预算。',
        resume: true,
      },
    };
    inFlight.current = true;
    setPending(submission);
    mutation.mutate(submission);
  }
  return {
    resume,
    permissionBlocked,
    permissionMode,
    setPermissionMode,
    decideAction,
    memory,
    setMemory: /* 仅影响下一条普通消息，当前 Run 与原请求重试保持原值。 */ (choice: {
      use: boolean;
      capture: boolean;
    }) => setMemoryChoice({ parentId: conversation.latest_run_id, ...choice }),
    mode,
    setMode,
    canDecide,
    decide,
    text,
    setText,
    skills: skills || [],
    autoRepair: Boolean(autoRepair),
    setAutoRepair: /* 只设置下一条新要求，不能覆盖已经发出的请求。 */ (enabled: boolean) =>
      setRepairChoice({ parentId: conversation.latest_run_id, enabled }),
    setSkills: /* 当前最新轮下的明确选择；空列表也必须保留。 */ (names: string[]) =>
      setSkillChoice({ parentId: conversation.latest_run_id, names }),
    pending,
    queuedJob,
    sending: mutation.isPending,
    confirming,
    canSend,
    send,
    error: mutation.error ? errorMessage(mutation.error) : null,
  };
}
