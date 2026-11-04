import { useId, useState } from 'react';
import { Alert, Button, Drawer, Empty, Spin, Tag } from 'antd';
import { DownloadOutlined, FileTextOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { artifactUrl, desktopAction, errorMessage, getOptions, previewArtifact } from '../api';
import type { ArtifactRef } from '../types';

/** 展示工作区及运行内产物，预览失败仍允许附件下载；runId 改变时详情页会整体重置。 */
export function ArtifactPanel({
  runId,
  artifacts,
  status,
  warnings,
  parentRunId,
}: {
  runId: string;
  artifacts: ArtifactRef[];
  status: 'pending' | 'complete' | 'incomplete'; // 尚未收集、完整、采集受限。
  warnings: string[]; // 后端受控提示；不代表测试失败。
  parentRunId?: string; // 子产物的父视图，缺省为顶层运行；仅参与宿主归属核验。
}) {
  const headingId = useId(); // 父与子产物面板同时打开时保持标题关联唯一。
  const [selection, setSelected] = useState<ArtifactRef | null>(null); // 当前抽屉预览目标。
  const [nativeError, setNativeError] = useState<string | null>(null); // 原生定位失败不影响历史文件预览。
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 复用宿主能力缓存以显示原生定位操作。 */ ({ signal }) => getOptions(signal),
  });
  const selected = selection; // 历史文件来自固定快照，不依赖共享工作树仍然存在。
  const preview = useQuery({
    queryKey: ['artifact', runId, parentRunId, selected?.location, selected?.path],
    queryFn: /* 仅选中后读取有界文本。 */ () => previewArtifact(runId, selected!, parentRunId),
    enabled: Boolean(selected),
    retry: false,
  });
  return (
    <section aria-labelledby={headingId}>
      <div className="section-heading">
        <div>
          <h2 id={headingId}>
            产物与输出 <small>{artifacts.length}</small>
          </h2>
        </div>
      </div>
      {status === 'incomplete' && (
        <Alert
          showIcon
          type="warning"
          title="文件收集不完整"
          description={warnings.join('；') || '部分文件未能固化，已保存的输出仍可查看。'}
        />
      )}
      {!artifacts.length && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            status === 'pending'
              ? '任务结束后自动收集本轮文件'
              : status === 'incomplete'
                ? '暂无可核对的文件产物'
                : '本轮没有新增或修改的文件及输出'
          }
        />
      )}
      <ul className="artifact-list">
        {artifacts.map(
          /* 相对位置与路径共同形成唯一键。 */ (artifact) => (
            <li key={`${artifact.location}:${artifact.path}`}>
              <FileTextOutlined />
              <div>
                <button
                  className="text-button artifact-name"
                  onClick={/* 打开预览，不执行文件。 */ () => setSelected(artifact)}
                >
                  {artifact.path}
                </button>
                <span className="muted artifact-location">
                  {artifact.location === 'workspace' ? '工作区' : '运行记录'} · {artifact.kind}
                </span>
              </div>
              {options.data?.desktop && (
                <Button
                  type="text"
                  icon={<FolderOpenOutlined />}
                  aria-label={`在文件夹中显示 ${artifact.path}`}
                  onClick={
                    /* 仅向宿主传已登记产物引用，不接受页面拼接宿主路径。 */ () => {
                      setNativeError(null);
                      void desktopAction('reveal', artifactUrl(runId, artifact, false, parentRunId)).catch(
                        /* 原生定位失败保留文件列表并提供可重试说明。 */ (error: unknown) =>
                          setNativeError(errorMessage(error)),
                      );
                    }
                  }
                />
              )}
              <a
                className="download-link"
                href={artifactUrl(runId, artifact, false, parentRunId)}
                aria-label={`下载 ${artifact.path}`}
                download
              >
                <DownloadOutlined />
              </a>
            </li>
          ),
        )}
      </ul>
      {nativeError && <Alert type="error" title={nativeError} />}
      <Drawer
        title={selected?.path || '产物预览'}
        size="large"
        open={Boolean(selected)}
        onClose={/* 关闭抽屉保留后端文件不变。 */ () => setSelected(null)}
        extra={
          selected && (
            <Button href={artifactUrl(runId, selected, false, parentRunId)} icon={<DownloadOutlined />}>
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
