import { Select } from 'antd';
import { memo } from 'react';
import { DownOutlined, ThunderboltOutlined, AuditOutlined } from '@ant-design/icons';
import './ComposerSelect.css';

/** 模式选择输入；切换只设置下一次提交，不改变正在执行的权限。 */
interface ModeSelectProps {
  value: 'auto' | 'plan'; // auto 自动处理；plan 先生成待批准计划。
  onChange: (value: 'auto' | 'plan') => void; // 接收受控模式，不直接启动运行。
  label?: string; // 区分新建与下一轮的无障碍名称，默认工作模式。
}

/** 返回新建与续聊共用的紧凑模式入口；菜单展示行为说明，关闭后只保留当前模式。 */
export const ModeSelect = memo(function ModeSelect({
  value,
  onChange,
  label = '工作模式',
}: ModeSelectProps) {
  return (
    <Select
      className="mode-select"
      aria-label={label}
      value={value}
      onChange={onChange}
      virtual={false}
      variant="borderless"
      size="small"
      popupMatchSelectWidth={248}
      classNames={{ popup: { root: 'composer-select-menu' } }}
      suffixIcon={<DownOutlined />}
      options={[
        {
          value: 'auto',
          label: '自动',
          description: '根据要求执行，必要时先规划',
          icon: <ThunderboltOutlined />,
        },
        {
          value: 'plan',
          label: '先规划',
          description: '先研究并提交计划，批准后再执行',
          icon: <AuditOutlined />,
        },
      ]}
      labelRender={
        /* 已选模式保持一行，避免说明文字占用编辑空间。 */ (option) => (
          <span className="mode-current">
            {option.value === 'plan' ? <AuditOutlined /> : <ThunderboltOutlined />}
            {option.label}
          </span>
        )
      }
      optionRender={
        /* 说明跟随固定模式数据，键盘选项仍由 Select 管理。 */ (option) => (
          <div className="mode-option">
            {option.data.icon}
            <div>
              <strong>{option.data.label}</strong>
              <small>{option.data.description}</small>
            </div>
          </div>
        )
      }
    />
  );
});
