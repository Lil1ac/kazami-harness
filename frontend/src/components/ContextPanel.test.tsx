import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ContextView } from '../types';
import { ContextPanel } from './ContextPanel';

/** 构造已加载的完整上下文证据；覆盖项仅替换明确字段，不发起模型请求。 */
function makeContext(overrides: Partial<ContextView> = {}): ContextView {
  return {
    instruction: {
      status: 'loaded',
      path: 'AGENTS.md',
      content: '不要修改测试。\n<script>alert(1)</script>',
      sha256: 'a'.repeat(64),
      size_bytes: 50,
      error_code: null,
    },
    last_check: {
      model_turn: 2,
      input_bytes: 1024,
      max_input_bytes: 2048,
      message_count: 5,
      allowed: true,
    },
    serialization: 'json_utf8_v1',
    ...overrides,
  };
}

describe('持久化上下文证据', /* 不从可截断的事件窗口推测加载和预算状态。 */ () => {
  it('已加载指令完整按纯文本展示，并展示最新预算而非 token 数', () => {
    // 指令即使包含 HTML 也只能展示文本，不得执行。
    const { container } = render(
      <ContextPanel context={makeContext()} stopReason="final_answer" />,
    );
    expect(screen.getByText('已加载')).toBeInTheDocument();
    expect(screen.getByLabelText('项目指令内容')).toHaveTextContent('<script>alert(1)</script>');
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText(/占用 50%/)).toBeInTheDocument();
    expect(screen.getByText(/不等于模型 token 数/)).toBeInTheDocument();
    expect(screen.getByText(/实际 1,024 字节，上限 2,048 字节/)).toBeInTheDocument();
  });
  it('超限显示实际超过 100% 的占比和本轮未请求的原因', () => {
    // 进度条只封顶视觉宽度，文字保留真实超限大小。
    render(
      <ContextPanel
        context={makeContext({
          last_check: {
            model_turn: 3,
            input_bytes: 3072,
            max_input_bytes: 2048,
            message_count: 9,
            allowed: false,
          },
        })}
        stopReason="max_model_input_bytes"
      />,
    );
    expect(screen.getByText(/占用 150%/)).toBeInTheDocument();
    expect(screen.getByText('整体输入达到上限，未发送本轮模型请求')).toBeInTheDocument();
    expect(screen.getByText('max_model_input_bytes')).toBeInTheDocument();
    expect(screen.getByLabelText('模型输入预算占比')).toHaveAttribute('value', '100');
  });
  it('指令读取错误显示明确原因，不伪造内容或零大小', () => {
    // 编码错误阻止指令加载，未得到的字节数仍显示未知。
    const instruction = {
      ...makeContext().instruction,
      status: 'error' as const,
      content: null,
      sha256: null,
      size_bytes: null,
      error_code: 'project_instructions_invalid_utf8',
    };
    render(
      <ContextPanel
        context={makeContext({ instruction, last_check: null })}
        stopReason={instruction.error_code}
      />,
    );
    expect(screen.getByText('项目指令不是有效的 UTF-8 文本。')).toBeInTheDocument();
    expect(screen.getByText('未记录')).toBeInTheDocument();
    expect(screen.getByText('尚无输入检查记录。')).toBeInTheDocument();
    expect(screen.queryByLabelText('项目指令内容')).not.toBeInTheDocument();
  });
  it('缺少旧记录上下文时只显示未记录，不用默认配置伪造加载结论', () => {
    // 旧记录可能确实没有加载过指令，前端不能根据新默认值推断。
    render(<ContextPanel context={null} stopReason={null} />);
    expect(screen.getByText(/未记录上下文数据/)).toBeInTheDocument();
    expect(screen.queryByText('已加载')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('模型输入预算占比')).not.toBeInTheDocument();
  });
  it.each(['missing', 'disabled'] as const)('状态 %s 与读取失败分开展示', (status) => {
    // 无文件和主动关闭都允许执行，但不应展示为成功读取。
    const instruction = {
      ...makeContext().instruction,
      status,
      content: null,
      sha256: null,
      size_bytes: null,
    };
    render(<ContextPanel context={makeContext({ instruction })} stopReason={null} />);
    expect(screen.getByText(status === 'missing' ? '未找到' : '已关闭加载')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('项目指令内容')).not.toBeInTheDocument();
  });
});
