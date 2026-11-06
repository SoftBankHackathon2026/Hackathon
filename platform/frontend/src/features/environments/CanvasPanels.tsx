import { useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import type { Severity } from "../../api/security";
import { SEVERITY_LABELS, SEVERITY_ORDER } from "../security/posture";
import { statusCounts } from "./canvasModel";
import { OVERLAY_GROUPS, type OverlayGroup } from "./securityModel";
import { LEVEL_COLORS, LEVEL_LABELS, LEVEL_ORDER, levelOf } from "./blockStyle";

/** 경계 범례(Cloudcraft 그룹 기준 칩처럼 단계마다 색). 지금 그림에 있는 단계만. */
export function BoundaryKey({
  environments,
}: {
  environments: FleetEnvironment[];
}) {
  const present = new Set(
    environments.flatMap((env) => [
      ...(env.boundary ?? []).map(levelOf),
      ...env.components.flatMap((c) => (c.placement ?? []).map(levelOf)),
    ]),
  );
  const levels = LEVEL_ORDER.filter((level) => present.has(level));
  return (
    <ol className="boundary-key" aria-label="경계 단계">
      {levels.map((level) => (
        <li key={level}>
          <i
            style={{
              borderColor: LEVEL_COLORS[level],
              borderStyle:
                level === "region" || level === "namespace"
                  ? "dashed"
                  : "solid",
            }}
          />
          {LEVEL_LABELS[level]}
        </li>
      ))}
    </ol>
  );
}

export function InfraLegend({
  environments,
}: {
  environments: FleetEnvironment[];
}) {
  const counts = statusCounts(environments.flatMap((e) => e.components));
  const rows = [
    { tone: "ok", label: "정상", count: counts.healthy },
    { tone: "warn", label: "일부 파드만 준비됨", count: counts.degraded },
    { tone: "bad", label: "중단", count: counts.down },
    { tone: "idle", label: "복제본 0개", count: counts.idle },
    { tone: "idle", label: "상태 정보 없음", count: counts.unknown },
  ].filter((row) => row.tone === "ok" || row.count > 0);
  return (
    <div className="canvas-legend" role="group" aria-label="범례">
      {rows.map((row) => (
        <span key={row.label} className="legend-row">
          <i className={`legend-box lb-${row.tone}`} />
          {row.label}
          <b>{row.count}</b>
        </span>
      ))}
    </div>
  );
}

export function PathLegend() {
  return (
    <div className="canvas-legend" role="group" aria-label="범례">
      <span className="legend-row">
        <i className="legend-line" />
        CDC 데이터 흐름
      </span>
      <span className="legend-row">
        <i className="legend-line solid" />
        쓰기 경로
      </span>
      <span className="legend-row">
        <i className="legend-box lb-ok" />
        쓰는 중인 DB
      </span>
    </div>
  );
}

export function SecurityBar({
  groupCounts,
  group,
  onGroup,
  severityCounts,
  hidden,
  onToggle,
}: {
  groupCounts: Record<OverlayGroup, number>;
  group: OverlayGroup | null;
  onGroup: (group: OverlayGroup | null) => void;
  severityCounts: Record<Severity, number>;
  hidden: Severity[];
  onToggle: (severity: Severity) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="sec-bar" role="group" aria-label="보안 범례">
      {OVERLAY_GROUPS.map((item) => (
        <button
          key={item.id}
          type="button"
          className={`sec-tab${group === item.id ? " on" : ""}`}
          aria-pressed={group === item.id}
          onClick={() => onGroup(group === item.id ? null : item.id)}
        >
          {item.label}
          <b>{groupCounts[item.id]}</b>
        </button>
      ))}
      <span className="sec-filter">
        <button
          type="button"
          className="sec-tab"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          심각도
          <span className="sec-swatches" aria-hidden="true">
            {SEVERITY_ORDER.map((s) => (
              <i
                key={s}
                className={`legend-box lb-sev-${s}`}
                style={{ opacity: hidden.includes(s) ? 0.25 : 1 }}
              />
            ))}
          </span>
        </button>
        {open && (
          <div className="sec-popover">
            {SEVERITY_ORDER.map((severity) => (
              <label key={severity} className="legend-check">
                <input
                  type="checkbox"
                  checked={!hidden.includes(severity)}
                  onChange={() => onToggle(severity)}
                />
                <i className={`legend-box lb-sev-${severity}`} />
                {SEVERITY_LABELS[severity]}
                <b>{severityCounts[severity]}</b>
              </label>
            ))}
          </div>
        )}
      </span>
    </div>
  );
}
