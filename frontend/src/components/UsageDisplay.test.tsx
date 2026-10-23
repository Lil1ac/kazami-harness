import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makeRun } from '../test/fixtures';
import { UsageDisplay } from './UsageDisplay';

describe('模型 token 用量', /* 只展示已知事实，在途请求和缺失计数保持可见。 */ () => {
  it('已发起模型请求但尚无返回时显示待统计', /* 不能把初始零显示为最终计费数。 */ () => {
    const run = makeRun({ active: true });
    run.result.usage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
    render(<UsageDisplay run={run} />);
    expect(screen.getByText('待统计')).toBeInTheDocument();
    expect(screen.getByText(/模型尚未返回用量/)).toBeInTheDocument();
  });
  it('运行中已有累计数据时仍标记已知部分', /* 后续请求与重试仍可能增加用量。 */ () => {
    render(<UsageDisplay run={makeRun({ active: true })} />);
    expect(screen.getByText('10')).toBeInTheDocument();
    expect(screen.getByText(/已知部分/)).toBeInTheDocument();
  });
  it('结束后缺少供应商统计时不假装是精确零', /* usage_complete=false 必须保留不确定性。 */ () => {
    const run = makeRun();
    run.result.usage_complete = false;
    run.result.usage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
    render(<UsageDisplay run={run} />);
    expect(screen.getByText('暂无已知用量')).toBeInTheDocument();
  });
});

it('服务中断时零用量显示未封存，已知用量只显示已知部分', () => {
  // 缺失结束记录不能把合成默认零计数当作完整供应商账单。
  const run = makeRun({ interrupted: true });
  run.result = {
    ...run.result,
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
  const view = render(<UsageDisplay run={run} />);
  expect(screen.getByText('本次用量未封存')).toBeInTheDocument();
  view.rerender(
    <UsageDisplay
      run={{
        ...run,
        result: {
          ...run.result,
          usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
        },
      }}
    />,
  );
  expect(screen.getByText('5')).toBeInTheDocument();
  expect(screen.getByText('· 已知部分')).toBeInTheDocument();
});
