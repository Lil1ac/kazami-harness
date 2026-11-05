import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Form, Input, InputNumber } from 'antd';
import { useState } from 'react';
import { errorMessage, renameProject, saveProjectSettings } from '../api';
import { cacheProject } from '../projects';
import { HookSettings } from './HooksPanel';
import type { LocalProjectOption, OptionsResponse } from '../types';

/** 项目持久设置的表单值；仅保存配置，提交表单不执行命令。 */
interface ProjectSettingsValues {
  pythonExecutable: string; // 空白时清除专用解释器，运行时按 PATH 查找。
  command: string; // Git 项目公开测试命令，空白时取消自动测试。
  seconds?: number; // 公开测试秒级超时，允许 1..3600；普通目录不注册该字段，使用默认 30。
}

/** 独立保存project名称并展示绑定目录；名称保存不依赖解释器可用性，返回项目身份表单。 */
function ProjectIdentityForm({ project }: { project: LocalProjectOption }) {
  const cache = useQueryClient();
  const [saved, setSaved] = useState(false); // 只在服务端确认后显示保存结果。
  const mutation = useMutation({
    mutationFn: /* 去除首尾空白，后端再次校验名称边界。 */ (values: { label: string }) =>
      renameProject(project.id, values.label.trim()),
    onSuccess: /* 侧栏、首页和已有会话标题同步展示新名称。 */ (updated) => {
      cacheProject(cache, updated, false);
      setSaved(true);
    },
  });
  return (
    <Form
      layout="vertical"
      requiredMark={false}
      initialValues={{ label: project.label }}
      onFinish={/* 校验通过才更新名称，不操作项目文件。 */ (values) => mutation.mutate(values)}
      onValuesChange={/* 修改草稿后清除上次保存提示。 */ () => setSaved(false)}
      className="project-identity-form"
    >
      <Form.Item
        name="label"
        label="项目名称"
        rules={[{ required: true, whitespace: true, message: '请输入项目名称' }]}
      >
        <Input maxLength={100} />
      </Form.Item>
      <Form.Item label="绑定文件夹">
        <Input aria-label="绑定文件夹" readOnly value={project.path} />
      </Form.Item>
      {mutation.error && (
        <Alert type="error" title="名称保存失败" description={errorMessage(mutation.error)} />
      )}
      <div className="settings-actions">
        <Button htmlType="submit" loading={mutation.isPending}>
          保存项目名称
        </Button>
        {saved && (
          <span role="status" className="applied-note">
            项目名称已保存
          </span>
        )}
      </div>
    </Form>
  );
}

/** 编辑 project 的后续运行环境并返回表单；保存失败保留输入，成功后同步全站项目快照。 */
export function ProjectSettingsPanel({ project }: { project: LocalProjectOption }) {
  const [form] = Form.useForm<ProjectSettingsValues>();
  const [saved, setSaved] = useState(false); // 仅确认配置已落盘，不表示测试通过。
  const cache = useQueryClient();
  const mutation = useMutation({
    mutationFn: /* 普通目录只设置解释器；测试命令只适用于 Git 项目。 */ (
      values: ProjectSettingsValues,
    ) =>
      saveProjectSettings(
        project.id,
        project.kind === 'project' ? values.command : '',
        values.seconds ?? 30,
        values.pythonExecutable,
      ),
    onSuccess: /* 使用服务器返回值刷新界面，不以草稿伪造已保存事实。 */ (updated) => {
      cache.setQueryData<OptionsResponse>(
        ['options'],
        /* 保留原排序与其他项目。 */ (current) =>
          current
            ? {
                ...current,
                recent_projects: current.recent_projects.map(
                  /* 稳定编号匹配同一项目。 */ (item) => (item.id === updated.id ? updated : item),
                ),
              }
            : current,
      );
      form.setFieldsValue({
        pythonExecutable: updated.python_executable || '',
        command: updated.test_command || '',
        seconds: updated.test_timeout_seconds || 30,
      });
      setSaved(true);
    },
  });
  return (
    <>
      <ProjectIdentityForm project={project} />
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        className="project-settings-form"
        initialValues={{
          pythonExecutable: project.python_executable || '',
          command: project.test_command || '',
          seconds: project.test_timeout_seconds || 30,
        }}
        onFinish={/* 校验通过才发送保存请求。 */ (values) => mutation.mutate(values)}
        onValuesChange={/* 编辑后撤去此前保存确认。 */ () => setSaved(false)}
      >
        <p className="muted">用于该项目后续运行。保存不会执行命令。</p>
        <Form.Item
          name="pythonExecutable"
          label="Python 解释器"
          extra="留空按 PATH 查找；相对路径以实际执行目录为准。"
        >
          <Input maxLength={4096} placeholder="例如 .venv/Scripts/python.exe" />
        </Form.Item>
        {project.kind === 'project' && (
          <>
            <Form.Item
              name="command"
              label="项目测试命令"
              extra="任务完成后执行；留空并保存可取消测试。"
            >
              <Input maxLength={8192} placeholder="例如 python -m pytest -q" />
            </Form.Item>
            <Form.Item
              name="seconds"
              label="测试超时（秒）"
              rules={[
                {
                  required: true,
                  type: 'number',
                  min: 1,
                  max: 3600,
                  message: '请输入 1 至 3600 秒',
                },
              ]}
            >
              <InputNumber min={1} max={3600} precision={0} />
            </Form.Item>
          </>
        )}
        {mutation.error && (
          <Alert type="error" title="保存失败" description={errorMessage(mutation.error)} />
        )}
        <div className="settings-actions">
          <Button type="primary" htmlType="submit" loading={mutation.isPending}>
            保存项目设置
          </Button>
          {saved && (
            <span role="status" className="applied-note">
              项目设置已保存
            </span>
          )}
        </div>
      </Form>
      <HookSettings projectId={project.id} />
    </>
  );
}
