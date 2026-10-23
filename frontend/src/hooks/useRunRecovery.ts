import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage, getRecovery, resumeRun } from '../api';
import type { ResumeRequest, RunView } from '../types';

/** 读取固定恢复载荷；仅校验存储格式，不把读取或刷新变成执行。 */
function readPending(runId: string): ResumeRequest | null {
  try {
    const value = JSON.parse(sessionStorage.getItem('harness-resume:' + runId) || 'null');
    const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
    if (uuid.test(value?.request_id || '') && uuid.test(value?.checkpoint_id || '')) return value;
  } catch {
    // 浏览器存储不可用或损坏时不自动提交，服务端仍独立核验请求。
  }
  return null;
}

/** 保存待确认载荷；只存公开编号，存储失败时仍保留当前页面内存状态。 */
function savePending(runId: string, body: ResumeRequest | null): void {
  try {
    const key = 'harness-resume:' + runId;
    if (body) sessionStorage.setItem(key, JSON.stringify(body));
    else sessionStorage.removeItem(key);
  } catch {
    // 存储限制不能把已经发生的提交显示为未发生。
  }
}

/** 管理某次运行的恢复条件与幂等提交；仅 resume 明确调用会 POST，onConfirmed 交付真实运行。 */
export function useRunRecovery(run: RunView, onConfirmed: (view: RunView) => void) {
  const cache = useQueryClient();
  const [pending, setPending] = useState<ResumeRequest | null>(
    /* 面板首次挂载只读草稿，即使有未知请求也等待用户点击确认。 */ () => readPending(run.run_id),
  );
  const inFlight = useRef(false); // 阻止同一事件循环中的重复按钮点击。
  const query = useQuery({
    queryKey: ['recovery', run.run_id],
    queryFn: /* 读取不会恢复工具或创建工作树。 */ ({ signal }) => getRecovery(run.run_id, signal),
    retry: false,
    refetchInterval: /* 活动运行和其他占槽运行结束后及时更新按钮，仅轮询 GET。 */ (state) =>
      run.active || state.state.data?.busy_run_id ? 1500 : false,
  });
  useEffect(
    /* 存储固定请求用于刷新后的显式确认，不发网络提交。 */ () => {
      savePending(run.run_id, pending);
    },
    [run.run_id, pending],
  );
  const mutation = useMutation({
    mutationFn: /* 明确重试时复用整个载荷，不自动改检查点或请求编号。 */ (body: ResumeRequest) =>
      resumeRun(run.run_id, body),
    retry: false,
    onSuccess: /* 服务确认后交付新尝试，源运行与历史消息不被覆盖。 */ (view) => {
      setPending(null);
      savePending(run.run_id, null);
      onConfirmed(view);
      void cache.invalidateQueries({ queryKey: ['recovery'] });
    },
    onError: /* 明确拒绝后释放编号；未知结果保留，避免网络重试重复执行。 */ (error) => {
      const detail = error as { status?: number; code?: string };
      if (
        detail.status &&
        detail.status >= 400 &&
        detail.status < 500 &&
        detail.code !== 'request_uncertain'
      )
        setPending(null);
      void cache.invalidateQueries({ queryKey: ['recovery', run.run_id] });
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
    onSettled: /* 仅在当前 HTTP 尝试结束后允许再次明确操作。 */ () => {
      inFlight.current = false;
    },
  });
  const confirming = pending !== null && !mutation.isPending;
  const canResume =
    !mutation.isPending &&
    (confirming ||
      Boolean(query.data?.can_resume && query.data.checkpoint && !run.active && !query.error));

  /** 发起新恢复或确认原请求；忙碌和无安全检查点时忽略，返回 void。 */
  function resume(): void {
    if (inFlight.current || !canResume) return;
    const body = pending || {
      request_id: crypto.randomUUID(),
      checkpoint_id: query.data!.checkpoint!.checkpoint_id,
    };
    inFlight.current = true;
    savePending(run.run_id, body);
    setPending(body);
    mutation.mutate(body);
  }
  return {
    view: query.data,
    reading: query.isPending,
    readError: query.error ? errorMessage(query.error) : null,
    submitError: mutation.error ? errorMessage(mutation.error) : null,
    submitting: mutation.isPending,
    confirming,
    canResume,
    resume,
    refresh: query.refetch,
  };
}
