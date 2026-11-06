import { request } from './api';
import type { McpConnection, McpServerConfig, McpToolOption } from './mcpTypes';

/** 获取连接和可用工具，返回公开目录；读取不启动服务。 */
export function getMcp(): Promise<{
  items: McpConnection[];
  tools: McpToolOption[];
  desktop_credentials: boolean;
  policy: string;
}> {
  return request('/api/mcp');
}

/** 保存 config；fingerprint 缺省为新增，更新必须提交当前版本，不自动信任。 */
export function saveMcp(config: McpServerConfig, fingerprint?: string): Promise<McpConnection> {
  return request('/api/mcp', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config, fingerprint }),
  });
}

/** 审核 item 当前版本的 action；researchTools 只用于明确审核研究用途，默认不开放。 */
export function reviewMcp(
  item: McpConnection,
  action: 'trust' | 'enable' | 'disable' | 'research',
  researchTools: string[] = [],
): Promise<McpConnection> {
  return request(`/api/mcp/${item.config.id}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fingerprint: item.fingerprint, action, research_tools: researchTools }),
  });
}

/** 用户明确连接 item 并重新发现工具；不会调用业务工具，返回新目录。 */
export function discoverMcp(item: McpConnection): Promise<McpConnection> {
  return request(`/api/mcp/${item.config.id}/discover?fingerprint=${item.fingerprint}`, {
    method: 'POST',
  });
}

/** 保存 item 的桌面 secret；空字符串清除，响应仅返回撤销审核后的公开配置。 */
export function saveMcpCredential(item: McpConnection, secret: string): Promise<McpConnection> {
  return request(`/api/mcp/${item.config.id}/credential`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fingerprint: item.fingerprint, secret }),
  });
}
