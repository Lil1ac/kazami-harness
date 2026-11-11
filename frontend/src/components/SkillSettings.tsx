import { Alert, Button, Modal, Select, Skeleton } from 'antd';
import { BookOutlined, ImportOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { errorMessage } from '../api';
import {
  changeSkill,
  importSkill,
  previewSkill,
  previewSkillResource,
  readSkillFolder,
} from '../skillApi';
import type { SkillImport } from '../skillApi';
import type { SkillOption } from '../types';
import { IntegrationDrawer, IntegrationRow, IntegrationStatus } from './IntegrationCatalog';

/** 返回技能目录项的稳定身份；没有服务端标识的无效目录以路径和名称区分。 */
function skillKey(item: SkillOption): string {
  return item.id || item.source + item.name;
}

/** 根据宿主目录项返回有效状态，不把格式错误当成用户停用。 */
function skillStatus(item: SkillOption) {
  if (item.error_code === 'skill_disabled') return <IntegrationStatus label="已停用" />;
  if (!item.available) return <IntegrationStatus label="不可用" tone="error" />;
  return <IntegrationStatus label="已启用" tone="ready" />;
}

/** 管理已登记的 skills；导入、启停和移除通过固定版本的同源接口完成。 */
export function SkillSettings({ skills }: { skills: SkillOption[] }) {
  const cache = useQueryClient();
  const picker = useRef<HTMLInputElement>(null); // 只在明确导入时打开目录选择器。
  const importTrigger = useRef<HTMLElement | null>(null); // 取消或完成导入后返回工具栏。
  const detailTrigger = useRef<HTMLButtonElement | null>(null); // 关闭详情后返回打开它的目录行。
  const [reading, setReading] = useState(false); // 当前是否在读取本地文件。
  const [localError, setLocalError] = useState<string | null>(null); // 本地目录读取错误。
  const [pending, setPending] = useState<{ input: SkillImport; conflict?: SkillOption } | null>(
    null,
  ); // 导入草稿和实际看到的冲突版本。
  const [viewKey, setViewKey] = useState<string | null>(null); // 选中条目的稳定标识，刷新后读取最新状态。
  const [path, setPath] = useState(''); // 空值阅读 SKILL.md，否则阅读选中的附属文件。
  const [remove, setRemove] = useState<SkillOption | null>(null); // 用户正在确认移除的目录版本。
  const view = skills.find((item) => skillKey(item) === viewKey);
  const conflict = pending?.conflict;
  const detail = useQuery({
    queryKey: ['skill-preview', view?.id, view?.revision],
    enabled: Boolean(view?.id),
    queryFn: /* 显式打开才读取正文。 */ () => previewSkill(view!.id!),
  });
  const resource = useQuery({
    queryKey: ['skill-resource', view?.id, view?.revision, path],
    enabled: Boolean(view?.id && path),
    queryFn: /* 仅读取当前选中的资料路径。 */ () => previewSkillResource(view!.id!, path),
  });
  const mutation = useMutation({
    mutationFn: /* 执行用户确认的唯一操作。 */ (operation: () => Promise<unknown>) => operation(),
    onSuccess: /* 任务选择器与管理目录共用同一份刷新结果。 */ async () => {
      setPending(null);
      if (remove) setViewKey(null);
      setRemove(null);
      setPath('');
      await cache.invalidateQueries({ queryKey: ['options'] });
    },
  });

  /** 将 files 解码成可预览文本包；取消目录选择时返回，不产生写入。 */
  async function choose(files: FileList | null): Promise<void> {
    if (!files?.length) return;
    setReading(true);
    setLocalError(null);
    mutation.reset();
    try {
      const input = await readSkillFolder(files);
      setPending({ input, conflict: skills.find((item) => item.name === input.name) });
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : '无法读取所选技能目录。');
    } finally {
      setReading(false);
    }
  }

  /** 打开技能 item 的阅读详情并记住入口 trigger，清除上一条的阅读和操作反馈，无返回值。 */
  function openSkill(item: SkillOption, trigger: HTMLButtonElement): void {
    detailTrigger.current = trigger;
    setViewKey(skillKey(item));
    setPath('');
    mutation.reset();
  }

  const attachmentPaths = Object.keys(detail.data?.resources || {});
  const readingQuery = path ? resource : detail;

  return (
    <section className="integration-catalog" aria-label="技能目录">
      <header className="integration-toolbar">
        <h2>技能目录</h2>
        <Button
          type="primary"
          icon={<ImportOutlined />}
          loading={reading}
          disabled={mutation.isPending}
          onClick={
            /* 记住导入入口后打开浏览器目录选择器。 */ (event) => {
              importTrigger.current = event.currentTarget;
              picker.current?.click();
            }
          }
        >
          导入技能
        </Button>
        <input
          ref={picker}
          type="file"
          hidden
          {...{ webkitdirectory: '' }}
          aria-label="技能目录"
          onChange={
            /* 重置输入允许重复选择同一目录。 */ (event) => {
              void choose(event.currentTarget.files);
              event.currentTarget.value = '';
            }
          }
        />
      </header>
      {localError && <Alert type="error" showIcon title={localError} />}
      {skills.length > 0 ? (
        <ul className="integration-list">
          {skills.map(
            /* 路径与原文只在详情读取，列表保持单行摘要。 */ (item) => (
              <IntegrationRow
                key={skillKey(item)}
                name={item.name || '未命名技能'}
                icon={<BookOutlined />}
                summary={
                  item.error_code !== 'skill_disabled' && item.error_message
                    ? item.error_message
                    : [
                        item.managed ? '导入副本' : '宿主登记',
                        item.manual_only ? '仅手动' : '',
                        item.description,
                      ]
                        .filter(Boolean)
                        .join(' · ')
                }
                status={skillStatus(item)}
                onOpen={/* 详情可定位无效目录的来源。 */ (trigger) => openSkill(item, trigger)}
              />
            ),
          )}
        </ul>
      ) : (
        <p className="integration-empty">还没有技能</p>
      )}
      <IntegrationDrawer
        open={pending !== null}
        returnFocus={importTrigger.current}
        title={conflict?.managed ? '更新技能' : '导入技能'}
        onClose={
          /* 未确认的内容只在内存中，提交期间保留当前预览。 */ () => {
            if (!mutation.isPending) setPending(null);
          }
        }
        footer={
          <div className="integration-actions">
            <Button
              disabled={mutation.isPending}
              onClick={/* 丢弃未确认的预览。 */ () => setPending(null)}
            >
              取消
            </Button>
            <Button
              type="primary"
              loading={mutation.isPending}
              disabled={Boolean(conflict && !conflict.managed)}
              onClick={
                /* 后端按已看到的版本一次性导入或替换。 */ () =>
                  pending &&
                  mutation.mutate(() => importSkill(pending.input, conflict?.revision || null))
              }
            >
              {conflict?.managed ? '替换副本' : '确认导入'}
            </Button>
          </div>
        }
      >
        {pending && (
          <div className="integration-detail">
            <h3>{pending.input.name}</h3>
            {conflict && (
              <Alert
                type={conflict.managed ? 'warning' : 'error'}
                showIcon
                title={conflict.managed ? '导入会替换现有副本' : '同名技能已由宿主登记'}
              />
            )}
            <pre aria-label="待导入技能正文">
              {pending.input.files.find((file) => file.path === 'SKILL.md')?.content}
            </pre>
            <details className="integration-disclosure">
              <summary>包含文件（{pending.input.files.length}）</summary>
              <ul>
                {pending.input.files.map(
                  /* 仅列出本次读取的相对路径。 */ (file) => (
                    <li key={file.path}>
                      <code>{file.path}</code>
                    </li>
                  ),
                )}
              </ul>
            </details>
            {pending.input.ignored > 0 && (
              <p className="muted">已跳过其他目录中的 {pending.input.ignored} 个文件。</p>
            )}
            {mutation.error && <Alert type="error" showIcon title={errorMessage(mutation.error)} />}
          </div>
        )}
      </IntegrationDrawer>
      <IntegrationDrawer
        open={Boolean(view)}
        returnFocus={detailTrigger.current}
        title={view?.name || '技能详情'}
        onClose={
          /* 关闭不改变技能目录。 */ () => {
            if (!mutation.isPending) setViewKey(null);
          }
        }
        footer={
          view?.id && (
            <div className="integration-actions">
              {view.managed && (
                <Button
                  type="text"
                  danger
                  disabled={mutation.isPending}
                  onClick={
                    /* 固定待移除版本，随后明确确认。 */ () => {
                      mutation.reset();
                      setRemove(view);
                    }
                  }
                >
                  移除
                </Button>
              )}
              <Button
                disabled={
                  mutation.isPending ||
                  (view.enabled === false && view.error_code !== 'skill_disabled')
                }
                onClick={
                  /* 启停使用详情展示的版本。 */ () =>
                    mutation.mutate(() =>
                      changeSkill(view, view.enabled === false ? 'enable' : 'disable'),
                    )
                }
              >
                {view.enabled === false ? '启用' : '停用'}
              </Button>
            </div>
          )
        }
      >
        {view && (
          <div className="integration-detail">
            <div>
              {skillStatus(view)}
              {view.manual_only && <span className="muted"> · 仅手动</span>}
            </div>
            {view.description && <p>{view.description}</p>}
            {view.error_code !== 'skill_disabled' && view.error_message && (
              <Alert type="warning" showIcon title={view.error_message} />
            )}
            {mutation.error && !remove && (
              <Alert type="error" showIcon title={errorMessage(mutation.error)} />
            )}
            {detail.data &&
              (attachmentPaths.length ? (
                <Select
                  className="integration-resource-picker"
                  aria-label="技能文件"
                  value={path}
                  onChange={setPath}
                  options={[
                    { value: '', label: 'SKILL.md' },
                    ...attachmentPaths.map(
                      /* 资料以受控相对路径标识。 */ (item) => ({ value: item, label: item }),
                    ),
                  ]}
                />
              ) : (
                <h3>SKILL.md</h3>
              ))}
            {readingQuery.isFetching && <Skeleton active paragraph={{ rows: 3 }} />}
            {readingQuery.error && (
              <Alert type="error" showIcon title={errorMessage(readingQuery.error)} />
            )}
            {readingQuery.data && (
              <pre aria-label={path ? '技能资料正文' : '技能正文'}>{readingQuery.data.content}</pre>
            )}
            <details className="integration-disclosure">
              <summary>来源</summary>
              <p className="muted">{view.managed ? '导入副本' : '宿主登记'}</p>
              <code>{view.source}</code>
            </details>
          </div>
        )}
      </IntegrationDrawer>
      <Modal
        open={remove !== null}
        title={'移除技能 ' + (remove?.name || '')}
        okText="移除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={mutation.isPending}
        onCancel={
          /* 只关闭尚未提交的确认。 */ () => {
            if (!mutation.isPending) setRemove(null);
          }
        }
        onOk={
          /* 只删除当前确认的导入副本。 */ () =>
            remove && mutation.mutate(() => changeSkill(remove, 'remove'))
        }
      >
        <p>将移除导入副本。历史运行仍可查看当时的技能正文。</p>
        {mutation.error && <Alert type="error" showIcon title={errorMessage(mutation.error)} />}
      </Modal>
    </section>
  );
}
