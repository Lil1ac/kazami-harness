import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage, getRuntime, pauseRuntime, sendRuntimeInput } from '../api';
import type { RuntimeInputRequest } from '../runtimeTypes';

/** 读取 runId 的未确认输入；损坏或不可用返回 null，不自动重新投递。 */
function readPending(runId: string): RuntimeInputRequest | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(`harness-runtime:${runId}`) || 'null');
    return typeof value?.request_id === 'string' &&
      typeof value?.prompt === 'string' &&
      ['steer', 'next'].includes(value?.delivery)
      ? value
      : null;
  } catch {
    return null;
  }
}

/** 在 runId 下保存 pending 的原载荷；null 清除，存储不可用仍保留组件内状态。 */
function writePending(runId: string, pending: RuntimeInputRequest | null): void {
  try {
    if (pending) sessionStorage.setItem(`harness-runtime:${runId}`, JSON.stringify(pending));
    else sessionStorage.removeItem(`harness-runtime:${runId}`);
  } catch {
    // 幂等事实仍在宿主；浏览器缓存异常不能自动生成新请求。
  }
}

/** 管理 runId 的运行中交互；active 控制实时刷新，历史读取不启动任务，返回输入及暂停操作。 */
export function useRuntimeControl(runId: string | undefined, active: boolean) {
  const cache = useQueryClient();
  const [delivery, setDelivery] = useState<'steer' | 'next'>('steer'); // 用户明确选择本轮或下一轮生效。
  const [pending, setPending] = useState<RuntimeInputRequest | null>(() =>
    runId ? readPending(runId) : null,
  ); // 未确认请求原文，错误后只确认同一载荷。
  const pauseId = useRef<{ runId: string; id: string } | null>(null); // 暂停重试保持同一编号。
  const sending = useRef(false); // 同一渲染间隔内双击也只接纳一次。
  useEffect(
    /* 切换真实执行段时加载其专属待确认请求，不混用不同轮次的编号。 */ () => {
      setPending(runId ? readPending(runId) : null);
    },
    [runId],
  );
  const query = useQuery({
    queryKey: ['runtime', runId],
    queryFn: /* 只读快照，暂停与恢复均需要明确操作。 */ () => getRuntime(runId!),
    enabled: Boolean(runId),
    refetchInterval: active ? 1000 : false,
    retry: false,
  });
  useEffect(
    /* 活动转为终态时立即核对暂停检查点，不能停留在旧的 running 缓存。 */ () => {
      if (runId) void cache.invalidateQueries({ queryKey: ['runtime', runId] });
    },
    [active, runId, cache],
  );
  const input = useMutation({
    mutationFn: /* 接口响应丢失时重试同一 body；不会改成新输入。 */ (body: RuntimeInputRequest) =>
      sendRuntimeInput(runId!, body),
    onSuccess: /* 只在宿主确认后删除缓存，刷新消息状态。 */ () => {
      setPending(null);
      writePending(runId!, null);
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
    },
    onError: /* 明确未接纳的客户端错误保留草稿并解除旧请求；网络及冲突仍确认原编号。 */ (error) => {
      const detail = error as { status?: number; code?: string };
      if (detail.status && detail.status >= 400 && detail.status < 500 && !['request_uncertain', 'request_conflict'].includes(detail.code || '')) {
        setPending(null);
        writePending(runId!, null);
      }
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
      void cache.invalidateQueries({ queryKey: ['run', runId] });
      void cache.invalidateQueries({ queryKey: ['conversation'] });
    },
  });
  const pause = useMutation({
    mutationFn: /* 暂停只请求停调度，不取消或重启命令。 */ () => {
      if (pauseId.current?.runId !== runId)
        pauseId.current = { runId: runId!, id: crypto.randomUUID() };
      return pauseRuntime(runId!, pauseId.current!.id);
    },
    onSuccess: /* 保存最新暂停意图；已暂停仍由后端安全收尾确认。 */ () => {
      void cache.invalidateQueries({ queryKey: ['runtime', runId] });
    },
  });

  /** 发送 text 或确认缓存载荷；返回是否已确认，失败保留原请求，不清空用户草稿。 */
  async function send(text: string): Promise<boolean> {
    if (!runId || sending.current || (!pending && !text.trim())) return false;
    const body = pending || { request_id: crypto.randomUUID(), prompt: text.trim(), delivery };
    sending.current = true;
    setPending(body);
    writePending(runId, body);
    try {
      await input.mutateAsync(body);
      return true;
    } catch {
      return false;
    } finally {
      sending.current = false;
    }
  }
  return {
    query,
    delivery,
    setDelivery,
    pending,
    send,
    sending: input.isPending,
    pause: pause.mutate,
    pausing: pause.isPending,
    error: input.error || pause.error ? errorMessage(input.error || pause.error) : null,
  };
}
