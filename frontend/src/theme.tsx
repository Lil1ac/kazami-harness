import { createContext, useContext, useEffect, useLayoutEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { ConfigProvider, theme as antdTheme } from 'antd';
import { DesktopOutlined, MoonOutlined, SunOutlined } from '@ant-design/icons';
import zhCN from 'antd/locale/zh_CN';

/** 外观偏好；system 随系统实时变化，显式选项跨刷新保存在当前浏览器。 */
type ThemePreference = 'system' | 'light' | 'dark';
/** 全站外观上下文，修改仅影响本机显示，不改变任务配置。 */
interface ThemeState {
  preference: ThemePreference; // 用户选择，初始为 system。
  setPreference: (value: ThemePreference) => void; // 切换并保存外观。
}
const ThemeContext = createContext<ThemeState>({
  preference: 'system',
  setPreference: /* 独立组件测试缺省不持久化。 */ () => undefined,
});

/** 读取本地外观；存储不可用或值非法时返回跟随系统，不阻断工作台。 */
function readPreference(): ThemePreference {
  try {
    const value = localStorage.getItem('harness.appearance');
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    return 'system';
  }
}

/** 为 children 提供同步的 CSS 与 Ant Design 主题；返回界面，卸载清理系统订阅。 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreference] = useState<ThemePreference>(readPreference);
  const [systemDark, setSystemDark] = useState(
    /* 首帧匹配系统，避免明暗闪烁。 */ () =>
      window.matchMedia('(prefers-color-scheme: dark)').matches,
  );
  const dark = preference === 'dark' || (preference === 'system' && systemDark);
  useLayoutEffect(
    /* 首次绘制前同步外观，CSS 和弹层使用同一色板。 */ () => {
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
      try {
        localStorage.setItem('harness.appearance', preference);
      } catch {
        /* 隐私模式仍可在本次页面切换。 */
      }
    },
    [preference, dark],
  );
  useEffect(
    /* 系统模式变化立即更新，不重建路由或输入草稿。 */ () => {
      const media = window.matchMedia('(prefers-color-scheme: dark)');
      /** 接收系统颜色变化；event 为当前媒体状态，返回 void。 */
      function update(event: MediaQueryListEvent): void {
        setSystemDark(event.matches);
      }
      media.addEventListener('change', update);
      return /* 释放当前媒体监听。 */ () => media.removeEventListener('change', update);
    },
    [],
  );
  return (
    <ThemeContext.Provider value={{ preference, setPreference }}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
          token: {
            colorPrimary: '#7278ef',
            colorInfo: '#7278ef',
            colorSuccess: '#38a57d',
            colorWarning: '#c49244',
            colorError: '#df6973',
            colorText: dark ? '#ececef' : '#25262c',
            colorTextSecondary: dark ? '#999aa6' : '#737581',
            colorBgBase: dark ? '#17181c' : '#ffffff',
            colorBgContainer: dark ? '#202126' : '#ffffff',
            colorBgElevated: dark ? '#26272e' : '#ffffff',
            colorBorder: dark ? '#36373f' : '#dedee5',
            borderRadius: 7,
            controlHeight: 34,
            fontSize: 13,
            fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
          },
          components: {
            Button: { primaryShadow: 'none' },
            Tabs: { horizontalItemGutter: 20 },
            Modal: { paddingContentHorizontalLG: 24 },
          },
        }}
      >
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

/** 返回侧栏外观分段控件；按钮直接设置浅色、深色或系统偏好，不请求后端。 */
export function ThemeToggle() {
  const { preference, setPreference } = useContext(ThemeContext);
  const choices: { key: ThemePreference; label: string; icon: ReactNode }[] = [
    { key: 'light', label: '浅色', icon: <SunOutlined aria-hidden /> },
    { key: 'dark', label: '深色', icon: <MoonOutlined aria-hidden /> },
    { key: 'system', label: '跟随系统', icon: <DesktopOutlined aria-hidden /> },
  ]; // 固定三种显示偏好，与本地保存值一一对应。
  return (
    <div className="appearance-control">
      <span>外观</span>
      <div className="appearance-options" role="group" aria-label="外观">
        {choices.map(
          /* 显式按钮支持键盘操作，选中态与本地偏好保持一致。 */ (choice) => (
            <button
              key={choice.key}
              type="button"
              title={choice.label}
              aria-label={choice.label}
              aria-pressed={preference === choice.key}
              onClick={/* 仅更改显示偏好，不影响正在执行的任务。 */ () => setPreference(choice.key)}
            >
              {choice.icon}
            </button>
          ),
        )}
      </div>
    </div>
  );
}
