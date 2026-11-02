import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Modal, Select } from 'antd';
import { errorMessage, getOptions, manageConversationBatch } from '../api';
import type { ConversationSummary } from '../types';
import { refreshConversations } from './ConversationActions';

/** 整理当前页明确选择的items；onDone保留失败编号，不把全选扩展到其他页。 */
export function ConversationBatchActions({
  items,
  state,
  onDone,
}: {
  items: ConversationSummary[]; // 本页当前选中的会话及其最新管理版本。
  state: string; // 当前列表生命周期，决定可显示动作。
  onDone: (failed: string[]) => void; // 成功项取消选择，失败项继续选中。
}) {
  const cache = useQueryClient();
  const [action, setAction] = useState<string | null>(null); // 当前明确确认的批量动作。
  const [project, setProject] = useState(''); // 移动目标，空字符串代表未分组。
  const retry = useRef<{ key: string; body: object } | null>(null); // 响应不明时保持同一请求载荷及编号。
  const projects = useQuery({
    queryKey: ['options'],
    queryFn: /* 移动弹窗才需要目录登记。 */ ({ signal }) => getOptions(signal),
    enabled: action === 'move',
    staleTime: 15000,
  });
  const mutation = useMutation({
    mutationFn: /* body已经冻结当前页编号和版本。 */ (body: object) =>
      manageConversationBatch(body),
    onSuccess: /* 每项结果独立呈现；成功不掩盖其余会话的冲突或忙碌。 */ (response) => {
      refreshConversations(cache);
      onDone(
        response.items
          .filter(/* 失败项保留选择，便于检查后重新操作。 */ (item) => !item.success)
          .map(/* 稳定编号不依赖列表顺序。 */ (item) => item.session_id),
      );
      setAction(null);
      retry.current = null;
    },
  });
  /** 确认本次批量动作；载荷变化才分配新编号，返回void，运行和永久清理不在此入口。 */
  function submit(): void {
    if (mutation.isPending) return;
    const payload = {
      action,
      project_id: project || null,
      items: items.map(
        /* 以当前显示版本防止静默覆盖。 */ (item) => ({
          session_id: item.session_id,
          expected_revision: item.management.revision,
        }),
      ),
    };
    const key = JSON.stringify({
      action,
      project,
      ids: items.map(/* 当前选择范围变化才开始新操作。 */ (item) => item.session_id),
    });
    if (retry.current?.key !== key)
      retry.current = { key, body: { ...payload, request_id: crypto.randomUUID() } };
    mutation.mutate(retry.current.body);
  }
  /** 显示next批量确认弹窗并清除上次错误；返回void，不立刻执行管理动作。 */
  function show(next: string): void {
    mutation.reset();
    retry.current = null;
    setAction(next);
  }
  const failures =
    mutation.data?.items.filter(
      /* 保留后端逐项说明，不把冲突变成整个页面异常。 */ (item) => !item.success,
    ) || [];
  return (
    <>
      {items.length > 0 && (
        <div className="conversation-batch-toolbar">
          <span>已选 {items.length} 项</span>
          {state === 'trashed' ? (
            <Button onClick={/* 回收站批量只提供可恢复操作。 */ () => show('restore')}>恢复</Button>
          ) : (
            <>
              <Button onClick={/* 移动前显示目标确认。 */ () => show('move')}>移动</Button>
              <Button
                onClick={
                  /* 归档与取消归档均按当前视图决定。 */ () =>
                    show(state === 'archived' ? 'unarchive' : 'archive')
                }
              >
                {state === 'archived' ? '取消归档' : '归档'}
              </Button>
              <Button
                aria-label="批量删除"
                danger
                onClick={/* 批量删除仍先进入回收站。 */ () => show('trash')}
              >
                删除
              </Button>
            </>
          )}
          <Button type="text" onClick={/* 取消选择不发送请求。 */ () => onDone([])}>
            取消选择
          </Button>
        </div>
      )}
      {failures.length > 0 && (
        <Alert
          type="warning"
          title={`${failures.length} 项操作未完成`}
          description={failures
            .map(
              /* 失败编号可与保留的选择对应。 */ (item) =>
                `${items.find((row) => row.session_id === item.session_id)?.title || item.session_id}：${item.error?.message}`,
            )
            .join('；')}
        />
      )}
      <Modal
        title={
          action === 'move'
            ? '批量移动对话'
            : action === 'trash'
              ? '批量删除对话'
              : action === 'restore'
                ? '批量恢复对话'
                : '批量整理对话'
        }
        open={action !== null}
        onOk={submit}
        confirmLoading={mutation.isPending}
        okText="确认"
        cancelText="取消"
        okButtonProps={{
          'aria-label': '确认批量操作',
          danger: action === 'trash',
          disabled: items.length === 0,
        }}
        cancelButtonProps={{ disabled: mutation.isPending }}
        onCancel={
          /* 请求进行中不关闭，避免重复确认。 */ () => {
            if (!mutation.isPending) setAction(null);
          }
        }
        closable={!mutation.isPending}
      >
        <p>将处理当前页选中的 {items.length} 条对话。忙碌或版本变化的项目会保留并说明原因。</p>
        {action === 'move' && (
          <>
            <Select
              aria-label="批量目标项目"
              className="conversation-target"
              value={project}
              disabled={mutation.isPending}
              virtual={false}
              showSearch={{ optionFilterProp: 'label' }}
              onChange={setProject}
              options={[
                { value: '', label: '未分组' },
                ...(projects.data?.recent_projects || []).map(
                  /* 同名目录通过路径区分。 */ (item) => ({
                    value: item.id,
                    label: item.label + ' · ' + item.path,
                  }),
                ),
              ]}
            />
            <p className="muted">仅调整归属，继续执行使用各自原来的目录。</p>
          </>
        )}
        {action === 'trash' && <p>删除后移入回收站，可恢复；不会回滚项目文件。</p>}
        {mutation.error && (
          <Alert type="error" title="批量操作未完成" description={errorMessage(mutation.error)} />
        )}
      </Modal>
    </>
  );
}
