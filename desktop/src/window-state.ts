import { readOptional, atomicWrite } from "./files";

/** Electron 窗口逻辑像素坐标；允许多显示器负坐标，不允许非有限数值。 */
export interface Bounds {
  x: number; // 左上角水平位置。
  y: number; // 左上角垂直位置。
  width: number; // 常规窗口宽度。
  height: number; // 常规窗口高度。
}

/** 跨启动保留的常规窗口尺寸和最大化状态，与后台运行设置分开维护。 */
export interface WindowPreferences {
  bounds: Bounds; // 恢复时会重新检查是否仍位于可用显示器内。
  maximized: boolean; // 最大化时仍保留原始常规尺寸。
}

/** 把 saved 放回可用的 displays 区域，返回有效窗口状态；显示器变化时居中并限制尺寸。 */
export function visibleWindow(
  saved: WindowPreferences | undefined,
  displays: Bounds[],
): WindowPreferences {
  const primary = displays[0];
  const width = Math.min(saved?.bounds.width ?? 1280, primary.width);
  const height = Math.min(saved?.bounds.height ?? 840, primary.height);
  const bounds = saved?.bounds;
  const visible =
    bounds &&
    displays.some(
      /* 标题栏必须位于可用屏幕，避免窗口恢复后无法拖回。 */ (area) =>
        bounds.x >= area.x &&
        bounds.y >= area.y &&
        bounds.x + Math.min(200, width) <= area.x + area.width &&
        bounds.y + 40 <= area.y + area.height,
    );
  return {
    bounds: {
      x: visible
        ? bounds.x
        : Math.round(primary.x + (primary.width - width) / 2),
      y: visible
        ? bounds.y
        : Math.round(primary.y + (primary.height - height) / 2),
      width,
      height,
    },
    maximized: saved?.maximized ?? false,
  };
}

/** 从 file 读取窗口状态；不存在返回 undefined，格式损坏明确抛错，不隐式迁移旧格式。 */
export async function loadWindowState(
  file: string,
): Promise<WindowPreferences | undefined> {
  const raw = await readOptional(file);
  if (!raw) return undefined;
  const value = JSON.parse(raw.toString("utf8")) as WindowPreferences;
  if (
    !value.bounds ||
    typeof value.maximized !== "boolean" ||
    !["x", "y", "width", "height"].every(
      /* 固定字段必须为有限整数，防止无效坐标传入原生窗口。 */ (key) =>
        Number.isSafeInteger(value.bounds[key as keyof Bounds]),
    ) ||
    value.bounds.width < 200 ||
    value.bounds.height < 200
  )
    throw new Error("窗口设置格式无效");
  return value;
}

/** 原子保存 file 对应的 state；只写窗口状态，不包含密钥、任务正文或后台会话令牌。 */
export async function saveWindowState(
  file: string,
  state: WindowPreferences,
): Promise<void> {
  await atomicWrite(file, JSON.stringify(state, null, 2));
}
