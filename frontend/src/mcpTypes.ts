/** 当前连接的公开设置；认证只保存引用，页面不能回读秘密。 */
export interface McpServerConfig {
  id: string; // 稳定连接名，修改需创建另一条连接。
  label: string; // 用户可识别的名称。
  transport: 'http' | 'stdio'; // 远程 HTTP 或本机进程。
  url: string; // HTTP 端点；不含查询或凭据。
  command: string; // 本机绝对程序路径。
  args: string[]; // 不经 Shell 的独立参数。
  cwd: string; // 本机服务启动目录。
  env_vars: string[]; // 明确传给本机服务的环境变量名。
  credential_env: string; // 非空使用宿主变量，空时桌面尝试对应加密凭据。
  credential_header: 'Authorization' | 'X-API-Key'; // Bearer 或 API Key。
  startup_timeout_seconds: number; // 连接和目录发现的秒级超时。
  tool_timeout_seconds: number; // 单次调用的秒级超时。
}

/** 审核后的服务目录；状态只表示最近一次发现，不承诺远端此刻在线。 */
export interface McpConnection {
  config: McpServerConfig; // 完整公开配置，审核前必须可查看。
  fingerprint: string; // 配置、目录、代次与研究授权的整体指纹。
  revision: number; // 配置或凭据变化递增。
  trusted: boolean; // 用户是否允许启动或连接。
  enabled: boolean; // 是否可供后续任务选择。
  research_tools: string[]; // 人工审核的研究工具原名。
  catalog: { name: string; description?: string; inputSchema: Record<string, unknown> }[]; // 实际发现的工具定义。
  status: string; // 真实管理状态，不能与任务结果混淆。
  error: string | null; // 安全错误，不含认证正文。
  protocol_version: string | null; // 最近协商的协议版本。
}

/** 运行携带工具名和选择时指纹，空列表明确关闭所有外部工具。 */
export interface McpToolSelection {
  name: string; // 稳定命名空间名称。
  fingerprint: string; // 选择时冻结的连接版本。
}

/** 当前可选择的工具元数据，不包含服务认证与运行结果。 */
export interface McpToolOption extends McpToolSelection {
  server_id: string; // 配置归属。
  server_label: string; // 页面连接名称。
  tool_name: string; // 远端原工具名。
  description: string; // 远端用途说明，仅作资料。
  research: boolean; // 宿主审核后可在研究阶段使用，仍需动作批准。
}
