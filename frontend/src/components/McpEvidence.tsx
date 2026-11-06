import { useQuery } from '@tanstack/react-query';
import { Alert } from 'antd';
import { errorMessage, request } from '../api';
import './Mcp.css';

/** 当轮持久的外部工具证据，正文作为纯文本展示，不加载远端内容或当前配置。 */
interface Evidence {
  seq: number; // 单调持久事件编号。
  type: string; // 开始、结果或失败。
  name: string; // 固定命名空间工具名。
  server_id: string; // 当时连接标识。
  tool?: string; // 远端工具原名。
  fingerprint?: string; // 执行时定义指纹。
  sources?: string[]; // 从真实结果中提取的 URL。
  result?: unknown; // 有界原始结果快照。
  unknown?: boolean; // 未获可信结果时需人工核对。
  code?: string; // 固定安全错误码。
}

/** 展示真实 sources URL；首屏三条，其余保留在可展开列表，不发起远端请求。 */
function SourceLinks({ sources }: { sources: string[] }) {
  const urls = sources.filter(
    /* 外部内容不能创建脚本或本机文件链接。 */ (url) => /^https?:\/\//i.test(url),
  );
  /** 将 url 渲染为显式点击的网页链接；返回段落，不预加载来源。 */
  function link(url: string) {
    return (
      <p key={url}>
        <a href={url} target="_blank" rel="noreferrer">
          {url}
        </a>
      </p>
    );
  }
  return (
    <>
      {urls.slice(0, 3).map(link)}
      {urls.length > 3 && (
        <details>
          <summary>更多来源（{urls.length - 3}）</summary>
          {urls.slice(3).map(link)}
        </details>
      )}
    </>
  );
}

/** 展示 runId 的外部结果；active 时轮询追加证据，历史不回连外部服务。 */
export function McpEvidence({ runId, active }: { runId: string; active: boolean }) {
  const query = useQuery({
    queryKey: ['mcp-evidence', runId],
    queryFn: /* 仅访问本机持久日志。 */ () =>
      request<{ items: Evidence[]; tools: unknown[] }>(`/api/runs/${runId}/mcp`),
    refetchInterval: active ? 1500 : false,
  });
  if (query.error)
    return (
      <Alert
        type="warning"
        title="外部工具记录暂时无法读取"
        description={errorMessage(query.error)}
      />
    );
  if (!query.data?.tools.length) return null;
  return (
    <section className="mcp-evidence">
      <h3>外部资料与调用</h3>
      {query.data.items
        .filter(
          /* 开始事件不重复占一行，终态缺失则保留待核对提示。 */ (item) =>
            item.type !== 'mcp_call_started',
        )
        .map(
          /* 顺序来自当轮日志，不从当前服务补造来源。 */ (item) => (
            <article key={item.seq}>
              <p>
                <strong>
                  {item.server_id} / {item.tool || item.name}
                </strong>
              </p>
              {item.unknown && <Alert type="warning" title="结果待核对，原请求不会自动重放。" />}
              {item.code && <p>{item.code}</p>}
              <SourceLinks sources={item.sources || []} />
              <details>
                <summary>当轮结果与版本</summary>
                <code>{item.fingerprint}</code>
                <pre>{JSON.stringify(item.result, null, 2)}</pre>
              </details>
            </article>
          ),
        )}
      <details>
        <summary>本轮连接与工具快照</summary>
        <pre>{JSON.stringify(query.data.tools, null, 2)}</pre>
      </details>
    </section>
  );
}
