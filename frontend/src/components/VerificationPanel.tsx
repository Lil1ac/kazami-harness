import { Alert, Tag } from 'antd';
import { artifactUrl } from '../api';
import { duration } from '../format';
import type { VerificationAttempt, VerificationResult } from '../types';

/** 将实际检查状态转换为公开测试或独立验收文案；不读取模型回答。 */
function resultLabel(detail: VerificationResult, project: boolean): string {
  if (detail.status === 'passed') return project ? '测试通过' : '验收通过';
  if (detail.status === 'failed') return project ? '测试未通过' : '产物不符合要求';
  return project ? '测试运行异常' : '验收运行异常';
}

/** 展示detail的真实进程输出；runId只用于该次已登记原始输出的受限下载。 */
function CheckEvidence({ detail, runId }: { detail: VerificationResult; runId: string }) {
  return (
    <>
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
        <Alert type="warning" title="下方输出预览已截断，可下载该次原始输出。" />
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
      {detail.artifacts.length > 0 && (
        <p className="muted">
          {detail.artifacts.map(
            /* 每次检查保留自己的命令编号，后续结果不会替换该链接。 */ (artifact) => (
              <a key={artifact.path} href={artifactUrl(runId, artifact)} download>
                {artifact.kind === 'stderr' ? '下载错误输出' : '下载标准输出'}{' '}
              </a>
            ),
          )}
        </p>
      )}
    </>
  );
}

/** 分开展示当前代码的检查和历史失败；修复期间没有当前检查，旧通过不能冒充新结果。 */
export function VerificationPanel({
  detail,
  attempts,
  repairAttempts,
  runId,
  active,
  project = false,
}: {
  detail: VerificationResult | null; // 当前代码的检查；未检查或修复中为空。
  attempts: VerificationAttempt[]; // 全部真实检查，按落盘顺序提供。
  repairAttempts: number; // 已开始的修复次数，0至2；不按事件条数猜测。
  runId: string; // 原始输出所属运行，跨用户轮次不变更旧链接。
  active: boolean; // 模型、工具、测试及收尾仍在执行。
  project?: boolean; // 公开项目测试使用测试文案，其他使用独立验收文案。
}) {
  const previous = detail ? attempts.slice(0, -1) : attempts; // 当前详情单独展示，其余留作历史。
  return (
    <section className="verification-panel" aria-labelledby="verification-heading">
      <div className="section-heading">
        <h2 id="verification-heading">{project ? '项目测试' : '独立验收'}</h2>
      </div>
      {repairAttempts > 0 && (
        <p role="status">
          {active && !detail ? '正在进行' : '已进行'}第 {repairAttempts} / 2 次自动修复
        </p>
      )}
      {!detail ? (
        <p className="muted">
          {repairAttempts > 0
            ? '修复后的代码尚未取得新的检查结果。'
            : project
              ? active
                ? '如已配置项目测试，任务结束后会运行检查。'
                : '本轮没有项目测试结果，保持未验收；可按需要配置测试或查看执行记录。'
              : active
                ? '尚未产生独立验收结果。'
                : '本次没有独立验收结果，执行完成不代表内容已验证。'}
        </p>
      ) : (
        <>
          <Tag color={detail.status === 'passed' ? 'success' : 'error'}>
            {resultLabel(detail, project)}
          </Tag>
          <CheckEvidence detail={detail} runId={runId} />
        </>
      )}
      {previous.length > 0 && (
        <section aria-label="历史检查">
          <h3>历史检查</h3>
          {previous.map(
            /* 以修复次数定位实际检查，末轮成功也保留先前失败的全部输出。 */ (attempt) => (
              <details key={attempt.repair_attempt}>
                <summary>
                  {attempt.repair_attempt === 0
                    ? '首次检查'
                    : `第 ${attempt.repair_attempt} 次修复后`}{' '}
                  · {resultLabel(attempt.result, project)}
                </summary>
                <CheckEvidence detail={attempt.result} runId={runId} />
              </details>
            ),
          )}
        </section>
      )}
    </section>
  );
}
