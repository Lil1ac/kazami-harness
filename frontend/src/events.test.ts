import { describe, expect, it } from 'vitest';
import { hasEventGap, mergeEvents, parseRunEvent } from './events';
import { makeEvent } from './test/fixtures';

describe('事件窗口', /* 验证跨页重叠、乱序及任务切换的真实输入边界。 */ () => {
  it('按运行和序号去重排序，拒绝其他任务', /* 返回确定顺序，避免重连重复显示。 */ () => {
    const result = mergeEvents(
      [makeEvent(2), makeEvent(1)],
      [makeEvent(2), makeEvent(3), makeEvent(4, 'old-run')],
      'run-a',
    );
    expect(result.map(/* 提取排序序号。 */ (event) => event.seq)).toEqual([1, 2, 3]);
  });
  it('限制内存窗口并保留最近事件', /* 长轨迹不会无限占用浏览器内存。 */ () => {
    expect(
      mergeEvents([], [makeEvent(1), makeEvent(2), makeEvent(3)], 'run-a', 2).map(
        /* 提取保留序号。 */ (event) => event.seq,
      ),
    ).toEqual([2, 3]);
  });
  it('缺口需要补齐，重复或紧邻事件不需要', /* 比较持久化游标。 */ () => {
    expect(hasEventGap(4, makeEvent(7))).toBe(true);
    expect(hasEventGap(4, makeEvent(4))).toBe(false);
    expect(hasEventGap(4, makeEvent(5))).toBe(false);
  });
  it('拒绝损坏正文、非法序号和旧任务消息', /* SSE 数据属于不可信输入。 */ () => {
    expect(parseRunEvent('{', 'run-a')).toBeNull();
    expect(parseRunEvent(JSON.stringify(makeEvent(1, 'old')), 'run-a')).toBeNull();
    expect(parseRunEvent(JSON.stringify(makeEvent(-1)), 'run-a')).toBeNull();
    expect(parseRunEvent(JSON.stringify(makeEvent(1)), 'run-a')?.seq).toBe(1);
  });
});
