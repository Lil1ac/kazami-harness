import { act, renderHook, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { ConfigDraftProvider, useConfigDraft } from '../config';
import { createFollowup } from '../api';
import { makeRun } from '../test/fixtures';
import type { ConversationView, RunView } from '../types';
import { useConversationSend } from './useConversationSend';

vi.mock(
  '../api',
  /* 仅替换 HTTP 提交，技能继承、冻结与状态流转仍使用真实实现。 */ () => ({
    createFollowup: vi.fn(),
    errorMessage: /* 测试只保留受控文本。 */ (error: unknown) =>
      error instanceof Error ? error.message : '请求失败',
  }),
);

/** 构造可继续会话，latest 为真实最新轮 ID，不读取远端服务。 */
function conversation(latest = 'run-a'): ConversationView {
  return {
    session_id: 'run-a',
    latest_run_id: latest,
    can_continue: true,
    blocked_code: null,
    blocked_reason: null,
    busy_run_id: null,
    turns: [],
  };
}

/** 注入旧首轮草稿，用于验证续轮优先采用最新真实选择；children 是实际 hook 宿主。 */
function DraftPrimer({ children }: { children: ReactNode }) {
  const { setDraft } = useConfigDraft();
  useEffect(
    /* 只在首次挂载时设置原页面遗留的技能覆盖。 */ () => {
      setDraft({ skills: ['stale-first-run'], limits: { max_model_turns: 7 } });
    },
    [setDraft],
  );
  return children;
}

/** 包装真实查询与草稿上下文；返回跟踪确认回调和支持切换最新轮的 hook。 */
function setup(run = makeRun({ config: { ...makeRun().config, skills: ['current-skill'] } })) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const confirmed = vi.fn();
  /** 为测试 hook 提供同一组件树；查询与草稿状态不会因 rerender 丢失。 */
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <ConfigDraftProvider>
          <DraftPrimer>{children}</DraftPrimer>
        </ConfigDraftProvider>
      </QueryClientProvider>
    );
  }
  return {
    confirmed,
    ...renderHook(
      /* 只有调用 send 才发出请求；读取新 run 不执行模型任务。 */ ({
        latest,
      }: {
        latest: RunView;
      }) => useConversationSend('run-a', latest, conversation(latest.run_id), confirmed),
      { wrapper: Wrapper, initialProps: { latest: run } },
    ),
  };
}

beforeEach(
  /* 每例清除待确认状态，不复用前次幂等请求。 */ () => {
    vi.resetAllMocks();
    sessionStorage.clear();
  },
);

it('续轮选择默认继承当前真实配置，清空覆盖不会恢复旧首轮草稿', async () => {
  // 当前已选技能具有优先级；其余显式预算覆盖仍然有效。
  vi.mocked(createFollowup).mockRejectedValue(
    Object.assign(new Error('明确拒绝'), { status: 409 }),
  );
  const { result } = setup();
  expect(result.current.skills).toEqual(['current-skill']);
  act(
    /* 编辑文本与明确清空选择，尚不发送。 */ () => {
      result.current.setText('不使用技能继续');
      result.current.setSkills([]);
    },
  );
  act(/* 用户明确发送后固定配置。 */ () => result.current.send());
  await waitFor(
    /* 检查真实 HTTP 边界的配置值。 */ () => expect(createFollowup).toHaveBeenCalledOnce(),
  );
  expect(vi.mocked(createFollowup).mock.calls[0][1].config).toEqual({
    skills: [],
    limits: { max_model_turns: 7 },
  });
});

it('更改技能后网络重试固定原始选择，编辑的新选择留给下一条', async () => {
  // 幂等重试不能因用户更改选择而变成另一组模型输入。
  vi.mocked(createFollowup)
    .mockRejectedValueOnce(new Error('网络断开'))
    .mockResolvedValueOnce(
      makeRun({ run_id: 'run-b', config: { ...makeRun().config, skills: ['changed-skill'] } }),
    );
  const { result, confirmed, rerender } = setup();
  act(
    /* 准备明确的新技能选择。 */ () => {
      result.current.setText('更换流程');
      result.current.setSkills(['changed-skill']);
    },
  );
  act(/* 第一次提交。 */ () => result.current.send());
  await waitFor(
    /* 网络结果未知时进入确认模式。 */ () => expect(result.current.confirming).toBe(true),
  );
  const original = vi.mocked(createFollowup).mock.calls[0];
  expect(original[1].config?.skills).toEqual(['changed-skill']);
  act(/* 用户可以继续编辑下一条的选择，原请求仍冻结。 */ () => result.current.setSkills([]));
  act(/* 只重试原请求。 */ () => result.current.send());
  await waitFor(/* 服务确认后才推进最新轮。 */ () => expect(confirmed).toHaveBeenCalledOnce());
  expect(vi.mocked(createFollowup).mock.calls[1]).toEqual(original);
  rerender({
    latest: makeRun({
      run_id: 'run-b',
      config: { ...makeRun().config, skills: ['changed-skill'] },
    }),
  });
  expect(result.current.skills).toEqual([]);
});

it('接纳新轮后沿用其实际选择，活动运行允许预选但不提交', async () => {
  // 新最新轮的配置只影响新消息，读取本身没有提交副作用。
  const host = setup();
  host.rerender({
    latest: makeRun({
      run_id: 'run-b',
      active: true,
      config: { ...makeRun().config, skills: ['next-skill'] },
    }),
  });
  expect(host.result.current.skills).toEqual(['next-skill']);
  act(
    /* 执行期间可以预写下一个工作流程。 */ () => {
      host.result.current.setSkills(['future-skill']);
      host.result.current.setText('执行完再处理');
    },
  );
  expect(host.result.current.skills).toEqual(['future-skill']);
  expect(host.result.current.canSend).toBe(false);
  act(/* 活动任务期间的发送直接忽略。 */ () => host.result.current.send());
  expect(createFollowup).not.toHaveBeenCalled();
});
