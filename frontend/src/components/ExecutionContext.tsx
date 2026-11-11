import { FolderOpenOutlined } from '@ant-design/icons';
import { Select } from 'antd';
import { useId, useState } from 'react';
import type { ReactNode } from 'react';
import './ExecutionContext.css';

/** 在发送区展示已选项目、实际执行方式与模型，目录和基线按需展开。 */
export function ExecutionContext({
  project,
  path,
  mode,
  model,
  onModeChange,
  disabled,
  details,
}: {
  project: string; // 执行来源的项目名，与会话整理归属无关。
  path: string; // 宿主登记的原目录，或已建立会话的实际工作树。
  mode: 'local' | 'isolated'; // 首轮待提交或续轮已固定的执行方式。
  model?: string; // 继承的请求模型；首轮另有可编辑的模型选择器。
  onModeChange?: (mode: 'local' | 'isolated') => void; // 仅首轮 Git 项目提供切换。
  disabled?: boolean; // 已提交待确认时锁定原请求的执行目标。
  details?: ReactNode; // 允许首轮编辑基线和进入项目设置。
}) {
  const [expanded, setExpanded] = useState(false); // 发送区默认只显示常用上下文。
  const detailId = useId(); // 关联路径和基线的披露区域。
  return (
    <div className="execution-context" role="group" aria-label="执行位置与模型">
      <button
        type="button"
        className="execution-location"
        title={project}
        aria-expanded={expanded}
        aria-controls={detailId}
        onClick={/* 展开目录信息不改变实际执行选择。 */ () => setExpanded(!expanded)}
      >
        <FolderOpenOutlined aria-hidden="true" />
        <strong>{project}</strong>
        {!onModeChange && <span>{mode === 'isolated' ? '独立工作树' : '原目录'}</span>}
      </button>
      {onModeChange && (
        <Select
          aria-label="执行目录"
          className="execution-mode"
          size="small"
          value={mode}
          virtual={false}
          popupMatchSelectWidth={false}
          onChange={onModeChange}
          disabled={disabled}
          options={[
            { value: 'local', label: '使用所选目录' },
            { value: 'isolated', label: '创建独立 Git 工作树' },
          ]}
        />
      )}
      {model && (
        <span className="execution-model" title={model}>
          {model}
        </span>
      )}
      <div id={detailId} className="execution-location-details" hidden={!expanded}>
        <code>{path}</code>
        {details}
      </div>
    </div>
  );
}
