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
            {i > 0 && <span className="cdc-arrow" aria-hidden="true" />}
            <div className={`cdc-node tone-${node.tone}`}>
              <div className="cdc-head">
                {node.flag ? (
                  <span className="cdc-flag">{node.flag}</span>
                ) : (
                  <span>{node.kind}</span>
                )}
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
        <p className="info-line">
          쓰기 워커는{" "}
          <StatusDot
            tone={worker.state.toUpperCase() === "RUNNING" ? "ok" : "warn"}
          >
            {stateLabel(worker.state)}
          </StatusDot>{" "}
          상태이고, 지금은 {worker.target} DB에 기록해요. 대기 중인 쓰기{" "}
          {formatCount(worker.queue_length)}건, 처리 속도{" "}
          {formatRate(worker.rate_per_second)}, 응답 시간(p95){" "}
          {formatSeconds(worker.latency.p95)}.
        </p>
      )}
    </>
  );
}
