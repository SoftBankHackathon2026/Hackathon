import { useState } from "react";
import type { EnvComponent, FleetEnvironment } from "../../api/migrations";
import type { Control, Severity } from "../../api/security";
import { StatusDot } from "../../components/StatusDot";
import { SEVERITY_LABELS, SEVERITY_ORDER } from "../security/posture";
import { controlHash } from "../../app/routes";
import { SeverityDot } from "../../components/Severity";
import { SEVERITY_LABELS as SEV_LABELS } from "../security/posture";
import { excerpt } from "../security/explorer";
import { placementPath, statusCounts } from "./canvasModel";
import {
  OVERLAY_GROUPS,
  type Marker,
  type OverlayGroup,
} from "./securityModel";
import {
  COMPONENT_STATUS_LABELS,
  LAYER_LABELS,
  componentTone,
  dependents,
  kindLabel,
  detailText,
} from "./model";
import { blockPaint } from "./blockStyle";
import { cloudIconUrl } from "./cloudAssets";
import { cloudProviderOf, serviceIcon } from "./cloudIcons";
import { iconFor } from "./iconMap";
import { inkOn } from "./iso";
import type { Overlay, TileRef } from "./IsoCanvas";

function ServiceChip({
  component,
  env,
}: {
  component: EnvComponent;
  env: FleetEnvironment;
}) {
  const url = cloudIconUrl(serviceIcon(cloudProviderOf(env), component));
  const icon = iconFor(component);
  const paint = blockPaint(component, false);
  return (
    <span className="rc-chip">
      {url ? (
        <img src={url} width="16" height="16" alt="" />
      ) : (
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          aria-hidden="true"
          style={{ background: paint.top, borderRadius: 3, padding: 2 }}
          fill={icon.filled ? inkOn(paint.top) : "none"}
          stroke={icon.filled ? "none" : inkOn(paint.top)}
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d={icon.path} />
        </svg>
      )}
      {kindLabel(component.kind)}
    </span>
  );
}

export function ResourceCard({
  env,
  component,
  overlay,
  marker,
  onSelect,
  onClose,
}: {
  env: FleetEnvironment;
  component: EnvComponent;
  overlay: Overlay;
  marker: Marker | undefined;
  onSelect: (ref: TileRef) => void;
  onClose: () => void;
}) {
  const nameOf = (id: string) =>
    env.components.find((c) => c.id === id)?.name ?? id;
  const users = dependents(env.components, component.id);
  const path = placementPath(env, component);
  return (
    <aside className="resource-card" aria-label={`${component.name} 상세`}>
      <header>
        <div className="rc-chips">
          <ServiceChip component={component} env={env} />
        </div>
        <button
          type="button"
          className="card-close"
          aria-label="닫기"
          onClick={onClose}
        >
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
            <path
              d="M3 3l10 10M13 3L3 13"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </header>
      <h3 className="rc-name">{component.name}</h3>
      <dl className="rc-keys">
        <div>
          <dt>환경</dt>
          <dd>{env.name}</dd>
        </div>
        <div>
          <dt>종류</dt>
          <dd>
            {LAYER_LABELS[component.layer]}
            {component.managed ? " · 관리형 서비스" : ""}
          </dd>
        </div>
        <div>
          <dt>배치</dt>
          <dd>{path.slice(-2).join(" › ") || "-"}</dd>
        </div>
        <div>
          <dt>상태</dt>
          <dd>
            <StatusDot tone={componentTone(component.status)}>
              {COMPONENT_STATUS_LABELS[component.status]}
            </StatusDot>
          </dd>
        </div>
      </dl>
      <dl className="card-rows">
        <div>
          <dt>상세</dt>
          <dd>{detailText(component.detail) || "-"}</dd>
        </div>
        <div>
          <dt>의존</dt>
          <dd>
            {component.depends_on.length === 0
              ? "-"
              : component.depends_on.map((id) => (
                  <button
                    key={id}
                    type="button"
                    className="link-button"
                    onClick={() => onSelect({ envId: env.id, id })}
                  >
                    {nameOf(id)}
                  </button>
                ))}
          </dd>
        </div>
        <div>
          <dt>사용처</dt>
          <dd>
            {users.length === 0
              ? "-"
              : users.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className="link-button"
                    onClick={() => onSelect({ envId: env.id, id: c.id })}
                  >
                    {c.name}
                  </button>
                ))}
          </dd>
        </div>
      </dl>
      {overlay === "security" && (
        <section className="rc-security">
          <h4 className="rc-section">보안</h4>
          {marker ? (
            <ul className="rc-findings">
              {marker.controls.map((control: Control) => (
                <li key={control.id}>
                  <div className="rc-find-head">
                    <SeverityDot severity={control.severity}>
                      {SEV_LABELS[control.severity]}
                    </SeverityDot>
                    <span className="strong">{control.title}</span>
                  </div>
                  {control.standard_refs.length > 0 && (
                    <div className="chips">
                      {control.standard_refs.slice(0, 3).map((ref) => (
                        <span key={ref} className="chip">
                          {ref}
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="sub rc-why">{excerpt(control.why, 70)}</p>
                  <a
                    className="text-link rc-more"
                    href={controlHash(control.id)}
                  >
                    자세히 보기 →
                  </a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="rc-clean">실패한 통제 없음 ✓</p>
          )}
        </section>
      )}
    </aside>
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
    { tone: "warn", label: "일부만 준비됨", count: counts.degraded },
    { tone: "bad", label: "중단", count: counts.down },
    { tone: "idle", label: "꺼 둠", count: counts.idle },
    { tone: "idle", label: "상태 미수집", count: counts.unknown },
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
