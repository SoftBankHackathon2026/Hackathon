import type { MigrationDetail } from "../../api/migrations";
import { StatusDot } from "../../components/StatusDot";
import { formatCount, formatRate, formatSeconds } from "../../lib/format";
import { cdcNodes } from "./cdc";
import { stateLabel } from "./stages";

export function CdcPath({
  detail,
  names,
  counts,
}: {
  detail: MigrationDetail;
  names: { source: string; target: string };
  counts: { source: number; target: number };
}) {
  const nodes = cdcNodes(detail, names, counts);
  const { worker } = detail;
  return (
    <>
      <ol className="cdc-path">
        {nodes.map((node, i) => (
          <li key={node.key} className="cdc-item">
            {i > 0 && (
              <svg className="cdc-arrow" viewBox="0 0 24 12" aria-hidden="true">
                <path d="M0 6h20M15 1.5L20 6l-5 4.5" />
              </svg>
            )}
            <div className={`cdc-node tone-${node.tone}`}>
              <div className="cdc-head">
                <span>{node.kind}</span>
                {node.flag && <span className="cdc-flag">{node.flag}</span>}
              </div>
              <div className="cdc-body">
                <div className="cdc-name" title={node.name}>
                  {node.name}
                </div>
                <div className="sub">{node.detail}</div>
                {node.metric && <div className="cdc-metric">{node.metric}</div>}
              </div>
            </div>
          </li>
        ))}
      </ol>
      {worker && (
        <dl className="kv-inline" aria-label="쓰기 워커">
          <div>
            <dt>쓰기 워커</dt>
            <dd>
              <StatusDot
                tone={worker.state.toUpperCase() === "RUNNING" ? "ok" : "warn"}
              >
                {stateLabel(worker.state)}
              </StatusDot>
            </dd>
          </div>
          <div>
            <dt>기록 대상</dt>
            <dd>{worker.target} DB</dd>
          </div>
          <div>
            <dt>대기</dt>
            <dd>{formatCount(worker.queue_length)}건</dd>
          </div>
          <div>
            <dt>처리 속도</dt>
            <dd>{formatRate(worker.rate_per_second)}</dd>
          </div>
          <div>
            <dt>응답 p95</dt>
            <dd>{formatSeconds(worker.latency.p95)}</dd>
          </div>
        </dl>
      )}
    </>
  );
}
