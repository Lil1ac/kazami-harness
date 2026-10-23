import { Checkbox } from 'antd';
import type { SkillOption } from '../types';

/** 技能手动激活控件；只提交名称，失效或达到八项上限时禁止新增选择。 */
export function SkillSelector({
  options,
  selected,
  onChange,
  label = '任务技能',
  disabled = false,
}: {
  options: SkillOption[]; // 宿主当前目录；不包含正文，也不能由浏览器改写来源。
  selected: string[]; // 当前明确选择；包含失效名称时仍允许取消。
  onChange: (names: string[]) => void; // 用户更改后返回新名称列表，[] 表示清空。
  label?: string; // 区分首次任务与续轮控件的可访问名称。
  disabled?: boolean; // 只在无法确认当前运行等情形禁用；活动运行可预先编辑。
}) {
  const known = new Set(options.map(/* 用目录名称识别已失效选择。 */ (skill) => skill.name));
  const missing = selected.filter(
    /* 旧选择即使已移出目录也须显示，便于用户取消。 */ (name) => !known.has(name),
  );
  return (
    <fieldset className="skill-selector" disabled={disabled} aria-label={label}>
      <legend>
        {label}
        <span className="muted"> · {selected.length}/8</span>
      </legend>
      <p className="muted">手动加载流程指导；工具、预算和验收由宿主配置控制。</p>
      {!options.length && !missing.length && (
        <p className="muted">暂无可用技能，宿主可登记技能目录。</p>
      )}
      {[
        ...options,
        ...missing.map(
          /* 已选文件缺失时允许移除，不把旧快照伪装成当前可用文件。 */ (name): SkillOption => ({
            name,
            description: '当前目录未找到此技能，请取消选择或由宿主恢复文件。',
            source: '',
            available: false,
          }),
        ),
      ].map(
        /* 每个技能同时展示名称、用途和不可用原因，不要求用户猜测标识。 */ (skill, index) => {
          const checked = selected.includes(skill.name);
          return (
            <div className="skill-choice" key={skill.source + ':' + skill.name + ':' + index}>
              <Checkbox
                checked={checked}
                disabled={disabled || (!checked && (!skill.available || selected.length >= 8))}
                onChange={
                  /* 保留既有选择顺序；取消最后一项返回 []，不会隐式恢复默认。 */ (event) =>
                    onChange(
                      event.target.checked
                        ? [...selected, skill.name]
                        : selected.filter(
                            /* 仅删除用户取消的这一项。 */ (name) => name !== skill.name,
                          ),
                    )
                }
              >
                {skill.name || '技能目录不可用'}
              </Checkbox>
              <div className="skill-description">{skill.description}</div>
              {!skill.name && skill.source && (
                <div className="skill-description">{skill.source}</div>
              )}
              {!skill.available && (
                <p className="skill-unavailable">{skill.error_message || '此技能当前不可用。'}</p>
              )}
            </div>
          );
        },
      )}
      {selected.length >= 8 && <p className="muted">最多选择 8 个技能，可取消后再调整。</p>}
    </fieldset>
  );
}
