import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Form, Input, Modal, Select } from 'antd';
import { FolderOpenOutlined, PlusOutlined } from '@ant-design/icons';
import { createProject, errorMessage, openProject, pickProject } from '../api';
import { cacheProject } from '../projects';
import type { LocalProjectOption, ProjectSelection } from '../types';

/** 首页项目选择与确认创建；value是当前项目，onChange只接收真实保存/重新核验的登记。 */
export function ProjectSelector({
  projects,
  value,
  pickerAvailable,
  disabled,
  onChange,
  onBusyChange,
}: {
  projects: LocalProjectOption[]; // 当前宿主登记，按最近打开排序。
  value?: LocalProjectOption; // 未选择时为空，不能据此发起任务。
  pickerAvailable?: boolean; // false时解释原生选择不可用，undefined允许尝试。
  disabled?: boolean; // 原提交待确认时锁定项目，避免展示不同执行目标。
  onChange: (project: LocalProjectOption) => void; // 成功后选择项目，不清除任务文字。
  onBusyChange: (busy: boolean) => void; // 弹窗或切换请求期间禁止向旧项目发送任务。
}) {
  const cache = useQueryClient();
  const [visible, setVisible] = useState(false); // 创建弹窗只由明确点击打开。
  const [selection, setSelection] = useState<ProjectSelection | null>(null); // 尚未确认的原生选择，关闭时丢弃。
  const [form] = Form.useForm<{ label: string }>(); // 项目名草稿；失败时保留。
  const choices =
    value && !projects.some(/* 接纳刚保存而尚未刷新的登记。 */ (item) => item.id === value.id)
      ? [value, ...projects]
      : projects;

  /** 接纳真实project并同步全站缓存，返回void；不调用模型或改变任务文字。 */
  function accept(project: LocalProjectOption): void {
    cacheProject(cache, project);
    onChange(project);
  }
  const opening = useMutation({ mutationFn: openProject, onSuccess: accept });
  const picking = useMutation({
    mutationFn: pickProject,
    onSuccess: /* 取消原生选择时保留上次目录和名称。 */ (result) => {
      if (!result.selection) return;
      setSelection(result.selection);
      if (result.selection.existing_project_id || !form.isFieldTouched('label')) {
        form.setFieldsValue({ label: result.selection.label });
      }
    },
  });
  const creating = useMutation({
    mutationFn: /* 只提交选择凭据和名称，不接受页面自由路径。 */ (values: { label: string }) =>
      createProject(selection!.selection_id, values.label.trim()),
    onSuccess: /* 确认落盘后关闭弹窗并选中项目。 */ (project) => {
      accept(project);
      setVisible(false);
      setSelection(null);
    },
  });
  const pending = picking.isPending || creating.isPending; // 原生窗口或保存完成前，禁止重复点击和关闭。
  useEffect(
    /* 将异步选择状态同步给发送区，卸载时解除当前表单阻塞。 */ () => {
      onBusyChange(visible || opening.isPending);
      return () => onBusyChange(false);
    },
    [visible, opening.isPending, onBusyChange],
  );

  /** 打开空白创建弹窗，重置上次结果与错误；无参数，返回void，不弹原生窗口。 */
  function showCreate(): void {
    form.resetFields();
    setSelection(null);
    picking.reset();
    creating.reset();
    opening.reset();
    setVisible(true);
  }
  return (
    <div className="project-selector">
      <div className="project-selector-row">
        <Select
          aria-label="选择项目"
          placeholder="选择项目"
          className="project-select"
          value={value?.id}
          showSearch={{ optionFilterProp: 'search' }}
          virtual={false}
          loading={opening.isPending}
          disabled={disabled || opening.isPending}
          onChange={/* 切换只重新核验已登记项目，不重新弹目录选择框。 */ (id) => opening.mutate(id)}
          options={choices.map(
            /* 同名项目通过绑定路径区分，路径也参与搜索。 */ (project) => ({
              value: project.id,
              label: project.label,
              search: project.label + ' ' + project.path,
              title: project.path,
              // 不可用项目保留可见性，避免刷新时悄悄切换到其他目录。
              disabled: project.available === false,
              content: project,
            }),
          )}
          optionRender={
            /* 下拉项用两行表达名称与目录，不重复添加辅助标签。 */ (option) => (
              <div className="project-select-option">
                <strong>{option.data.label}</strong>
                <span>{option.data.title}</span>
                {option.data.content.error && <small>{option.data.content.error}</small>}
              </div>
            )
          }
          notFoundContent="没有匹配的项目"
        />
        <Button
          aria-label="新建项目"
          icon={<PlusOutlined />}
          onClick={showCreate}
          disabled={disabled || opening.isPending}
        >
          新建项目
        </Button>
      </div>
      {!value && (
        <p className="project-selection-empty">选择一个项目，或新建项目并绑定文件夹。</p>
      )}
      {opening.error && (
        <Alert type="error" title="切换项目失败" description={errorMessage(opening.error)} />
      )}
      <Modal
        title="新建项目"
        open={visible}
        okText={selection?.existing_project_id ? '使用已有项目' : '创建项目'}
        cancelText="取消"
        confirmLoading={creating.isPending}
        okButtonProps={{ disabled: !selection || pending }}
        cancelButtonProps={{ disabled: pending, 'aria-label': '取消新建项目' }}
        closable={!pending}
        mask={{ closable: !pending }}
        keyboard={!pending}
        onOk={/* 名称校验通过后才确认目录绑定。 */ () => form.submit()}
        onCancel={
          /* 取消草稿不改变登记或当前项目。 */ () => {
            setVisible(false);
            setSelection(null);
          }
        }
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={
            /* 选择目录后才保存。 */ (values) => {
              if (selection) creating.mutate(values);
            }
          }
        >
          <Form.Item
            name="label"
            label="项目名称"
            rules={[{ required: true, whitespace: true, message: '请输入项目名称' }]}
          >
            <Input
              maxLength={100}
              placeholder="例如：订单服务"
              disabled={pending || Boolean(selection?.existing_project_id)}
            />
          </Form.Item>
          <Form.Item label="绑定文件夹">
            <div className="project-folder-choice">
              <Input
                aria-label="绑定文件夹"
                readOnly
                value={selection?.path || ''}
                placeholder="尚未选择文件夹"
              />
              <Button
                aria-label="选择文件夹"
                icon={<FolderOpenOutlined />}
                onClick={/* 明确点击才打开系统目录窗口。 */ () => picking.mutate()}
                loading={picking.isPending}
                disabled={creating.isPending || pickerAvailable === false}
              >
                选择文件夹
              </Button>
            </div>
          </Form.Item>
          {pickerAvailable === false && (
            <Alert
              type="warning"
              title="当前环境无法打开系统文件夹选择器，请在运行服务的本机操作。"
            />
          )}
          {picking.isPending && <p role="status">请在系统窗口中选择文件夹。</p>}
          {selection?.existing_project_id && (
            <Alert type="info" title="该文件夹已绑定项目，将直接使用已有项目。" />
          )}
          {(picking.error || creating.error) && (
            <Alert
              type="error"
              title="创建未完成"
              description={errorMessage(picking.error || creating.error)}
            />
          )}
        </Form>
      </Modal>
    </div>
  );
}
