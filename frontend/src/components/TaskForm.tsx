import { useState } from 'react';
import { Alert, Button, Form, Input, Radio, Select } from 'antd';
import { ArrowRightOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { createRun, errorMessage } from '../api';
import { useConfigDraft, effectiveConfig } from '../config';
import { formatContextBytes } from '../context';
import type { OptionsResponse } from '../types';

/** 提交表单的本地值；outputs 为每行一个相对路径，提交时再拆分。 */
interface TaskValues {
  prompt: string; // 任意非空任务文本。
  target: 'workspace' | 'project'; // 任务目标类型，只在登记了 Git 项目时显示切换。
  workspace_id?: string; // 普通任务使用的已登记目录标识。
  project_id?: string; // 代码任务使用的已登记 Git 项目标识。
  base_ref?: string; // 代码基线，空白表示 HEAD。
  verifier_id?: string; // 空值表示不验收。
  outputs?: string; // 用户输入的多行产物路径。
}

/** 提交任务并保留失败时的输入；options 来自宿主能力接口，busyId 来自最新任务列表。 */
export function TaskForm({ options, busyId }: { options: OptionsResponse; busyId: string | null }) {
  const [form] = Form.useForm<TaskValues>(); // 输入保留在挂载页面中，提交失败不重置。
  const [demoNotice, setDemoNotice] = useState(false); // 仅在用户主动填充样例后显示适用范围。
  const { draft, setDraft } = useConfigDraft();
  const config = effectiveConfig(options.config, draft);
  const navigate = useNavigate();
  const cache = useQueryClient();
  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: /* 登记成功才导航，避免请求失败丢失草稿。 */ (run) => {
      cache.setQueryData(['run', run.run_id], run);
      void cache.invalidateQueries({ queryKey: ['runs'] });
      void cache.invalidateQueries({ queryKey: ['options'] });
      void navigate(`/runs/${run.run_id}`);
    },
  });
  const target = Form.useWatch('target', form) || 'workspace';
  const projects = options.projects || [];
  const selectedProject = Form.useWatch('project_id', form);
  const project = projects.find(
    /* 项目说明来自宿主登记，页面只传标识与提交引用。 */ (item) => item.id === selectedProject,
  );
  const selectedVerifier = Form.useWatch('verifier_id', form);
  const verifier = options.verifiers.find(
    /* 选择宿主预设，仅用于解释适用范围。 */ (item) => item.id === selectedVerifier,
  );
  const logWorkspace = options.workspaces.find(
    /* 日志演示只选登记的隔离样例目录。 */ (item) => item.id.includes('logs'),
  );
  const logVerifier = options.verifiers.find(
    /* 固定日志验收器不会被自动用于任意任务。 */ (item) => item.id.includes('logs'),
  );

  /** 填充可编辑日志任务，之后仍走普通提交 API；不隐式创建或执行任务。 */
  function fillDemo(): void {
    if (!logWorkspace) return;
    form.setFieldsValue({
      prompt:
        '读取工作区 app.log，按 ERROR 行中的错误类型统计次数。编写 analyze_logs.py，运行它生成 report.json，包含 counts 和 total_errors。不要统计 WARN 或 INFO。读取生成的报告核对结果，并如实说明完成情况。',
      target: 'workspace',
      workspace_id: logWorkspace.id,
      verifier_id: logVerifier?.id,
      outputs: 'analyze_logs.py\nreport.json',
    });
    setDraft({ ...draft, tools: ['read_file', 'search_files', 'write_file', 'run_command'] });
    mutation.reset();
    setDemoNotice(true);
  }

  /** 规范化空白与路径行后提交；网络与业务错误交给表单顶部显示，不清空字段。 */
  function submit(values: TaskValues): void {
    mutation.mutate({
      prompt: values.prompt.trim(),
      ...(values.target === 'project'
        ? { project_id: values.project_id!, base_ref: values.base_ref?.trim() || 'HEAD' }
        : { workspace_id: values.workspace_id!, verifier_id: values.verifier_id || null }),
      expected_outputs: [
        ...new Set(
          (values.outputs || '')
            .split(/\r?\n/)
            .map(/* 去除每行两端空白。 */ (line) => line.trim())
            .filter(Boolean),
        ),
      ],
      config: draft,
    });
  }

  return (
    <section className="task-composer" aria-labelledby="new-task-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">NEW RUN</span>
          <h2 id="new-task-heading">提交任务</h2>
        </div>
        {logWorkspace && (
          <Button type="text" size="small" onClick={fillDemo}>
            填充日志样例
          </Button>
        )}
      </div>
      <p className="muted">描述目标，让 Agent 在选定工作区中执行。</p>
      {!options.key_configured && (
        <Alert
          type="warning"
          showIcon
          title={`宿主未配置 ${options.config.api_key_env}`}
          description="请在启动后端的终端中设置环境变量，再启动服务。"
        />
      )}
      {busyId && (
        <Alert
          type="info"
          showIcon
          title="已有任务正在运行"
          description={
            <span>
              当前只允许同时运行一个任务。
              <Button
                type="link"
                size="small"
                onClick={/* 导航查看占用运行槽的任务。 */ () => void navigate(`/runs/${busyId}`)}
              >
                查看任务
              </Button>
            </span>
          }
        />
      )}
      {mutation.error && (
        <Alert
          type="error"
          showIcon
          title="提交未完成"
          description={errorMessage(mutation.error)}
          closable
          onClose={/* 清理错误提示，保留输入。 */ () => mutation.reset()}
        />
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={{
          target: options.workspaces.length
            ? 'workspace'
            : projects.length
              ? 'project'
              : 'workspace',
          workspace_id: options.workspaces[0]?.id,
          project_id: projects.find(/* 仅预选当前可用项目。 */ (item) => item.available !== false)
            ?.id,
          base_ref: 'HEAD',
        }}
        requiredMark="optional"
      >
        <Form.Item
          label="任务描述"
          name="prompt"
          rules={[{ required: true, whitespace: true, message: '请描述要完成的任务' }]}
        >
          <Input.TextArea
            rows={6}
            maxLength={20000}
            placeholder="例如：统计 app.log 中各类错误，生成分析脚本和报告。"
          />
        </Form.Item>
        {projects.length > 0 && (
          <Form.Item label="任务目标" name="target">
            <Radio.Group
              optionType="button"
              options={[
                { label: '普通工作区', value: 'workspace', disabled: !options.workspaces.length },
                { label: '代码项目', value: 'project' },
              ]}
            />
          </Form.Item>
        )}
        {target === 'project' ? (
          <>
            <Form.Item
              label="代码项目"
              name="project_id"
              rules={[{ required: true, message: '请选择已登记代码项目' }]}
            >
              <Select
                options={projects.map(
                  /* 不可用项目保留说明，但禁止提交。 */ (item) => ({
                    value: item.id,
                    label: `${item.label}${item.available === false ? ' · 暂不可用' : ''}`,
                    disabled: item.available === false,
                  }),
                )}
              />
            </Form.Item>
            {!project &&
              projects.every(
                /* 不可用时直接说明，不强迫用户打开下拉列表查找。 */ (item) =>
                  item.available === false,
              ) && (
                <Alert
                  type="error"
                  title="当前没有可用代码项目"
                  description={projects
                    .map(/* 合并宿主登记错误。 */ (item) => item.error || item.label)
                    .join('；')}
                />
              )}
            <Form.Item
              label="提交基线"
              name="base_ref"
              extra="从该引用解析到的提交建立独立工作树；不包含原目录中的未提交改动。"
            >
              <Input placeholder="HEAD" maxLength={200} />
            </Form.Item>
            {project && (
              <div className="project-preset" aria-label="项目预设说明">
                <dl className="compact-dl">
                  <div>
                    <dt>原始仓库</dt>
                    <dd>{project.path}</dd>
                  </div>
                  <div>
                    <dt>当前 HEAD</dt>
                    <dd>
                      <code>{project.head_commit || '无法读取'}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>原目录</dt>
                    <dd>{project.source_dirty ? '有未提交改动 · 不带入任务' : '无未提交改动'}</dd>
                  </div>
                  <div>
                    <dt>项目测试</dt>
                    <dd>
                      <code>{project.test_argv.join(' ')}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>测试超时</dt>
                    <dd>{project.test_timeout_seconds} 秒</dd>
                  </div>
                </dl>
                <p className="muted">测试命令由宿主登记，Agent 结束后在本任务工作树中执行。</p>
                {project.error && <Alert type="error" title={project.error} />}
              </div>
            )}
            <p className="inline-note">
              代码任务通常需要读取、搜索、写入和命令工具，请在运行配置中确认。
            </p>
          </>
        ) : (
          <>
            <Form.Item
              label="工作区"
              name="workspace_id"
              rules={[{ required: true, message: '请选择已登记工作区' }]}
            >
              <Select
                options={options.workspaces.map(
                  /* 标签解释是否每次复制，实际目录由服务端决定。 */ (item) => ({
                    value: item.id,
                    label: `${item.label}${item.copy_on_run ? ' · 每次独立副本' : ''}`,
                  }),
                )}
              />
            </Form.Item>
            <Form.Item
              label="独立验收"
              name="verifier_id"
              extra={verifier?.description || '不选择时，执行完成会明确标记为“未验收”。'}
            >
              <Select
                allowClear
                placeholder="不进行独立验收"
                options={options.verifiers.map(
                  /* 仅向后端提交登记 ID。 */ (item) => ({ value: item.id, label: item.label }),
                )}
              />
            </Form.Item>
          </>
        )}
        {demoNotice && target !== 'project' && (
          <p className="inline-note">
            样例已启用读取、搜索、写入、命令四类工具。固定日志验收仅适用于这份输入，修改任务后请检查验收器选择。
          </p>
        )}
        <Form.Item
          label="预期产物"
          name="outputs"
          extra="每行一个工作区相对路径；声明路径不代表内容已验收。"
        >
          <Input.TextArea rows={2} placeholder={'analyze_logs.py\nreport.json'} />
        </Form.Item>
        <div className="composer-config">
          <span>{config.requested_model}</span>
          <span>
            {config.tools.length} 类工具
            {config.context.compression && config.context.compression !== 'off'
              ? ' + 历史回读'
              : ''}
          </span>
          <span>输入上限 {formatContextBytes(config.context.max_input_bytes)}</span>
          <Button
            type="link"
            size="small"
            onClick={/* 本地配置只影响后续提交。 */ () => void navigate('/settings')}
          >
            调整配置
          </Button>
        </div>
        <Button
          type="primary"
          htmlType="submit"
          size="large"
          block
          loading={mutation.isPending}
          disabled={
            Boolean(busyId) ||
            !options.key_configured ||
            (target === 'project' && (!project || project.available === false))
          }
          icon={<ArrowRightOutlined />}
          iconPlacement="end"
        >
          开始执行
        </Button>
      </Form>
    </section>
  );
}
