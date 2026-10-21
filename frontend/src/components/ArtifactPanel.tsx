import { useState } from 'react';
import { Alert, Button, Drawer, Empty, Spin, Tag } from 'antd';
import { DownloadOutlined, FileTextOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { artifactUrl, errorMessage, previewArtifact } from '../api';
import type { ArtifactRef } from '../types';

/** 展示工作区及运行内产物，预览失败仍允许附件下载；runId 改变时详情页会整体重置。 */
export function ArtifactPanel({
  runId,
  artifacts,
  missing,
  workspaceUnavailable = false,
}: {
  runId: string;
  artifacts: ArtifactRef[];
  missing: string[];
  workspaceUnavailable?: boolean; // 项目工作树已丢弃时，只允许读取运行目录中的证据。
}) {
  const [selection, setSelected] = useState<ArtifactRef | null>(null); // 当前抽屉预览目标。
  const selected = workspaceUnavailable && selection?.location === 'workspace' ? null : selection;
  const preview = useQuery({
    queryKey: ['artifact', runId, selected?.location, selected?.path],
    queryFn: /* 仅选中后读取有界文本。 */ () => previewArtifact(runId, selected!),
    enabled: Boolean(selected),
    retry: false,
  });
  return (
    <section aria-labelledby="artifact-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">ARTIFACTS</span>
          <h2 id="artifact-heading">
            产物与输出 <small>{artifacts.length}</small>
          </h2>
        </div>
      </div>
      {workspaceUnavailable && (
        <Alert
          type="info"
          showIcon
          title="工作树已丢弃，工作区产物不再提供访问；运行记录、测试输出和补丁仍保留。"
        />
      )}
      {missing.length > 0 && (
        <Alert
          showIcon
          type="warning"
          title="部分预期产物未找到"
          description={missing.join('、')}
        />
      )}
      {!artifacts.length && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未登记产物" />
      )}
      <ul className="artifact-list">
        {artifacts.map(
          /* 相对位置与路径共同形成唯一键。 */ (artifact) => (
            <li key={`${artifact.location}:${artifact.path}`}>
              <FileTextOutlined />
              <div>
                <button
                  className="text-button artifact-name"
                  disabled={workspaceUnavailable && artifact.location === 'workspace'}
                  onClick={/* 打开预览，不执行文件。 */ () => setSelected(artifact)}
                >
                  {artifact.path}
                </button>
                <span className="muted artifact-location">
                  {artifact.location === 'workspace' ? '工作区' : '运行记录'} · {artifact.kind}
                </span>
              </div>
              {!(workspaceUnavailable && artifact.location === 'workspace') && (
                <a
                  className="download-link"
                  href={artifactUrl(runId, artifact)}
                  aria-label={`下载 ${artifact.path}`}
                  download
                >
                  <DownloadOutlined />
                </a>
              )}
            </li>
          ),
        )}
      </ul>
      <Drawer
        title={selected?.path || '产物预览'}
        size="large"
        open={Boolean(selected)}
        onClose={/* 关闭抽屉保留后端文件不变。 */ () => setSelected(null)}
        extra={
          selected && (
            <Button href={artifactUrl(runId, selected)} icon={<DownloadOutlined />}>
              下载
            </Button>
          )
        }
      >
        {preview.isPending && <Spin tip="读取预览" />}
        {preview.error && (
          <Alert
            type="info"
            title="无法提供文本预览"
            description={errorMessage(preview.error)}
            showIcon
          />
        )}
        {preview.data && (
          <>
            <p className="muted">
              UTF-8 · {preview.data.size_bytes.toLocaleString()} 字节{' '}
              {preview.data.truncated && <Tag color="warning">预览已截断</Tag>}
            </p>
            <pre className="file-preview">{preview.data.content}</pre>
          </>
        )}
      </Drawer>
    </section>
  );
}
