import { useEffect, useId, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button, Checkbox, Dropdown, Input, Pagination, Select } from 'antd';
import { FilterOutlined, CheckSquareOutlined } from '@ant-design/icons';
import { useSearchParams } from 'react-router';
import { errorMessage, getConversations, getOptions } from '../api';
import { ConversationList } from './ConversationList';
import { ConversationBatchActions } from './ConversationBatchActions';

/** 会话生命周期分类的视觉和键盘顺序。 */
const categories = [
  { value: 'active', label: '正常对话' },
  { value: 'archived', label: '已归档' },
  { value: 'trashed', label: '回收站' },
] as const;

/** 全局与项目共用搜索、生命周期和分页；筛选在后端完成，批量选择仅限当前页。 */
export function ConversationHistory({ projectId }: { projectId?: string }) {
  const [params, setParams] = useSearchParams();
  const categoryId = useId(); // 分类标签及筛选列表面板的可访问标识。
  const [selecting, setSelecting] = useState(false); // 用户主动进入多选后才显示复选框。
  const [selected, setSelected] = useState<Set<string>>(new Set()); // 筛选或页码变化清空，绝不跨页隐式全选。
  const parsed = Number(params.get('page'));
  const page = Number.isInteger(parsed) && parsed > 0 ? parsed : 1; // 非法页码使用第一页。
  const status = params.get('status') || ''; // 最新轮执行状态，和管理生命周期分别筛选。
  const state = ['archived', 'trashed'].includes(params.get('state') || '')
    ? params.get('state')!
    : 'active';
  const search = params.get('q') || ''; // 标题及首条需求搜索，读取不调用模型。
  const group = projectId || params.get('group') || ''; // 空表示全部，ungrouped表示未分组。
  const filterKey = [projectId, page, status, state, search, group].join('|'); // 批量选择的可见范围。
  const query = useQuery({
    queryKey: [
      'conversations',
      { projectId: group, offset: (page - 1) * 12, limit: 12, status, state, query: search },
    ],
    queryFn: /* 在分页之前按整理归属及状态筛选，不在页面拼接各轮。 */ ({ signal }) =>
      getConversations(
        {
          projectId: group && group !== 'ungrouped' ? group : undefined,
          ungrouped: group === 'ungrouped',
          offset: (page - 1) * 12,
          limit: 12,
          status,
          state,
          query: search,
        },
        signal,
      ),
    refetchInterval: 5000,
  });
  const options = useQuery({
    queryKey: ['options'],
    queryFn: /* 全局历史的项目筛选共用目录缓存。 */ ({ signal }) => getOptions(signal),
    enabled: !projectId,
    staleTime: 15000,
  });
  useEffect(/* 筛选和分页改变后清空选择。 */ () => setSelected(new Set()), [filterKey]);
  useEffect(
    /* 删除末页最后一项后回到有效页码，不出现无内容的高页码。 */ () => {
      if (query.data && page > Math.max(1, Math.ceil(query.data.total / 12)))
        update({ page: String(Math.max(1, Math.ceil(query.data.total / 12))) }, false);
    },
    [query.data?.total, page],
  );

  /** 用fields更新URL；reset=true回到第一页，否则仅改变指定页码，返回void。 */
  function update(fields: Record<string, string>, reset = true): void {
    const next = new URLSearchParams(params);
    if (reset) next.delete('page');
    for (const [key, value] of Object.entries(fields)) {
      if (value && !(key === 'page' && value === '1')) next.set(key, value);
      else next.delete(key);
    }
    setParams(next);
  }

  /** 切换生命周期 value；正常状态使用默认 URL，其余筛选条件保留。 */
  function chooseCategory(value: (typeof categories)[number]['value']): void {
    update({ state: value === 'active' ? '' : value });
  }

  /** 方向键循环切换分类，Home/End 直达两端，并将焦点移到新选中的标签。 */
  function categoryKeys(event: KeyboardEvent<HTMLDivElement>): void {
    const current = categories.findIndex(
      /* 当前分类由 URL 决定，键盘与点击使用同一事实。 */ (item) => item.value === state,
    );
    const last = categories.length - 1;
    let next: number;
    switch (event.key) {
      case 'ArrowRight':
        next = current === last ? 0 : current + 1;
        break;
      case 'ArrowLeft':
        next = current <= 0 ? last : current - 1;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = last;
        break;
      default:
        return;
    }
    event.preventDefault();
    chooseCategory(categories[next].value);
    event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  }
  /** 按checked切换id的本页选择；返回void，不变更会话管理状态。 */
  function select(id: string, checked: boolean): void {
    setSelected(
      /* 创建独立集合，保留本页其余显式选择。 */ (current) => {
        const next = new Set(current);
        if (checked) next.add(id);
        else next.delete(id);
        return next;
      },
    );
  }
  const items = query.data?.items || []; // 刷新失败时保留查询缓存中的已有会话。
  const selectable = items.filter(
    /* 清理中的会话只能单项重试清理。 */ (item) => item.management.state !== 'purging',
  );
  return (
    <section
      className="conversation-history"
      aria-label={projectId ? '项目会话列表' : '全部会话列表'}
    >
      <div className="section-heading">
        <h2>
          会话 <small>{query.data?.total ?? '—'}</small>
        </h2>
        <Button
          type="text"
          size="small"
          icon={<CheckSquareOutlined />}
          aria-pressed={selecting}
          aria-label={selecting ? '完成多选' : '多选'}
          onClick={
            /* 退出批量模式同时清空选择，不改变任何会话。 */ () => {
              setSelecting(!selecting);
              setSelected(new Set());
            }
          }
        >
          {selecting ? '完成多选' : '多选'}
        </Button>
      </div>
      <div
        className="conversation-category-tabs"
        role="tablist"
        aria-label="会话分类"
        onKeyDown={categoryKeys}
      >
        {categories.map(
          /* 分类直接切换服务端查询，保留搜索与当前项目范围。 */ (item) => (
            <button
              key={item.value}
              type="button"
              role="tab"
              id={categoryId + '-' + item.value}
              aria-controls={categoryId + '-panel'}
              tabIndex={state === item.value ? 0 : -1}
              aria-selected={state === item.value}
              onClick={/* 切换分类重置分页。 */ () => chooseCategory(item.value)}
            >
              {item.label}
            </button>
          ),
        )}
      </div>
      <div className="conversation-history-controls">
        <Input.Search
          aria-label="搜索对话"
          key={search}
          defaultValue={search}
          allowClear
          maxLength={200}
          placeholder="搜索标题或首条需求"
          onSearch={
            /* 明确搜索或清空时更新查询，不逐字请求。 */ (value) => update({ q: value.trim() })
          }
        />
        {!projectId && (
          <Select
            aria-label="筛选整理项目"
            value={group}
            virtual={false}
            onChange={/* 分组筛选不改变执行来源。 */ (value) => update({ group: value })}
            options={[
              { value: '', label: '全部项目' },
              { value: 'ungrouped', label: '未分组' },
              ...(options.data?.recent_projects || []).map(
                /* 项目身份来自受控登记。 */ (item) => ({ value: item.id, label: item.label }),
              ),
            ]}
          />
        )}
        <Dropdown
          trigger={['click']}
          menu={{
            selectedKeys: [status],
            onClick: /* 状态筛选不修改会话，只回到第一页重新读取。 */ ({ key }) =>
              update({ status: key }),
            items: [
              { key: '', label: '全部状态' },
              { key: 'running', label: '执行中' },
              { key: 'waiting_input', label: '等待你的决定' },
              { key: 'completed', label: '执行完成' },
              { key: 'failed', label: '执行失败' },
              { key: 'cancelled', label: '已取消' },
              { key: 'timed_out', label: '已超时' },
              { key: 'limit_exceeded', label: '预算耗尽' },
            ],
          }}
        >
          <Button
            type="text"
            icon={<FilterOutlined />}
            aria-label="筛选会话状态"
            className={status ? 'has-filter' : ''}
          >
            筛选{status ? ' · 1' : ''}
          </Button>
        </Dropdown>
      </div>
      {selecting && selectable.length > 0 && (
        <Checkbox
          checked={selectable.every(
            /* 全选仅覆盖当前页可选项。 */ (item) => selected.has(item.session_id),
          )}
          indeterminate={selected.size > 0 && selected.size < selectable.length}
          onChange={
            /* 明确选中或取消当前页。 */ (event) =>
              setSelected(
                new Set(event.target.checked ? selectable.map((item) => item.session_id) : []),
              )
          }
        >
          全选当前页
        </Checkbox>
      )}
      {selecting && (
        <ConversationBatchActions
          items={items.filter(
            /* 忽略已不在当前页的旧编号。 */ (item) => selected.has(item.session_id),
          )}
          state={state}
          onDone={/* 批量成功项移除，失败项保持选择。 */ (failed) => setSelected(new Set(failed))}
        />
      )}
      <div role="tabpanel" id={categoryId + '-panel'} aria-labelledby={categoryId + '-' + state}>
        <ConversationList
          items={items}
          loading={query.isPending}
          error={query.error ? errorMessage(query.error) : null}
          showProject={!projectId}
          selected={selecting ? selected : undefined}
          onSelect={selecting ? select : undefined}
        />
        {(query.data?.total || 0) > 12 && (
          <Pagination
            current={page}
            pageSize={12}
            total={query.data?.total || 0}
            showSizeChanger={false}
            onChange={
              /* 翻页只导航，不提交执行。 */ (value) => update({ page: String(value) }, false)
            }
          />
        )}
      </div>
    </section>
  );
}
