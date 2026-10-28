import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Skeleton } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { Link, useParams } from 'react-router';
import { errorMessage, getOptions } from '../api';
import { localProjects } from '../projects';
import { ConversationHistory } from '../components/ConversationHistory';

/** 一个项目的会话入口；新建与继续使用不同路由，读取本页不会发起任务。 */
export function ProjectPage() {
  const { projectId = '' } = useParams();
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 读取当前真实登记。 */ ({ signal }) => getOptions(signal),
    staleTime: 15000,
  });
  const project = options.data
    ? localProjects(options.data).find(
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
      </header>
      {project.available === false && (
        <Alert
          type="warning"
          title="当前项目无法新建任务"
          description={project.error || '目录不可访问；已有会话仍可查看。'}
        />
      )}
      <ConversationHistory key={projectId} projectId={projectId} />
    </div>
  );
}
