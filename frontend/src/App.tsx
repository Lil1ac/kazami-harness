import { NavLink, Navigate, Route, Routes } from 'react-router';
import {
  DeploymentUnitOutlined,
  HistoryOutlined,
  SettingOutlined,
  ArrowUpOutlined,
} from '@ant-design/icons';
import { RunsPage } from './pages/RunsPage';
import { RunDetailPage } from './pages/RunDetailPage';
import { SettingsPage } from './pages/SettingsPage';

/** 工作台壳层；导航与主体使用真实路由，未知地址引导回运行列表。 */
export function App() {
  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <NavLink to="/runs" className="brand" aria-label="Agent Harness 首页">
          <span className="brand-mark">
            <DeploymentUnitOutlined />
          </span>
          <span>
            Agent
            <br />
            <strong>Harness</strong>
          </span>
        </NavLink>
        <span className="sidebar-caption">LOCAL WORKSPACE</span>
        <nav aria-label="主导航">
          <NavLink to="/runs" aria-label="任务工作台">
            <HistoryOutlined />
            <span>任务工作台</span>
            <ArrowUpOutlined className="nav-arrow" />
          </NavLink>
          <NavLink to="/settings" aria-label="运行配置">
            <SettingOutlined />
            <span>运行配置</span>
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          <span className="local-indicator" /> 本地运行环境
          <span className="sidebar-version">v0.1 / 控制台</span>
        </div>
      </aside>
      <main className="app-main">
        <div className="topbar">
          <span>AGENT OPERATIONS</span>
          <span className="topbar-status">
            <i /> 本地工作台
          </span>
        </div>
        <div className="page-content">
          <Routes>
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:runId" element={<RunDetailPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/runs" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
