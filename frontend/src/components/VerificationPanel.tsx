import { Alert, Tag } from 'antd';
import { duration } from '../format';
import type { VerificationResult } from '../types';

/** 展示独立验收事实；detail=null 时明确说明缺少检查，模型回答不会替代验收。 */
export function VerificationPanel({
  detail,
  active,
  project = false,
}: {
  detail: VerificationResult | null;
  active: boolean; // 运行或测试仍在进行。
  project?: boolean; // true 时使用项目测试文案，缺省沿用普通独立验收。
}) {
  return (
    <section className="verification-panel" aria-labelledby="verification-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">VERIFICATION</span>
          <h2 id="verification-heading">{project ? '项目测试' : '独立验收'}</h2>
        </div>
      </div>
      {!detail ? (
        <p className="muted">
          {project
            ? active
              ? '任务结束后运行宿主登记的项目测试。'
              : '本次没有项目测试结果，请检查结束原因与执行记录。'
            : active
              ? '尚未产生独立验收结果。'
              : '本次没有独立验收结果，执行完成不代表内容已验证。'}
        </p>
      ) : (
        <>
          <Tag color={detail.status === 'passed' ? 'success' : 'error'}>
            {detail.status === 'passed'
              ? project
                ? '测试通过'
                : '验收通过'
              : detail.status === 'failed'
                ? project
                  ? '测试未通过'
                  : '产物不符合要求'
                : project
                  ? '测试运行异常'
                  : '验收运行异常'}
          </Tag>
          <dl className="compact-dl">
            <div>
              <dt>原因</dt>
              <dd>
                <code>{detail.reason}</code>
              </dd>
            </div>
            <div>
              <dt>退出码</dt>
              <dd>{detail.exit_code ?? '未获得'}</dd>
            </div>
            <div>
              <dt>耗时</dt>
              <dd>{duration(detail.elapsed_ms)}</dd>
            </div>
            <div>
              <dt>清理</dt>
              <dd>{detail.cleanup_status}</dd>
            </div>
          </dl>
          {detail.truncated && (
            <Alert type="warning" title="下方输出预览已截断，可从产物下载原始输出。" />
          )}
          {detail.stdout && (
            <details open>
              <summary>标准输出</summary>
              <pre>{detail.stdout}</pre>
            </details>
          )}
          {detail.stderr && (
            <details>
              <summary>错误输出</summary>
              <pre>{detail.stderr}</pre>
            </details>
          )}
        </>
      )}
    </section>
  );
}
