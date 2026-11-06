import { useEffect, useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import { ProviderLogo } from "../../components/ProviderLogo";
import { NodeIcon } from "../security/ImpactPath";
import {
  COL_GAP,
  NODE_H,
  NODE_W,
  buildTree,
  countComponents,
  ancestorIds,
  layoutTree,
  quietNamespaces,
  worstStatus,
  type GroupKind,
  type TreeNode,
} from "./treeModel";
import { COMPONENT_STATUS_LABELS, componentTone, kindLabel } from "./model";
import { podSummary } from "./podModel";

const KIND_LABELS: Record<GroupKind, string> = {
  env: "환경",
  site: "사이트",
  vpc: "VPC",
  subnet: "서브넷",
  cluster: "클러스터",
  namespace: "네임스페이스",
  regional: "리전 서비스",
};

const KIND_GLYPHS: Record<Exclude<GroupKind, "env">, string> = {
  site: "M3 20V9l9-5 9 5v11M9 20v-6h6v6",
  vpc: "M7 18a5 5 0 1 1 .8-9.9 7 7 0 0 1 13.1 3A3.5 3.5 0 0 1 19 18z",
  subnet: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  cluster:
    "M12 2.5l8.5 4.9v9.2L12 21.5l-8.5-4.9V7.4zM12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z",
  namespace: "M4 4h16v16H4z",
  regional:
    "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.8 2.6 2.8 15.4 0 18M12 3c-2.8 2.6-2.8 15.4 0 18",
};

function shortGroupLabel(label: string) {
  return label.replace(/^ns /, "");
}

/**
 * 계층 모드(Argo CD 리소스 트리 방식). 환경마다 가로 트리: 환경 → VPC·서브넷·클러스터 → 네임스페이스 → 구성 요소.
 * 그룹 노드를 누르면 접고 펴고, 구성 요소를 누르면 상세 카드가 열린다.
 */
export function ResourceTree({
  environments,
  selected,
  onSelect,
}: {
  environments: FleetEnvironment[];
  selected: { envId: string; id: string } | null;
  onSelect: (ref: { envId: string; id: string } | null) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(
    () =>
      new Set(environments.flatMap((env) => quietNamespaces(buildTree(env)))),
  );
  // 고른 구성 요소가 접힌 그룹 안에 있으면 그 줄을 펼친다
  useEffect(() => {
    if (!selected) return;
    const env = environments.find((e) => e.id === selected.envId);
    if (!env) return;
    const trail = ancestorIds(buildTree(env), `${env.id}:${selected.id}`);
    setCollapsed((prev) =>
      trail.some((id) => prev.has(id))
        ? new Set([...prev].filter((id) => !trail.includes(id)))
        : prev,
    );
  }, [selected, environments]);
  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <div className="rtree-list" data-panel-owner>
      {environments.map((env) => {
        const root = buildTree(env);
        const { placed, edges, width, height } = layoutTree(root, collapsed);
        const pos = new Map(placed.map((p) => [p.node.id, p]));
        return (
          <section
            key={env.id}
            className="rtree"
            aria-label={`${env.name} 리소스 트리`}
          >
            <div className="rtree-scroll">
              <div className="rtree-canvas" style={{ width, height }}>
                <svg
                  className="rtree-edges"
                  width={width}
                  height={height}
                  aria-hidden="true"
                >
                  {edges.map(({ from, to }) => {
                    const a = pos.get(from);
                    const b = pos.get(to);
                    if (!a || !b) return null;
                    const x1 = a.x + NODE_W;
                    const y1 = a.y + NODE_H / 2;
                    const x2 = b.x;
                    const y2 = b.y + NODE_H / 2;
                    const xm = x1 + COL_GAP / 2;
                    const r = Math.min(8, Math.abs(y2 - y1) / 2);
                    const dir = y2 > y1 ? 1 : -1;
                    const d =
                      Math.abs(y2 - y1) < 1
                        ? `M${x1} ${y1}H${x2}`
                        : `M${x1} ${y1}H${xm - r}Q${xm} ${y1} ${xm} ${y1 + r * dir}V${y2 - r * dir}Q${xm} ${y2} ${xm + r} ${y2}H${x2}`;
                    return <path key={`${from}>${to}`} d={d} />;
                  })}
                </svg>
                {placed.map(({ node, x, y, collapsed: folded }) => (
                  <TreeCard
                    key={node.id}
                    node={node}
                    env={env}
                    style={{ left: x, top: y, width: NODE_W, height: NODE_H }}
                    folded={folded}
                    active={
                      node.type === "component" &&
                      selected?.envId === env.id &&
                      selected.id === node.component.id
                    }
                    onClick={() => {
                      if (node.type === "group") {
                        if (node.kind !== "env") toggle(node.id);
                      } else
                        onSelect(
                          selected?.envId === env.id &&
                            selected.id === node.component.id
                            ? null
                            : { envId: env.id, id: node.component.id },
                        );
                    }}
                  />
                ))}
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}

function TreeCard({
  node,
  env,
  style,
  folded,
  active,
  onClick,
}: {
  node: TreeNode;
  env: FleetEnvironment;
  style: React.CSSProperties;
  folded: boolean;
  active: boolean;
  onClick: () => void;
}) {
  const status = worstStatus(node);
  const tone = componentTone(status);
  if (node.type === "component") {
    const c = node.component;
    return (
      <button
        type="button"
        className={`rtree-node comp${active ? " active" : ""}`}
        style={style}
        onClick={onClick}
        title={c.detail || c.name}
      >
        <NodeIcon layer={c.layer} kind={c.kind} />
        <span className="rule-text">
          <span className="rule-title">{c.name}</span>
          <span className="rule-sub">
            {kindLabel(c.kind)}
            {c.managed ? ", 관리형" : ""}
            {(() => {
              const pods = podSummary(c);
              return pods ? `, 파드 ${pods.ready}/${pods.total}` : "";
            })()}
          </span>
        </span>
        <span
          className={`rtree-status tone-${tone}`}
          title={COMPONENT_STATUS_LABELS[status]}
          aria-label={COMPONENT_STATUS_LABELS[status]}
        >
          <i aria-hidden="true" />
          {(tone === "bad" || tone === "warn") &&
            COMPONENT_STATUS_LABELS[status]}
        </span>
      </button>
    );
  }
  const count = countComponents(node);
  const isEnv = node.kind === "env";
  return (
    <button
      type="button"
      className={`rtree-node group kind-${node.kind}${folded ? " folded" : ""}`}
      style={style}
      onClick={onClick}
      aria-expanded={isEnv ? undefined : !folded}
      disabled={isEnv}
    >
      {isEnv ? (
        <ProviderLogo provider={env.provider} />
      ) : (
        <svg className="rtree-glyph" viewBox="0 0 24 24" aria-hidden="true">
          <path d={KIND_GLYPHS[node.kind as Exclude<GroupKind, "env">]} />
        </svg>
      )}
      <span className="rule-text">
        <span className="rule-title">
          {isEnv ? node.label : shortGroupLabel(node.label)}
        </span>
        <span className="rule-sub">
          {KIND_LABELS[node.kind]}, {count}개
          {isEnv && env.boundary && env.boundary.length > 0
            ? `, ${env.boundary.join(" / ")}`
            : ""}
        </span>
      </span>
      <span
        className={`rtree-dot tone-${tone}`}
        title={`가장 나쁜 상태: ${COMPONENT_STATUS_LABELS[status]}`}
      />
      {!isEnv && (
        <span className="rtree-fold" aria-hidden="true">
          {folded ? `+${count}` : "−"}
        </span>
      )}
    </button>
  );
}
