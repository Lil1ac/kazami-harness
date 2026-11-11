import { Drawer } from 'antd';
import { RightOutlined } from '@ant-design/icons';
import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import './IntegrationCatalog.css';

/** 展示由目录记录派生的状态；tone 仅提供视觉提示，label 同时说明实际状态。 */
export function IntegrationStatus({
  label,
  tone = 'neutral',
}: {
  label: string; // 当前条目的可读状态。
  tone?: 'neutral' | 'ready' | 'attention' | 'error'; // 省略时用于一般状态或停用。
}) {
  return (
    <span className="integration-status" data-tone={tone}>
      {label}
    </span>
  );
}

/** 返回可从键盘打开的目录行；读屏同时获得名称、摘要和状态。 */
export function IntegrationRow({
  name,
  summary,
  status,
  icon,
  onOpen,
}: {
  name: string; // 列表和详情共用的显示名称。
  summary: string; // 适合快速浏览的简短信息，过长时只在详情完整显示。
  status: ReactNode; // 从宿主记录得到的当前状态。
  icon: ReactNode; // 对应技能、连接或脚本类别的图标。
  onOpen: (trigger: HTMLButtonElement) => void; // 打开条目详情，并提供关闭时的焦点目标。
}) {
  const id = useId(); // 关联当前行的可见标题和说明。
  return (
    <li>
      <button
        type="button"
        className="integration-row"
        onClick={/* 将实际打开详情的行交给目录。 */ (event) => onOpen(event.currentTarget)}
        aria-labelledby={id}
        aria-describedby={summary ? id + '-summary ' + id + '-status' : id + '-status'}
      >
        <span className="integration-row-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="integration-row-text">
          <strong id={id}>{name}</strong>
          {summary && <small id={id + '-summary'}>{summary}</small>}
        </span>
        <span id={id + '-status'}>{status}</span>
        <RightOutlined className="integration-row-arrow" aria-hidden="true" />
      </button>
    </li>
  );
}

/** 返回管理详情抽屉；窄屏占满可用宽度，关闭后恢复焦点到目录入口。 */
export function IntegrationDrawer({
  title,
  open,
  onClose,
  children,
  footer,
  returnFocus,
}: {
  title: string; // 当前对象或编辑操作的名称。
  open: boolean; // 是否在查看或编辑对象。
  onClose: () => void; // 退出详情或取消当前编辑。
  children: ReactNode; // 详情正文和表单。
  footer?: ReactNode; // 需要一直可见的确认和取消动作。
  returnFocus: HTMLElement | null; // 关闭后恢复焦点的入口；已移除的入口会跳过。
}) {
  const wasOpen = useRef(false); // 区分用户关闭详情和初始隐藏状态。
  useEffect(
    /* Drawer 退出焦点约束后，在下一帧将键盘操作还给目录。 */ () => {
      const closed = wasOpen.current && !open;
      wasOpen.current = open;
      if (!closed || !returnFocus) return;
      const frame = requestAnimationFrame(
        /* 目录刷新可能已移除入口，此时保留浏览器的默认焦点。 */ () => {
          if (returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
        },
      );
      return /* 重新打开或离开设置页时取消排队的恢复。 */ () => cancelAnimationFrame(frame);
    },
    [open, returnFocus],
  );
  return (
    <Drawer
      title={title}
      open={open}
      onClose={onClose}
      focusable={{ focusTriggerAfterClose: false }}
      destroyOnHidden
      rootClassName="integration-drawer"
      styles={{ wrapper: { width: 'min(100vw, 560px)' } }}
      footer={footer}
    >
      {children}
    </Drawer>
  );
}
