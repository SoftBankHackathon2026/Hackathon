import { useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import { KeyValue } from "../../components/KeyValue";
import { StatusDot } from "../../components/StatusDot";
import { useElementWidth } from "../../components/charts/useElementWidth";
import { NODE_H, NODE_W, layoutArchitecture } from "./architecture";
import {
  COMPONENT_STATUS_LABELS,
  LAYER_LABELS,
  componentTone,
  dependents,
  kindLabel,
  nodeDetail,
} from "./model";

export function ArchitectureDiagram({
  env,
  otherName,
}: {
  env: FleetEnvironment;
  otherName: string | null;
}) {
  const { ref, width } = useElementWidth(700);
  const [selected, setSelected] = useState<string | null>(null);
  const layout = layoutArchitecture(env.components, width);
  const at = new Map(layout.nodes.map((n) => [n.id, n]));
  const current = env.components.find((c) => c.id === selected) ?? null;
  const linked = new Set(
    current
      ? layout.edges
          .filter((e) => e.from === current.id || e.to === current.id)
          .map((e) => `${e.from}>${e.to}`)
      : [],
  );
  const nameOf = (id: string) =>
    env.components.find((c) => c.id === id)?.name ?? id;
  if (env.components.length === 0)
    return <p className="empty-line">구성 요소 정보가 없어요.</p>;
  return (
    <>
      <div className="arch-scroll" ref={ref}>
        <div
          className="arch"
          style={{ width: layout.width, height: layout.height }}
        >
          <svg
            className="arch-edges"
            width={layout.width}
            height={layout.height}
            aria-hidden="true"
          >
            <defs>
              <marker
                id={`arrow-${env.id}`}
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="8"
                markerHeight="8"
                orient="auto"
              >
                <path d="M0 0 L8 4 L0 8 z" className="arch-arrow" />
              </marker>
            </defs>
            {layout.edges.map((edge) => (
              <path
                key={`${edge.from}>${edge.to}`}
                d={edge.path}
                className={
                  linked.has(`${edge.from}>${edge.to}`)
                    ? "arch-edge active"
                    : "arch-edge"
                }
                markerEnd={`url(#arrow-${env.id})`}
              />
            ))}
            {layout.stubs.map((stub) => (
              <path
                key={stub.id}
                d={stub.path}
                className="arch-edge stub"
                markerEnd={`url(#arrow-${env.id})`}
              />
            ))}
          </svg>
          {layout.captions.map((caption) => (
            <span
              key={`${caption.text}-${caption.x}-${caption.y}`}
              className="arch-caption"
              style={{ left: caption.x, top: caption.y }}
            >
              {caption.text}
              {caption.text === LAYER_LABELS.link && otherName
                ? ` → ${otherName}`
                : ""}
            </span>
          ))}
          {env.components.map((component) => {
            const node = at.get(component.id);
            if (!node) return null;
            const tone = componentTone(component.status);
            return (
              <button
                key={component.id}
                type="button"
                className={`arch-node tone-${tone}${selected === component.id ? " selected" : ""}`}
                style={{
                  left: node.x,
                  top: node.y,
                  width: NODE_W,
                  height: NODE_H,
                }}
                aria-expanded={selected === component.id}
                title={component.detail}
                onClick={() =>
                  setSelected((prev) =>
                    prev === component.id ? null : component.id,
                  )
                }
              >
                <span className="arch-name">{component.name}</span>
                <span className="arch-kind">
                  {kindLabel(component.kind)}
                  {component.managed ? " · 관리형" : ""}
                </span>
                {component.status === "unknown" ? (
                  <span className="arch-state sub">
                    {nodeDetail(component)}
                  </span>
                ) : (
                  <span className="arch-state">
                    <i className={`dot dot-${tone}`} aria-hidden="true" />
                    <span className="arch-state-text">
                      {nodeDetail(component)}
                    </span>
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
      {current && (
        <div className="arch-detail">
          <KeyValue
            rows={[
              {
                label: "구성 요소",
                value: (
                  <>
                    {current.name} <span className="sub">{current.id}</span>
                  </>
                ),
              },
              { label: "계층", value: LAYER_LABELS[current.layer] },
              { label: "종류", value: kindLabel(current.kind) },
              { label: "관리형", value: current.managed ? "예" : "아니오" },
              {
                label: "상태",
                value: (
                  <StatusDot tone={componentTone(current.status)}>
                    {COMPONENT_STATUS_LABELS[current.status]}
                  </StatusDot>
                ),
              },
              { label: "상세", value: current.detail || "-" },
              {
                label: "의존",
                value:
                  current.depends_on.length === 0
                    ? "-"
                    : current.depends_on.map(nameOf).join(", "),
              },
              {
                label: "사용처",
                value:
                  dependents(env.components, current.id)
                    .map((c) => c.name)
                    .join(", ") || "-",
              },
            ]}
          />
        </div>
      )}
    </>
  );
}
