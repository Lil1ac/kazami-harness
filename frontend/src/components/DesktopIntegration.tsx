import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { message } from 'antd';
import { desktopAction, errorMessage, getOptions } from '../api';

/** 桌面窗口统一承接附件与外链点击；网页模式沿用浏览器行为，不创建第二套页面。 */
export function DesktopIntegration() {
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 与主工作台共享能力查询，页面加载不会执行原生操作。 */ ({ signal }) =>
      getOptions(signal),
  });
  const enabled = Boolean(options.data?.desktop); // 只由受信宿主返回的能力启用原生操作。
  const [feedback, contextHolder] = message.useMessage();

  useEffect(
    /* 后台中断时保留会话页面并显示固定提示，不自动重复提交任务。 */ () => {
      return window.agentHarnessDesktop?.onServiceError((message) => {
        void feedback.error(message, 0);
      });
    },
    [feedback],
  );

  useEffect(() => {
    if (!enabled) return;
    /** 捕获用户主动点击的附件或外链；内部路由交给 React Router，失败保留页面内容。 */
    function clicked(event: MouseEvent): void {
      if (event.defaultPrevented || !(event.target instanceof Element)) return;
      const anchor = event.target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor) return;
      const target = new URL(anchor.href, window.location.href);
      const attachment =
        target.origin === window.location.origin &&
        (/^\/api\/runs\/[0-9a-f-]{36}\/(artifacts|patch)$/i.test(target.pathname) || /^\/api\/projects\/[a-zA-Z0-9_-]{1,64}\/memories\/export$/.test(target.pathname)) &&
        !target.searchParams.has('preview');
      const external =
        target.origin !== window.location.origin && /^(https?:|mailto:)$/.test(target.protocol);
      if (!attachment && !external) return;
      event.preventDefault();
      const action = attachment ? 'download' : 'external';
      const url = attachment ? target.pathname + target.search : target.href;
      void desktopAction(action, url).then(
        /* 保存成功显示真实路径，用户可以在产物列表显式定位文件。 */ (result) => {
          if (attachment) void feedback.success(`已保存到 ${result.path}`, 6);
        },
        /* 原生操作失败不导航离开当前会话，并展示后端受控说明。 */ (error: unknown) => {
          void feedback.error(errorMessage(error));
        },
      );
    }
    document.addEventListener('click', clicked, true);
    return /* 能力变化或组件卸载时撤销原生接管，避免重复下载。 */ () =>
      document.removeEventListener('click', clicked, true);
  }, [enabled, feedback]);

  return contextHolder;
}
