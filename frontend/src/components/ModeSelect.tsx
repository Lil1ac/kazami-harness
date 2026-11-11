import { Select } from 'antd';
import { memo } from 'react';
import { CheckOutlined, DownOutlined, ThunderboltOutlined, AuditOutlined } from '@ant-design/icons';
import './ComposerSelect.css';

/** 模式选择输入；切换只设置下一次提交，不改变正在执行的权限。 */
interface ModeSelectProps {
  value: 'auto' | 'plan'; // auto 自动处理；plan 先生成待批准计划。
  onChange: (value: 'auto' | 'plan') => void; // 接收受控模式，不直接启动运行。
  label?: string; // 区分新建与下一轮的无障碍名称，默认工作模式。
  disabled?: boolean; // 已提交待确认时锁定工作模式。
}

/** 返回新建与续聊共用的两项菜单；value 标记当前模式，onChange 只更新下一次提交。 */
export const ModeSelect = memo(function ModeSelect({
  value,
  onChange,
  label = '工作模式',
  disabled,
}: ModeSelectProps) {
  return (
    <Select
      className="mode-select"
      aria-label={label}
      value={value}
      onChange={onChange}
      disabled={disabled}
      virtual={false}
      variant="borderless"
      size="small"
      popupMatchSelectWidth={136}
      classNames={{ popup: { root: 'composer-mode-menu' } }}
      suffixIcon={<DownOutlined />}
      options={[
        {
          value: 'auto',
          label: '自动',
          title: '根据要求执行，必要时先规划',
          icon: <ThunderboltOutlined />,
        },
        {
          value: 'plan',
          label: '先规划',
          title: '先提交计划，批准后执行',
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
        /* 每个选项一行；详细语义保留在提示中，键盘操作由 Select 管理。 */ (option) => (
          <span className="composer-mode-option">
            {option.data.icon}
            <span>{option.data.label}</span>
            {option.value === value && <CheckOutlined aria-hidden="true" />}
          </span>
        )
      }
    />
  );
});
