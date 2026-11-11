import { useLayoutEffect, useMemo, useState } from 'react';
import type { KeyboardEvent, RefObject } from 'react';
import { Button, Input } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, CloseOutlined } from '@ant-design/icons';
import type { ConversationTurn } from '../types';
import './ConversationSearch.css';

/** 一条已显示消息的匹配位置；一条消息即使多处包含关键词也只占一个位置。 */
interface MessageMatch {
  runId: string; // 消息所属的稳定运行编号。
  part: 'prompt' | 'answer'; // 同一轮中定位到用户提问或最终回答。
}

/** 返回 turns 中包含 keyword 的消息，按出现顺序排列；被过程覆盖的活动回答暂不参与。 */
function findMatches(
  turns: ConversationTurn[],
  keyword: string,
  hiddenAnswerRunId?: string,
): MessageMatch[] {
  const needle = keyword.trim().toLowerCase();
  if (!needle) return [];
  const matches: MessageMatch[] = [];
  for (const turn of turns) {
    if (turn.prompt.toLowerCase().includes(needle))
      matches.push({ runId: turn.run_id, part: 'prompt' });
    if (turn.run_id !== hiddenAnswerRunId && turn.final_answer?.toLowerCase().includes(needle))
      matches.push({ runId: turn.run_id, part: 'answer' });
  }
  return matches;
}

/** 当前会话内即时查找，保留输入焦点并只滚动消息视口；关闭后移除匹配提示。 */
export function ConversationSearch({
  id,
  turns,
  hiddenAnswerRunId,
  viewport,
  onReveal,
  onClose,
}: {
  id: string; // 与顶栏查找按钮关联的区域标识。
  turns: ConversationTurn[]; // 已加载的用户消息及最终回答。
  hiddenAnswerRunId?: string; // 仍在执行或等待问题、页面不展示最终回答的运行。
  viewport: RefObject<HTMLDivElement | null>; // 对话独立滚动容器。
  onReveal: () => void; // 在浏览命中消息前暂停新消息跟随。
  onClose: () => void; // 关闭查找并将焦点交回顶栏入口。
}) {
  const [keyword, setKeyword] = useState(''); // 空查询不改变现有阅读位置。
  const [index, setIndex] = useState(0); // 当前匹配的零基顺序。
  const matches = useMemo(
    /* 重用已加载正文，无搜索网络请求。 */ () => findMatches(turns, keyword, hiddenAnswerRunId),
    [turns, keyword, hiddenAnswerRunId],
  );
  const position = matches.length ? Math.min(index, matches.length - 1) : 0;
  const runId = matches[position]?.runId;
  const part = matches[position]?.part;

  useLayoutEffect(
    /* 使用消息实际边界定位，不调整页面或侧栏滚动，不改变当前运行。 */ () => {
      const container = viewport.current;
      if (!container || !runId || !part) return;
      const turn = Array.from(container.querySelectorAll<HTMLElement>('[data-run-id]')).find(
        /* 只定位稳定轮次，用户输入不进入 CSS 选择器。 */ (node) => node.dataset.runId === runId,
      );
      const node = turn?.querySelector<HTMLElement>('[data-find-part="' + part + '"]');
      if (!node) return;
      onReveal();
      node.classList.add('conversation-search-hit');
      container.scrollTop +=
        node.getBoundingClientRect().top - container.getBoundingClientRect().top - 12;
      return /* 查找关闭或目标改变时撤销旧标记。 */ () =>
        node.classList.remove('conversation-search-hit');
    },
    [viewport, onReveal, runId, part, keyword],
  );

  /** 按 step 移动到上一个或下一个匹配，首尾循环；空结果保持原阅读位置。 */
  function move(step: -1 | 1): void {
    if (matches.length) setIndex((position + step + matches.length) % matches.length);
  }

  /** 处理查找栏的 Enter、Shift+Enter 和 Escape；输入法确认字符时不移动或关闭。 */
  function keyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
    } else if (event.key === 'Enter' && event.target instanceof HTMLInputElement) {
      event.preventDefault();
      move(event.shiftKey ? -1 : 1);
    }
  }

  return (
    <div
      id={id}
      className="conversation-search"
      role="search"
      aria-label="会话内查找"
      onKeyDown={keyDown}
    >
      <Input
        autoFocus
        size="small"
        value={keyword}
        aria-label="查找提问或回答"
        placeholder="查找提问或回答"
        maxLength={200}
        onChange={
          /* 修改关键词时回到第一条匹配。 */ (event) => {
            setKeyword(event.target.value);
            setIndex(0);
          }
        }
      />
      <span role="status" className="conversation-search-count">
        {keyword.trim() && (matches.length ? position + 1 + ' / ' + matches.length : '无匹配')}
      </span>
      <Button
        type="text"
        size="small"
        icon={<ArrowUpOutlined />}
        aria-label="上一处匹配"
        title="上一处匹配"
        disabled={!matches.length}
        onClick={/* 循环浏览上一条匹配消息。 */ () => move(-1)}
      />
      <Button
        type="text"
        size="small"
        icon={<ArrowDownOutlined />}
        aria-label="下一处匹配"
        title="下一处匹配"
        disabled={!matches.length}
        onClick={/* 循环浏览下一条匹配消息。 */ () => move(1)}
      />
      <Button
        type="text"
        size="small"
        icon={<CloseOutlined />}
        aria-label="关闭会话查找"
        onClick={onClose}
      />
    </div>
  );
}
