import { Alert, Tag } from 'antd';
import { CompressionActions } from './CompressionActions';
import { formatContextBytes, instructionErrorLabel, instructionStatusLabels } from '../context';
import type { ContextView } from '../types';

/** 从持久化聚合快照展示项目规则与最新输入检查；null 或旧接口缺省都保持“未记录”。 */
export function ContextPanel({
  context,
  stopReason,
}: {
  context: ContextView | null | undefined; // 由 RunView 读取的完整证据，不从当前 SSE 窗口拼装。
  stopReason: string | null; // 真实运行结束原因；预算超限和指令错误分别解释。
}) {
  if (!context)
    return (
      <section className="context-panel" aria-labelledby="context-heading">
        <h2 id="context-heading">上下文</h2>
        <p className="muted">
          未记录上下文数据。旧任务或尚未准备上下文的任务无法展示加载与输入大小。
        </p>
      </section>
    );
  const { instruction, last_check: check } = context;
  const ratio = check ? (check.input_bytes / check.max_input_bytes) * 100 : null;
  return (
    <section className="context-panel" aria-labelledby="context-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">MODEL INPUT</span>
          <h2 id="context-heading">上下文</h2>
        </div>
        <span className="muted">UTF-8 JSON 字节数</span>
      </div>
      <p className="muted">
        统计消息和工具定义的整体输入大小，不等于模型 token 数。每轮发送前检查。
      </p>
      <section className="context-budget" aria-labelledby="input-budget-heading">
        <h3 id="input-budget-heading">最近一次输入检查</h3>
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
      <CompressionActions context={context} />
      <section className="context-instruction" aria-labelledby="instruction-heading">
        <div className="section-heading">
          <h3 id="instruction-heading">项目指令快照</h3>
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
