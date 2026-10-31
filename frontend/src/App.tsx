import { Navigate, Route, Routes, useMatch } from 'react-router';
import { RunsPage } from './pages/RunsPage';
import { RunDetailPage } from './pages/RunDetailPage';
import { ProjectPage } from './pages/ProjectPage';
import { SettingsPage } from './pages/SettingsPage';
import { WorkspaceNavigation } from './components/WorkspaceNavigation';
import { DesktopIntegration } from './components/DesktopIntegration';
import { JobsPage } from './pages/JobsPage';

/** 项目、会话和新建共享工作台；旧运行深链接保留，导航不会自动执行任务。 */
export function App() {
  const sessionRoute = useMatch('/runs/:runId'); // 会话消息区保持独立视口。
  return (
    <div className={'app-shell' + (sessionRoute ? ' app-session' : '')}>
      <DesktopIntegration />
      <WorkspaceNavigation />
      <main className="app-main">
        <div className="page-content">
          <Routes>
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/jobs/:jobId" element={<JobsPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:runId" element={<RunDetailPage />} />
            <Route path="/projects/:projectId" element={<ProjectPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/runs" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
