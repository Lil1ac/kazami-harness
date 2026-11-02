import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Dropdown, Input, Modal, Select } from 'antd';
import { MoreOutlined } from '@ant-design/icons';
import { errorMessage, getConversationManagement, getOptions, manageConversation } from '../api';
import type { ConversationAction, ConversationManagement } from '../types';
import './ConversationManagement.css';

/** 用服务端管理结果刷新所有会话入口；cache为共享查询器，id为稳定会话身份，返回void。 */
export function refreshConversations(cache: ReturnType<typeof useQueryClient>, id?: string): void {
  void cache.invalidateQueries({ queryKey: ['conversations'] });
  void cache.invalidateQueries({ queryKey: id ? ['conversation', id] : ['conversation'] });
  void cache.invalidateQueries({ queryKey: ['conversation-management'] });
  void cache.invalidateQueries({ queryKey: ['jobs'] });
  void cache.invalidateQueries({ queryKey: ['job'] });
}

/** 列表、侧栏和详情共用管理菜单；value来自后端，onChanged仅接收已保存的真实结果。 */
export function ConversationActions({
  value,
  onChanged,
}: {
  value: ConversationManagement; // 包含标题、版本和后端操作权限，不从运行状态自行猜测。
  onChanged?: (value: ConversationManagement) => void; // 可更新当前页面或离开已删除的对话。
}) {
  const cache = useQueryClient();
  const [dialog, setDialog] = useState<'rename' | 'move' | 'trash' | 'purge' | null>(null); // 当前明确打开的管理弹窗。
  const [title, setTitle] = useState(value.title); // 名称编辑草稿，失败保留。
  const [project, setProject] = useState<string | null>(value.assigned_project_id); // null表示未分组。
  const [confirmed, setConfirmed] = useState(false); // 仅本次彻底清理的明确确认。
  const [draftRevision, setDraftRevision] = useState(value.revision); // 弹窗打开时冻结基线，其他窗口更新不能静默放宽乐观锁。
  const retry = useRef<{ key: string; body: object } | null>(null); // 响应不明时沿用请求编号，编辑载荷后生成新编号。
  const refreshed = useRef<ConversationManagement | null>(null); // 用户明确刷新后的服务端版本，不覆盖编辑草稿。
  const statusRefresh = useMutation({
    mutationFn: /* 冲突后直接重读权威版本，不能只清除旧请求编号。 */ () =>
      getConversationManagement(value.session_id),
    onSuccess: /* 刷新完成才允许以新版本重新确认，保留名称和移动目标草稿。 */ (latest) => {
      refreshed.current = latest;
      retry.current = null;
      mutation.reset();
      refreshConversations(cache, value.session_id);
    },
  });
  const projects = useQuery({
    queryKey: ['options'],
    queryFn: /* 只在移动弹窗读取目录登记。 */ ({ signal }) => getOptions(signal),
    enabled: dialog === 'move',
    staleTime: 15000,
  });
  const mutation = useMutation({
    mutationFn: /* body中固定本次版本和幂等编号；purge只允许单条。 */ (request: {
      body: object;
      purge: boolean;
    }) => manageConversation(value.session_id, request.body, request.purge),
    onSuccess: /* 服务器确认后同步全站，不先从列表中乐观移除。 */ (updated) => {
      retry.current = null;
      refreshed.current = null;
      setDialog(null);
      refreshConversations(cache, value.session_id);
      onChanged?.(updated);
    },
  });

  /** 发送action与明确fields；相同载荷重试复用编号，purge=true时发送清理确认，返回void。 */
  function perform(action: ConversationAction | 'purge', fields: object = {}): void {
    if (mutation.isPending || statusRefresh.isPending) return;
    const revision = refreshed.current?.revision ?? (dialog ? draftRevision : value.revision);
    const payload =
      action === 'purge'
        ? { expected_revision: revision, confirm: true }
        : { expected_revision: revision, action, ...fields };
    const key = JSON.stringify({ action, ...fields }); // 刷新导致版本变化时仍重试原载荷，避免把未知结果重复提交。
    if (retry.current?.key !== key)
      retry.current = { key, body: { ...payload, request_id: crypto.randomUUID() } };
    mutation.mutate({ body: retry.current.body, purge: action === 'purge' });
  }
  /** 打开next弹窗并重置当前草稿、旧错误和确认勾选；不访问模型，返回void。 */
  function open(next: 'rename' | 'move' | 'trash' | 'purge'): void {
    setTitle(value.title);
    setProject(value.assigned_project_id);
    setConfirmed(false);
    setDraftRevision(value.revision);
    mutation.reset();
    statusRefresh.reset();
    retry.current = null;
    refreshed.current = null;
    setDialog(next);
  }
  /** 确认当前弹窗；空白标题保持校验提示，清理必须勾选，返回void。 */
  function submit(): void {
    if (dialog === 'rename' && title.trim()) perform('rename', { title: title.trim() });
    else if (dialog === 'move') perform('move', { project_id: project });
    else if (dialog === 'trash') perform('trash');
    else if (dialog === 'purge' && confirmed) perform('purge');
  }
  const deleted = ['trashed', 'purging', 'purged'].includes(value.state); // 删除状态不提供继续修改标题或归属。
  const items = deleted
    ? [
        { key: 'restore', label: '恢复对话', disabled: value.state !== 'trashed' },
        {
          key: 'purge',
          label: value.state === 'purging' ? '重试清理' : '彻底删除',
          danger: true,
          disabled: !value.can_purge,
          title: value.purge_blocked_reason || undefined,
        },
      ]
    : [
        { key: 'rename', label: '重命名' },
        {
          key: 'move',
          label: '移动到项目',
          disabled: !value.can_organize,
          title: value.blocked_reason || undefined,
        },
        { key: value.pinned_at ? 'unpin' : 'pin', label: value.pinned_at ? '取消置顶' : '置顶' },
        {
          key: value.state === 'archived' ? 'unarchive' : 'archive',
          label: value.state === 'archived' ? '取消归档' : '归档',
          disabled: value.state !== 'archived' && !value.can_organize,
          title: value.blocked_reason || undefined,
        },
        {
          key: 'trash',
          label: '删除',
          danger: true,
          disabled: !value.can_organize,
          title: value.blocked_reason || undefined,
        },
      ];
  return (
    <div className="conversation-actions">
      <Dropdown
        trigger={['click']}
        menu={{
          items,
          onClick: /* 明确菜单点击才执行或显示确认。 */ ({ key }) => {
            if (['rename', 'move', 'trash', 'purge'].includes(key))
              open(key as 'rename' | 'move' | 'trash' | 'purge');
            else perform(key as ConversationAction);
          },
        }}
      >
        <Button
          type="text"
          icon={<MoreOutlined />}
          aria-label={'管理对话：' + value.title}
          loading={mutation.isPending}
          disabled={mutation.isPending || value.state === 'purged'}
        />
      </Dropdown>
      {!dialog && mutation.error && <Alert type="error" title={errorMessage(mutation.error)} />}
      <Modal
        title={
          dialog === 'rename'
            ? '重命名对话'
            : dialog === 'move'
              ? '移动到项目'
              : dialog === 'purge'
                ? '彻底删除对话'
                : '删除对话'
        }
        open={dialog !== null}
        onCancel={
          /* 取消不改变管理状态或当前草稿。 */ () => {
            if (!mutation.isPending) setDialog(null);
          }
        }
        onOk={submit}
        okText={dialog === 'purge' ? '彻底删除' : dialog === 'trash' ? '移入回收站' : '保存'}
        cancelText="取消"
        confirmLoading={mutation.isPending}
        closable={!mutation.isPending}
        keyboard={!mutation.isPending}
        mask={{ closable: !mutation.isPending }}
        okButtonProps={{
          'aria-label':
            dialog === 'purge' ? '彻底删除' : dialog === 'trash' ? '移入回收站' : '保存',
          danger: dialog === 'purge' || dialog === 'trash',
          disabled:
            statusRefresh.isPending ||
            (dialog === 'rename' && !title.trim()) ||
            (dialog === 'purge' && !confirmed),
        }}
        cancelButtonProps={{ disabled: mutation.isPending, 'aria-label': '取消对话管理' }}
      >
        {dialog === 'rename' && (
          <>
            <Input
              aria-label="对话名称"
              value={title}
              maxLength={200}
              disabled={mutation.isPending || statusRefresh.isPending}
              onChange={/* 修改草稿不发请求。 */ (event) => setTitle(event.target.value)}
              onPressEnter={submit}
            />
            <Button
              type="link"
              onClick={/* 用户明确选择恢复默认标题。 */ () => perform('rename', { title: null })}
              disabled={mutation.isPending}
            >
              恢复默认名称
            </Button>
          </>
        )}
        {dialog === 'move' && (
          <>
            <Select
              aria-label="目标项目"
              className="conversation-target"
              value={project || ''}
              showSearch={{ optionFilterProp: 'search' }}
              virtual={false}
              loading={projects.isPending}
              disabled={mutation.isPending || statusRefresh.isPending}
              onChange={/* 空值明确移至未分组。 */ (id) => setProject(id || null)}
              options={[
                { value: '', label: '未分组', search: '未分组' },
                ...(projects.data?.recent_projects || []).map(
                  /* 同名项目通过路径区分。 */ (item) => ({
                    value: item.id,
                    label: item.label + ' · ' + item.path,
                    search: item.label + ' ' + item.path,
                  }),
                ),
              ]}
            />
            <p className="muted">仅调整列表归属。继续执行仍使用原来的工作目录和项目记忆。</p>
            {projects.error && <Alert type="error" title={errorMessage(projects.error)} />}
          </>
        )}
        {dialog === 'trash' && (
          <p>
            将“{value.title}”的 {value.run_count}{' '}
            轮对话移入回收站，可随后恢复。已另存的项目记忆继续由项目记忆管理。
          </p>
        )}
        {dialog === 'purge' && (
          <>
            <p>
              永久清理“{value.title}
              ”的运行、任务清单、作业正文、补丁及文件快照。项目目录中的代码保留。
            </p>
            <p>
              另存项目记忆引用：{value.memory_references} 条。来源会标记不可用，相关事实需重新核验。
            </p>
            <Checkbox
              checked={confirmed}
              onChange={
                /* 清理确认仅对当前弹窗有效。 */ (event) => setConfirmed(event.target.checked)
              }
            >
              确认永久删除这些记录
            </Checkbox>
          </>
        )}
        {(mutation.error || statusRefresh.error) && (
          <Alert
            type="error"
            title="操作未完成"
            description={errorMessage(statusRefresh.error || mutation.error)}
            action={
              <Button
                onClick={
                  /* 用户明确刷新后才抛弃原幂等载荷，草稿仍保留。 */ () => statusRefresh.mutate()
                }
                loading={statusRefresh.isPending}
              >
                刷新状态
              </Button>
            }
          />
        )}
      </Modal>
    </div>
  );
}
