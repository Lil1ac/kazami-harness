import { isValidElement, useState } from 'react';
import type { ReactNode } from 'react';
import Markdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CheckOutlined, CopyOutlined } from '@ant-design/icons';

/** 显示独立代码块；children 是 Markdown 生成的 code 节点，复制只由用户点击触发。 */
function MarkdownCodeBlock({ children }: { children?: ReactNode }) {
  const [state, setState] = useState<'idle' | 'copied' | 'error'>('idle'); // 当前块的复制反馈。
  const code = isValidElement<{ children?: ReactNode; className?: string }>(children)
    ? children.props
    : null;
  const value = typeof code?.children === 'string' ? code.children.replace(/\n$/, '') : '';
  const language = code?.className?.replace(/^language-/, '') || '代码';

  /** 将当前代码原文复制到剪贴板；失败保留内容并显示可重试反馈，不执行代码。 */
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch {
      setState('error');
    }
  }
  return (
    <div className="message-code">
      <div className="message-code-toolbar">
        <span>{language}</span>
        <button type="button" onClick={copy} aria-label="复制代码">
          {state === 'copied' ? <CheckOutlined /> : <CopyOutlined />}
          <span role="status">
            {state === 'copied' ? '已复制' : state === 'error' ? '复制失败，重试' : '复制'}
          </span>
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

/** 限制 Markdown 链接为网页、邮件、锚点或受限产物接口；其余协议返回空链接。 */
function messageUrl(url: string): string {
  const safe = defaultUrlTransform(url);
  return /^(https?:|mailto:|#|\/api\/)/i.test(safe) ? safe : '';
}

/** 将模型正文渲染为安全 Markdown；text 是真实回答，不解释原始 HTML、不自动加载外部图片。 */
export function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="markdown-message">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={messageUrl}
        components={{
          pre: MarkdownCodeBlock,
          a: /* 只为已通过协议检查的地址生成可点击链接。 */ ({ href, children }) =>
            href ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          table: /* 宽表格只在表格区域横向滚动，不扩大整页宽度。 */ ({ children }) => (
            <div className="message-table">
              <table>{children}</table>
            </div>
          ),
          img: /* 模型引用的远程图片以替代文本展示，避免正文加载任意外部资源。 */ ({ alt }) => (
            <span className="muted">[图片：{alt || '未提供说明'}]</span>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
