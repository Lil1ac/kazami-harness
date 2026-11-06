import { Checkbox } from 'antd';
import type { McpToolOption, McpToolSelection } from '../mcpTypes';

/** 选择本轮外部 options；selected 固定版本，onChange 只改草稿，disabled 时不可编辑。 */
export function McpSelector({
  options,
  selected,
  onChange,
  disabled = false,
}: {
  options: McpToolOption[]; // 当前已审核启用目录。
  selected: McpToolSelection[]; // 本轮选择，最多16项。
  onChange: (value: McpToolSelection[]) => void; // 返回新选择，[] 明确关闭。
  disabled?: boolean; // 待确认发送时禁止改变冻结载荷。
}) {
  if (!options.length && !selected.length) return null;
  const missing = selected.filter(
    /* 旧版本不静默替换为新授权。 */ (item) =>
      !options.some(
        (option) => option.name === item.name && option.fingerprint === item.fingerprint,
      ),
  );
  return (
    <fieldset className="skill-selector mcp-selector" disabled={disabled} aria-label="外部工具">
      <legend>外部工具 · {selected.length}/16</legend>
      {options.map(
        /* 每项绑定当时的完整定义指纹。 */ (option) => {
          const checked = selected.some(
            (item) => item.name === option.name && item.fingerprint === option.fingerprint,
          );
          return (
            <div className="skill-choice" key={option.name}>
              <Checkbox
                checked={checked}
                disabled={disabled || (!checked && selected.length >= 16)}
                onChange={
                  /* 重新勾选新版是用户的明确动作。 */ (event) =>
                    onChange(
                      event.target.checked
                        ? [
                            ...selected.filter((item) => item.name !== option.name),
                            { name: option.name, fingerprint: option.fingerprint },
                          ]
                        : selected.filter((item) => item.name !== option.name),
                    )
                }
              >
                {option.server_label} / {option.tool_name}
              </Checkbox>
              {option.description && <div className="skill-description">{option.description}</div>}
            </div>
          );
        },
      )}
      {missing.map(
        /* 无效选择仍可取消，但不再可调用。 */ (item) => (
          <div key={item.name}>
            <Checkbox
              checked
              onChange={
                /* 只删除该失效选择。 */ () =>
                  onChange(selected.filter((value) => value.name !== item.name))
              }
            >
              工具已停用或版本变化：{item.name}
            </Checkbox>
          </div>
        ),
      )}
    </fieldset>
  );
}
