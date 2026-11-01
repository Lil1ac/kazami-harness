import { Alert, Tag } from 'antd';
import { useId } from 'react';
import { CompressionActions } from './CompressionActions';
import { formatContextBytes, instructionErrorLabel, instructionStatusLabels } from '../context';
import type { ContextView } from '../types';
import { memoryPhaseLabels } from '../memory';
import { MemoryBody, MemorySource } from './MemoryPanel';

/** 从持久化聚合快照展示项目规则与最新输入检查；null 或旧接口缺省都保持“未记录”。 */
export function ContextPanel({
  context,
  stopReason,
}: {
  context: ContextView | null | undefined; // 由 RunView 读取的完整证据，不从当前 SSE 窗口拼装。
  stopReason: string | null; // 真实运行结束原因；预算超限和指令错误分别解释。
}) {
  const headingId = useId(); // 父上下文和多个子执行可以同时展开，标题引用必须各自唯一。
  if (!context)
    return (
      <section className="context-panel" aria-labelledby={headingId}>
        <h2 id={headingId}>上下文</h2>
        <p className="muted">
          未记录上下文数据。旧任务或尚未准备上下文的任务无法展示加载与输入大小。
        </p>
      </section>
    );
  const { instruction, last_check: check } = context;
  const ratio = check ? (check.input_bytes / check.max_input_bytes) * 100 : null;
  return (
    <section className="context-panel" aria-labelledby={headingId}>
      <div className="section-heading">
        <div>
          <h2 id={headingId}>上下文</h2>
        </div>
        <span className="muted">UTF-8 JSON 字节数</span>
      </div>
      <p className="muted">
        统计消息和工具定义的整体输入大小，不等于模型 token 数。每轮发送前检查。
      </p>
      <section className="context-budget" aria-labelledby={`${headingId}-budget`}>
        <h3 id={`${headingId}-budget`}>最近一次输入检查</h3>
        {check ? (
          <>
            <div className="context-budget-total">
              <strong>{formatContextBytes(check.input_bytes)}</strong>
              <span>/ {formatContextBytes(check.max_input_bytes)}</span>
              <Tag color={check.allowed ? 'success' : 'error'} variant="filled">
                {check.allowed ? '允许请求' : '达到上限'}
              </Tag>
            </div>
            <progress aria-label="模型输入预算占比" value={Math.min(ratio!, 100)} max={100} />
            <p className="muted">
              占用 {ratio!.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}% · 拟请求第{' '}
              {check.model_turn} 轮 · {check.message_count} 条消息
            </p>
            <p className="muted">
              实际 {check.input_bytes.toLocaleString('zh-CN')} 字节，上限{' '}
              {check.max_input_bytes.toLocaleString('zh-CN')} 字节。
            </p>
            {!check.allowed && (
              <Alert
                type="warning"
                showIcon
                title="整体输入达到上限，未发送本轮模型请求"
                description="此前的执行记录与文件改动已保留。请调整任务或后续任务的输入上限。"
              />
            )}
          </>
        ) : (
          <p className="muted">尚无输入检查记录。</p>
        )}
        {check?.raw_input_bytes != null && (
          <p className="muted">
            原始输入 {formatContextBytes(check.raw_input_bytes)}，当前发送{' '}
            {formatContextBytes(check.input_bytes)}。
          </p>
        )}
        {check?.estimated_input_tokens != null && (
          <p className="muted">
            本地估算约 {check.estimated_input_tokens.toLocaleString('zh-CN')} token
            {check.max_input_tokens != null
              ? ' / 输入预算 ' + check.max_input_tokens.toLocaleString('zh-CN')
              : '（未声明模型窗口，仅执行字节限制）'}
            。 这是启发式估算；实际用量以供应商返回为准。
          </p>
        )}
        {check?.purpose === 'summary' && <p className="muted">此次检查用于生成历史工作摘要。</p>}
        {stopReason === 'max_model_input_tokens' && (
          <Alert type="warning" title="估算输入超过已声明的 token 预算，未发出请求" />
        )}
        {stopReason === 'context_summary_invalid' && (
          <Alert type="warning" title="摘要无效或没有缩小输入，历史替换未采用" />
        )}
        {stopReason === 'max_model_input_bytes' && (
          <p className="stop-reason">
            结束原因 <code>max_model_input_bytes</code>：整体模型输入超过本次限制。
          </p>
        )}
      </section>
      <section className="context-skills" aria-labelledby={`${headingId}-skills`}>
        <h3 id={`${headingId}-skills`}>本轮技能快照</h3>
        {context.skill_error && (
          <Alert
            type="error"
            showIcon
            title={
              context.skill_error.name
                ? '技能加载失败：' + context.skill_error.name
                : '技能加载失败'
            }
            description={
              <>
                {context.skill_error.message} <code>{context.skill_error.code}</code>
              </>
            }
          />
        )}
        {context.skills === undefined ? (
          <p className="muted">此记录未保存技能快照，无法推断当时的加载情况。</p>
        ) : !context.skills.length && !context.skill_error ? (
          <p className="muted">本轮未选择技能。</p>
        ) : (
          context.skills.map(
            /* 仅读取本轮持久化内容；目录当前版本不会覆盖这份历史。 */ (skill) => (
              <details className="skill-snapshot" key={skill.name}>
                <summary>
                  {skill.name} · {formatContextBytes(skill.size_bytes)}
                </summary>
                <p>{skill.description}</p>
                <dl className="compact-dl">
                  <div>
                    <dt>来源</dt>
                    <dd>{skill.source}</dd>
                  </div>
                  <div>
                    <dt>文件大小</dt>
                    <dd>{skill.size_bytes.toLocaleString('zh-CN')} 字节</dd>
                  </div>
                  <div>
                    <dt>SHA-256</dt>
                    <dd>
                      <code>{skill.sha256}</code>
                    </dd>
                  </div>
                </dl>
                <p className="muted">建立本轮运行时固定的完整正文，后续文件修改不会改变此快照。</p>
                <pre
                  className="context-instruction-text"
                  aria-label={'技能 ' + skill.name + ' 正文'}
                >
                  {skill.content}
                </pre>
              </details>
            ),
          )
        )}
      </section>
      <CompressionActions context={context} />
      {context.memory && (
        <section className="memory-snapshot">
          <h3>本轮项目记忆</h3>
          <p>
            读取{context.memory.use ? '开启' : '关闭'} · 候选
            {context.memory.capture ? '开启' : '关闭'} · 固定版本 {context.memory.sequence}
          </p>
          {context.memory.selected.map(
            /* 使用当轮正文，不回查当前库。 */ (item) => (
              <details key={item.id}>
                <summary>
                  {item.title} · v{item.revision}
                </summary>
                <MemoryBody item={item} />
                <MemorySource item={item} />
              </details>
            ),
          )}
          {context.memory.uses && context.memory.uses.length > 0 && (
            <details>
              <summary>各次模型输入 · {context.memory.uses.length} 次</summary>
              {context.memory.uses.map(
                /* 严格使用该次真实输入，不回查当前库。 */ (use) => (
                  <div key={use.model_turn}>
                    <p>
                      模型请求 {use.model_turn} · {memoryPhaseLabels[use.phase]} ·{' '}
                      {use.paths.join('、') || '项目范围'}
                    </p>
                    {use.selected.map(
                      /* 保存类型呈现及采用原因。 */ (item) => (
                        <details key={item.id}>
                          <summary>
                            {item.title} · {item.presentation}
                          </summary>
                          <pre>{item.usage_text}</pre>
                          <MemorySource item={item} />
                        </details>
                      ),
                    )}
                  </div>
                ),
              )}
            </details>
          )}
          {context.memory.reads.length > 0 && (
            <details>
              <summary>实际回读 · {context.memory.reads.length} 页</summary>
              {context.memory.reads.map(
                /* 保留真正返回的页与指纹。 */ (read, index) => (
                  <div key={index}>
                    <code>
                      {read.id} · v{read.revision} · {read.sha256}
                    </code>
                    <pre>{read.content}</pre>
                  </div>
                ),
              )}
            </details>
          )}
          {context.memory.skipped.length > 0 && (
            <details>
              <summary>跳过的记忆</summary>
              {context.memory.skipped.map(
                /* 仅说明宿主确定的原因。 */ (item) => (
                  <p key={item.id}>
                    {item.title}：{item.reason}
                  </p>
                ),
              )}
            </details>
          )}
        </section>
      )}
      <section className="context-instruction" aria-labelledby={`${headingId}-instruction`}>
        <div className="section-heading">
          <h3 id={`${headingId}-instruction`}>项目指令快照</h3>
          <Tag color={instruction.status === 'error' ? 'error' : undefined}>
            {instructionStatusLabels[instruction.status]}
          </Tag>
        </div>
        <dl className="compact-dl">
          <div>
            <dt>来源</dt>
            <dd>
              本次实际工作区根目录 / <code>{instruction.path}</code>
            </dd>
          </div>
          <div>
            <dt>文件大小</dt>
            <dd>
              {instruction.size_bytes === null
                ? '未记录'
                : `${instruction.size_bytes.toLocaleString('zh-CN')} 字节（${formatContextBytes(instruction.size_bytes)}）`}
            </dd>
          </div>
          {instruction.sha256 && (
            <div>
              <dt>SHA-256</dt>
              <dd>
                <code>{instruction.sha256}</code>
              </dd>
            </div>
          )}
        </dl>
        {instruction.status === 'missing' && (
          <p className="muted">本次工作区没有根目录 AGENTS.md，继续使用宿主规则与任务描述。</p>
        )}
        {instruction.status === 'disabled' && (
          <p className="muted">本次配置关闭了项目指令自动加载。</p>
        )}
        {instruction.error_code && (
          <Alert
            type="error"
            showIcon
            title={instructionErrorLabel(instruction.error_code)}
            description={<code>{instruction.error_code}</code>}
          />
        )}
        {instruction.status === 'loaded' && instruction.content !== null && (
          <>
            <p className="muted">以下为建立本次运行时固定的完整快照，后续修改文件不会改变它。</p>
            <pre className="context-instruction-text" aria-label="项目指令内容">
              {instruction.content}
            </pre>
          </>
        )}
      </section>
    </section>
  );
}
