import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Drawer, Skeleton } from 'antd';
import { PlusOutlined, SettingOutlined } from '@ant-design/icons';
import { Link, useParams, useSearchParams } from 'react-router';
import { errorMessage, getOptions } from '../api';
import { ConversationHistory } from '../components/ConversationHistory';
import { ProjectSettingsPanel } from '../components/ProjectSettingsPanel';
import { MemoryPanel } from '../components/MemoryPanel';

/** 一个项目的会话入口；新建与继续使用不同路由，读取本页不会发起任务。 */
export function ProjectPage() {
  const { projectId = '' } = useParams();
  const [params, setParams] = useSearchParams(); // 项目设置可从任务输入区直接打开，关闭后保留列表筛选。
  /** 根据 open 显示或关闭设置抽屉；仅更新 URL，不改变项目或执行状态，返回 void。 */
  function showSettings(open: boolean): void {
    const next = new URLSearchParams(params);
    if (open) next.set('settings', '1');
    else next.delete('settings');
    setParams(next, { replace: true });
  }
  /** 显式打开项目记忆抽屉；open=false 只关闭，不保存或发起运行，返回 void。 */
  function showMemory(open: boolean): void {
    const next = new URLSearchParams(params);
    if (open) next.set('memory', '1');
    else {
      next.delete('memory');
      next.delete('consolidation');
    }
    setParams(next, { replace: true });
  }
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 读取当前真实登记。 */ ({ signal }) => getOptions(signal),
    staleTime: 15000,
  });
  const project = options.data
    ? options.data.recent_projects.find(
        /* 身份由宿主目录登记确定。 */ (item) => item.id === projectId,
      )
    : undefined;
  if (options.isPending) return <Skeleton active />;
  if (!project)
    return (
      <div className="project-workspace">
        <Link to="/runs?view=history">查看全部会话</Link>
        <Alert
          type="error"
          title="无法打开项目"
          description={
            options.error
              ? errorMessage(options.error)
              : '项目登记已移除；已有会话仍可从全部会话打开。'
          }
        />
      </div>
    );
  return (
    <div className="project-workspace">
      <header className="project-workspace-header">
        <div>
          <h1>{project.label}</h1>
          <p className="project-workspace-path">{project.path}</p>
        </div>
        <div className="workspace-actions">
          <Button onClick={/* 读取记忆不启动模型。 */ () => showMemory(true)}>项目记忆</Button>
          <Button
            icon={<SettingOutlined />}
            onClick={/* 明确打开当前项目设置。 */ () => showSettings(true)}
          >
            项目设置
          </Button>
          {project.available === false ? (
            <Button type="primary" icon={<PlusOutlined />} disabled>
              新建对话
            </Button>
          ) : (
            <Link
              to={'/runs?project=' + encodeURIComponent(project.id) + '&new=1'}
              className="new-conversation-link"
              aria-label="新建对话"
            >
              <PlusOutlined /> 新建对话
            </Link>
          )}
        </div>
      </header>
      {project.available === false && (
        <Alert
          type="warning"
          title="当前项目无法新建任务"
          description={project.error || '目录不可访问；已有会话仍可查看。'}
        />
      )}
      <ConversationHistory key={projectId} projectId={projectId} />
      <Drawer title="项目记忆" open={params.get('memory') === '1'} onClose={/* 关闭不更改知识。 */ () => showMemory(false)} destroyOnHidden styles={{ wrapper: { width: 'min(100vw, 720px)' } }}>
        {params.get('memory') === '1' && <MemoryPanel projectId={projectId} consolidationId={params.get('consolidation') || undefined} />}
      </Drawer>
      <Drawer
        title="项目设置"
        open={params.get('settings') === '1'}
        onClose={/* 关闭设置不发起任务或保存草稿。 */ () => showSettings(false)}
        destroyOnHidden
        styles={{ wrapper: { width: 'min(100vw, 480px)' } }}
      >
        <ProjectSettingsPanel key={project.id} project={project} />
      </Drawer>
    </div>
  );
}
