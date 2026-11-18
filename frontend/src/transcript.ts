import type { RunEvent } from './types';

/** 将同一 runId 的 incoming 事件并入可读过程；previous 是已折叠增量的投影，原始证据另存。
 * 文字按模型轮次与通道累积，完整消息覆盖增量；每个块保留 last_seq，断线补读不会重复文字。
 * 返回新数组，不修改输入。事件窗口裁剪不会影响已累计的正文；最多保留 1200 个过程块。
 */
export function projectTranscript(
  previous: RunEvent[],
  incoming: RunEvent[],
  runId: string,
): RunEvent[] {
  let items = previous.filter(/* 路由切换不能混入其他轮次。 */ (item) => item.run_id === runId);
  for (const event of incoming) {
    if (event.run_id !== runId) continue;
    const payload = event.payload;
    if (event.type === 'model_delta' || event.type === 'assistant_message') {
      const turn = payload.model_turn;
      if (typeof turn !== 'number' || !Number.isInteger(turn) || turn < 1) continue;
      const full = event.type === 'assistant_message';
      const message = payload.message as
        { content?: unknown; reasoning_content?: unknown; tool_calls?: unknown[] } | undefined;
      const channels = full ? ['text', 'reasoning'] : [String(payload.channel)];
      for (const channel of channels) {
        if (!['text', 'reasoning', 'tool_arguments'].includes(channel)) continue;
        const isTool = channel === 'tool_arguments';
        if (
          isTool &&
          (typeof payload.tool_index !== 'number' ||
            !Number.isInteger(payload.tool_index) ||
            payload.tool_index < 0)
        )
          continue;
        if (isTool && typeof payload.call_id === 'string' && items.some(
          /* 补读早期模型增量时让已保存的开始或结果继续覆盖占位。 */ (item) =>
            (item.type === 'tool_started' || item.type === 'tool_result') &&
            item.payload.call_id === payload.call_id && item.seq > event.seq,
        )) continue;
        const key = `${turn}:${channel}${isTool ? ':' + String(payload.tool_index) : ''}`;
        const index = items.findIndex(
          /* 同一内容块跨许多事件保持稳定身份。 */ (item) => item.payload.block_key === key,
        );
        const existing = items[index];
        if (existing && Number(existing.payload.last_seq) >= event.seq) continue;
        const value = full
          ? channel === 'text'
            ? message?.content
            : message?.reasoning_content
          : payload.delta;
        if (typeof value !== 'string' || (!value && !isTool)) continue;
        const text = full ? value : String(existing?.payload.text || '') + value;
        const block: RunEvent = {
          ...event,
          seq: existing?.seq ?? event.seq,
          type: isTool ? 'tool_preparing' : 'assistant_stream',
          payload: {
            block_key: key,
            last_seq: event.seq,
            model_turn: turn,
            channel,
            text,
            complete: full,
            has_tools: full ? Boolean(message?.tool_calls?.length) : existing?.payload.has_tools,
            call_id: payload.call_id ?? existing?.payload.call_id,
            name: payload.name ?? existing?.payload.name,
            arguments_json: isTool ? text : undefined,
          },
        };
        if (index < 0) items = [...items, block];
        else
          items = items.map(
            /* 仅替换正在变化的块，稳定其他块的对象引用。 */ (item, position) =>
              position === index ? block : item,
          );
      }
    } else if (
      [
        'tool_started',
        'tool_result',
        'verification_started',
        'verification_result',
        'repair_started',
        'run_finished',
      ].includes(event.type)
    ) {
      if ((event.type === 'tool_started' || event.type === 'tool_result') && typeof payload.call_id === 'string')
        items = items.filter(
          /* 截止或取消可直接收到跳过回执，也应替换该调用的参数生成占位。 */ (item) =>
            item.type !== 'tool_preparing' || item.payload.call_id !== payload.call_id,
        );
      if (
        !items.some(
          /* 原始操作按持久化序号幂等补读。 */ (item) =>
            item.type === event.type && item.seq === event.seq,
        )
      )
        items = [...items, event];
    }
  }
  return items.slice(-1200);
}
