import { phaseLabel } from '../format';
import { lifecycleOutcomes, lifecycleStages, mergeLifecycle } from '../lifecycle';
import type { LifecycleStageTrace } from '../lifecycle';

/** 合并所选运行的 snapshot 与 live 记录，展示各阶段最近一次实际执行的组件。 */
export function LifecycleDiagnostics({
  snapshot = [],
  live = [],
}: {
  snapshot?: LifecycleStageTrace[]; // 接口提供的完整历史快照，空时等待事件。
  live?: LifecycleStageTrace[]; // 同一运行的实时阶段结果。
}) {
  const stages = mergeLifecycle(snapshot, live);
  if (!stages.length) return null;
  return (
    <details className="diagnostic-section">
      <summary>生命周期组件</summary>
      <dl className="compact-dl">
        {stages.map(
          /* 每阶段仅显示最近一条，完整执行顺序可在轨迹中查看。 */ (trace) => (
            <div key={trace.stage}>
              <dt>
                {lifecycleStages[trace.stage]} · #{trace.seq}
              </dt>
              <dd>
                <ul>
                  {trace.components.map(
                    /* 顺序由宿主决定，显示实际运行的来源。 */ (component) => (
                      <li key={component.name}>
                        <code>{component.name}</code> · {lifecycleOutcomes[component.outcome]}
                        {component.reason ? ` · ${phaseLabel(component.reason)}` : null}
                        <br />
                        <small className="muted">{component.source}</small>
                      </li>
                    ),
                  )}
                </ul>
              </dd>
            </div>
          ),
        )}
      </dl>
    </details>
  );
}
