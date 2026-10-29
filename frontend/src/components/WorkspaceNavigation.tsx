import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Drawer } from 'antd';
import {
  PlusOutlined,
  MessageOutlined,
  SettingOutlined,
  FolderOutlined,
  DownOutlined,
  RightOutlined,
  MenuOutlined,
} from '@ant-design/icons';
import { Link, NavLink, useLocation, useMatch } from 'react-router';
import { errorMessage, getConversation, getConversations, getOptions, getRun } from '../api';
import type { LocalProjectOption } from '../types';

/** 项目下按会话展示最新五条，展开才查询；更多会话由项目页完整分页。 */
function ProjectGroup({
  project,
  selected,
  activeSession,
  onNavigate,
}: {
  project: LocalProjectOption; // 当前真实登记目录。
  selected: boolean; // 当前项目路由或当前会话所属项目。
  activeSession?: string; // 当前打开会话的稳定编号。
  onNavigate: () => void; // 手机导航后关闭抽屉，不改变草稿。
}) {
  const [expanded, setExpanded] = useState(selected); // 当前会话所属组默认展开。
  useEffect(
    /* 切换到该项目或重载该会话时自动展开所属组。 */ () => {
      if (selected) setExpanded(true);
    },
    [selected],
  );
  const query = useQuery({
    queryKey: ['conversations', { projectId: project.id, limit: 5 }],
    queryFn: /* 项目身份筛选发生在后端。 */ ({ signal }) =>
      getConversations({ projectId: project.id, limit: 5 }, signal),
    enabled: expanded,
    refetchInterval: expanded ? 5000 : false,
  });
  return (
    <section className="sidebar-project-group">
      <div className={'sidebar-project-heading' + (selected ? ' is-selected' : '')}>
        <button
          type="button"
          className="project-expand"
          aria-label={(expanded ? '收起' : '展开') + project.label + '的会话'}
          aria-expanded={expanded}
          aria-controls={'project-conversations-' + project.id}
          onClick={/* 展开只读取会话，不自动选择或发送。 */ () => setExpanded(!expanded)}
        >
          {expanded ? <DownOutlined /> : <RightOutlined />}
        </button>
        <NavLink
          aria-label={'打开项目：' + project.label}
          to={'/projects/' + encodeURIComponent(project.id)}
          onClick={onNavigate}
          title={project.path}
        >
          <FolderOutlined />
          <span>{project.label}</span>
        </NavLink>
        <Link
          to={'/runs?project=' + encodeURIComponent(project.id) + '&new=1'}
          className="project-new"
          aria-label={'在' + project.label + '中新建对话'}
          onClick={onNavigate}
        >
          <PlusOutlined />
        </Link>
      </div>
      {expanded && (
        <div id={'project-conversations-' + project.id} className="sidebar-project-conversations">
          {query.error && (
            <p className="sidebar-query-error" role="status">
              {errorMessage(query.error)}
            </p>
          )}
          {query.isPending ? (
            <p>读取会话…</p>
          ) : !query.data?.items.length ? (
            <p>还没有会话</p>
          ) : (
            <ul>
              {query.data.items.map(
                /* 一个会话一行，追加轮次只更新其状态和入口。 */ (item) => (
                  <li key={item.session_id}>
                    <Link
                      to={'/runs/' + item.entry_run_id}
                      className={item.session_id === activeSession ? 'is-current' : ''}
                      aria-current={item.session_id === activeSession ? 'page' : undefined}
                      aria-label={'继续对话：' + item.title}
                      title={item.error || item.title}
                      onClick={onNavigate}
                    >
                      <span>{item.title}</span>
                      {item.latest?.active && <i className="sidebar-running" title="正在执行" />}
                    </Link>
                  </li>
                ),
              )}
            </ul>
          )}
          {(query.data?.total || 0) > 5 && (
            <Link
              className="sidebar-project-more"
              to={'/projects/' + encodeURIComponent(project.id)}
              onClick={onNavigate}
            >
              查看全部 {query.data?.total} 个会话
            </Link>
          )}
        </div>
      )}
    </section>
  );
}

/** 桌面与手机共用相同的项目/会话树；只读查询复用详情页已有缓存。 */
function NavigationContent({ onNavigate }: { onNavigate: () => void }) {
  const location = useLocation();
  const projectRoute = useMatch('/projects/:projectId');
  const runRoute = useMatch('/runs/:runId');
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 共享目录快照。 */ ({ signal }) => getOptions(signal),
    staleTime: 15000,
  });
  const run = useQuery({
    queryKey: ['run', runRoute?.params.runId || ''],
    queryFn: /* 当前轮只用于定位所属稳定会话。 */ ({ signal }) =>
      getRun(runRoute!.params.runId!, signal),
    enabled: !!runRoute,
    retry: false,
  });
  const sessionId = run.data?.session_id || run.data?.run_id;
  const conversation = useQuery({
    queryKey: ['conversation', sessionId],
    queryFn: /* 从当前会话读取原项目身份，不按工作树路径猜测。 */ ({ signal }) =>
      getConversation(sessionId!, signal),
    enabled: !!runRoute && !!sessionId,
    retry: false,
  });
  const activeProject =
    projectRoute?.params.projectId ||
    (runRoute
      ? conversation.data?.source?.registered
        ? conversation.data.source.id
        : undefined
      : undefined);
  return (
    <div className="workspace-nav-content">
      <nav className="workspace-nav-links" aria-label="主导航">
        <NavLink
          to="/runs?new=1"
          aria-label="新建对话"
          end
          onClick={onNavigate}
          className={
            /* 只在新建页面高亮。 */ ({ isActive }) =>
              isActive && !location.search.includes('view=history') ? 'active' : ''
          }
        >
          <PlusOutlined />
          <span>新建对话</span>
        </NavLink>
        <NavLink
          to="/runs?view=history"
          aria-label="全部会话"
          onClick={onNavigate}
          className={
            /* 相同路径按query区分新建与全部会话。 */ ({ isActive }) =>
              isActive && location.search.includes('view=history') ? 'active' : ''
          }
        >
          <MessageOutlined />
          <span>全部会话</span>
        </NavLink>
      </nav>
      <div className="sidebar-projects">
        <div className="sidebar-projects-title">项目</div>
        {options.error && <p className="sidebar-query-error">无法刷新项目列表</p>}
        {options.data ? (
          options.data.recent_projects.length ? (
            options.data.recent_projects.map(
              /* 项目包含自己的会话集合。 */ (project) => (
                <ProjectGroup
                  key={project.id}
                  project={project}
                  selected={activeProject === project.id}
                  activeSession={runRoute ? sessionId : undefined}
                  onNavigate={onNavigate}
                />
              ),
            )
          ) : (
            <p className="sidebar-empty">打开文件夹后，项目会显示在这里。</p>
          )
        ) : (
          <p className="sidebar-empty">读取项目…</p>
        )}
      </div>
      <nav className="workspace-nav-links sidebar-settings" aria-label="设置">
        <NavLink to="/settings" onClick={onNavigate}>
          <SettingOutlined />
          <span>运行配置</span>
        </NavLink>
      </nav>
    </div>
  );
}

/** 固定桌面侧栏与小屏抽屉；切换页面只关闭导航，不卸载运行或删除输入。 */
export function WorkspaceNavigation() {
  const [open, setOpen] = useState(false); // 手机抽屉的显示状态。
  const location = useLocation();
  useEffect(
    /* 成功导航后收起抽屉，保留路由组件自己的草稿。 */ () => setOpen(false),
    [location.pathname, location.search],
  );
  /** 关闭导航层，返回void；不取消任务或清除草稿。 */
  function close(): void {
    setOpen(false);
  }
  return (
    <>
      <aside className="app-sidebar" aria-label="项目与会话导航">
        <Link to="/runs" className="brand" aria-label="Agent Harness 首页">
          Agent <strong>Harness</strong>
        </Link>
        <div className="desktop-navigation">
          <NavigationContent onNavigate={close} />
        </div>
        <div className="sidebar-mobile-controls">
          <Link to="/runs?new=1" aria-label="新建对话">
            <PlusOutlined />
          </Link>
          <button
            type="button"
            aria-label="打开项目与会话导航"
            onClick={/* 用户明确打开项目树。 */ () => setOpen(true)}
          >
            <MenuOutlined />
          </button>
        </div>
      </aside>
      <Drawer
        title="项目与会话"
        placement="left"
        size={300}
        open={open}
        onClose={close}
        className="navigation-drawer"
        destroyOnHidden
      >
        <NavigationContent onNavigate={close} />
      </Drawer>
    </>
  );
}
