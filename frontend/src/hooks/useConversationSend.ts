import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createFollowup, errorMessage } from '../api';
import { useConfigDraft } from '../config';
import type { ConversationView, FollowupRequest, RunView } from '../types';

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

/** 当前标签页的会话草稿；未知发送状态刷新后只供用户确认，不自动执行。 */
interface SavedDraft {
  text: string; // 尚未发送或等待确认的可编辑文本。
  pending: PendingSend | null; // 无待确认请求时为空。
}

/** 读取会话草稿并兼容旧版逐运行待确认请求；存储损坏返回空草稿，不触发提交。 */
function readDraft(sessionId: string, runId: string): SavedDraft {
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
    const legacy = JSON.parse(sessionStorage.getItem(`harness-followup:${runId}`) || 'null');
    if (legacy && typeof legacy.prompt === 'string' && typeof legacy.request_id === 'string')
      return { text: legacy.prompt, pending: { parentId: runId, body: legacy } };
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

/** 清除旧版待确认键；仅在服务端接纳或明确拒绝后调用，存储失败不影响后台执行。 */
function clearLegacy(parentId: string): void {
  try {
    sessionStorage.removeItem(`harness-followup:${parentId}`);
  } catch {
    /* 存储被禁用时无需清理。 */
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
  const [initial] = useState(
    /* 首次挂载才读取存储，切换检查轮次不重置会话草稿。 */ () =>
      readDraft(sessionId, run?.run_id || conversation.latest_run_id),
  );
  const [text, setText] = useState(initial.text); // 同一会话内连续编辑，允许执行中预写下一条。
  const [pending, setPending] = useState<PendingSend | null>(initial.pending); // 只在明确确认后清除。
  const [skillChoice, setSkillChoice] = useState<SkillChoice | null>(null); // null 自动继承；[] 明确不加载。
  // 续轮默认读取最新真实配置；首轮页面的全局 draft.skills 不能覆盖继承结果。
  const skills =
    skillChoice?.parentId === conversation.latest_run_id ? skillChoice.names : run?.config.skills;
  const inFlight = useRef(false); // 同一事件循环中的连续 Enter 也只能开始一次发送。
  useEffect(
    /* 持久化当前标签页草稿；此 effect 从不发送网络请求。 */ () => {
      writeDraft(sessionId, { text, pending });
    },
    [sessionId, text, pending],
  );

  const mutation = useMutation({
    mutationFn: /* 无自动重试；确认旧请求时继续使用其原父轮和原载荷。 */ (
      submission: PendingSend,
    ) => createFollowup(submission.parentId, submission.body),
    onSuccess: /* 只有收到真实运行后才交付新轮；发送期间预写的新要求继续保留。 */ (
      view,
      submission,
    ) => {
      clearLegacy(submission.parentId);
      setPending(null);
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
    onError: /* 确定未接纳的4xx清除待确认编号；网络/不确定结果保留原编号供明确重试。 */ (
      error,
      submission,
    ) => {
      const detail = error as { status?: number; code?: string };
      if (
        detail.status &&
        detail.status >= 400 &&
        detail.status < 500 &&
        detail.code !== 'request_uncertain'
      ) {
        setPending(null);
        clearLegacy(submission.parentId);
      }
      void cache.invalidateQueries({ queryKey: ['conversation', sessionId] });
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
        text.trim(),
      ));

  /** 发送新要求或确认原请求；返回 void，忙碌和空输入直接忽略，不制造新编号。 */
  function send(): void {
    if (inFlight.current || !canSend) return;
    const { skills: _previousTaskSkills, ...overrides } = draft;
    const submission = pending || {
      parentId: conversation.latest_run_id,
      body: {
        request_id: crypto.randomUUID(),
        prompt: text.trim(),
        // 冻结完整配置，后续修改草稿或技能时不改变幂等重试的原请求。
        config: structuredClone({
          ...overrides,
          ...(skills !== undefined ? { skills } : {}),
        }),
      },
    };
    inFlight.current = true;
    setPending(submission);
    mutation.mutate(submission);
  }
  return {
    text,
    setText,
    skills: skills || [],
    setSkills: /* 当前最新轮下的明确选择；空列表也必须保留。 */ (names: string[]) =>
      setSkillChoice({ parentId: conversation.latest_run_id, names }),
    pending,
    sending: mutation.isPending,
    confirming,
    canSend,
    send,
    error: mutation.error ? errorMessage(mutation.error) : null,
  };
}
