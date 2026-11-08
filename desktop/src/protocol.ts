/** 后台只允许请求固定原生能力；页面不能向主进程传入任意命令或文件 API。 */
export type NativeMethod =
  | "folder.choose"
  | "credential.read"
  | "credential.write"
  | "mcp.credential.read"
  | "mcp.credential.write"
  | "file.download"
  | "file.reveal"
  | "external.open"
  | "notification.show";

/** Python 经过业务校验后发给 Electron 的单个原生请求。 */
export interface NativeRequest {
  type: "native"; // 与 ready/error 消息区分。
  id: string; // 当前请求唯一标识，结果必须原样关联。
  method: NativeMethod; // 固定能力白名单。
  parameters: Record<string, unknown>; // 各方法继续按字段检查参数。
}

/** 主进程注入的原生执行器；返回值只通过私有标准流传回当前后台。 */
export type NativeHandler = (request: NativeRequest) => Promise<unknown>;

/** 校验 value 是否是允许的原生请求；失败抛固定错误，不输出可能含凭据的原始消息。 */
export function nativeRequest(value: unknown): NativeRequest {
  const methods: string[] = [
    "folder.choose",
    "credential.read",
    "credential.write",
    "mcp.credential.read",
    "mcp.credential.write",
    "file.download",
    "file.reveal",
    "external.open",
    "notification.show",
  ];
  if (!value || typeof value !== "object")
    throw new Error("后台原生请求格式无效");
  const item = value as NativeRequest;
  if (
    item.type !== "native" ||
    typeof item.id !== "string" ||
    !/^[a-f0-9]{32}$/.test(item.id) ||
    !methods.includes(item.method) ||
    !item.parameters ||
    typeof item.parameters !== "object" ||
    Array.isArray(item.parameters)
  )
    throw new Error("后台原生请求格式无效");
  return item;
}

/** 从 parameters 读取字符串 key；maximum 为字符数上限，默认16384，空白由具体能力检查。 */
export function textParameter(
  parameters: Record<string, unknown>,
  key: string,
  maximum = 16384,
): string {
  const value = parameters[key];
  if (
    typeof value !== "string" ||
    value.length > maximum ||
    value.includes("\0")
  )
    throw new Error("原生操作参数无效");
  return value;
}
