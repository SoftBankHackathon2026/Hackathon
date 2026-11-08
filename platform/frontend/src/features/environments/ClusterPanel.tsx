import type {
  ClusterInfo,
  ClusterNode,
  FleetEnvironment,
} from "../../api/migrations";
import { StatusLabel } from "../../components/PageKit";
import { ProviderLogo } from "../../components/ProviderLogo";
import { PanelGroup, SidePanel } from "../../components/SidePanel";
import { StatusDot } from "../../components/StatusDot";
import {
  clusterState,
  formatResource,
  isControlPlane,
  nodeState,
  otherPods,
  percent,
  podsByNode,
  resourceRows,
  roleLabel,
  shortNodeName,
  type PlacedPod,
  type ResourceRow,
} from "./clusterModel";
import { componentTone } from "./model";
import { podLabel, podTone } from "./podModel";

/** 자원 한 줄: 할당 가능량 막대 위에 요청(옅게)과 실사용(진하게). */
function ResourceMeter({ row }: { row: ResourceRow }) {
  const fmt = (v: number | null) =>
    v === null ? "모름" : formatResource(row.key, v);
  const main = row.usage ?? row.requests;
  const width = (share: number | null) =>
    share === null ? 0 : Math.min(100, share * 100);
  return (
    <div className={`cl-meter tone-${row.tone}`}>
      <div className="cl-meter-head">
        <span className="cl-meter-label">{row.label}</span>
        <span className="cl-meter-value">
          {row.usage === null && row.requests !== null && (
            <span className="cl-meter-kind">요청 </span>
          )}
          <b>{main === null ? "모름" : fmt(main)}</b>
          {row.allocatable !== null && <> / {fmt(row.allocatable)}</>}
          {(row.usageShare ?? row.requestShare) !== null && (
            <span className="cl-meter-pct">
              {percent(row.usageShare ?? row.requestShare)}
            </span>
          )}
        </span>
      </div>
      {row.allocatable !== null ? (
        <div className="cl-track" aria-hidden="true">
          <i
            className="cl-req"
            style={{ width: `${width(row.requestShare)}%` }}
          />
          {row.usageShare !== null && (
            <i
              className="cl-use"
              style={{ width: `${width(row.usageShare)}%` }}
            />
          )}
        </div>
      ) : null}
      <p className="cl-meter-sub">
        {[
          row.usage !== null ? `사용 ${fmt(row.usage)}` : "사용량 못 읽음",
          row.requests !== null ? `요청 ${fmt(row.requests)}` : null,
          row.allocatable !== null
            ? `할당 가능 ${fmt(row.allocatable)}`
            : "할당 가능량 못 읽음",
        ]
          .filter(Boolean)
          .join(", ")}
      </p>
    </div>
  );
}

function Resources({
  source,
  namespaces,
}: {
  source: Pick<ClusterNode, "allocatable" | "requests" | "usage">;
  namespaces?: string[];
}) {
  const rows = resourceRows(source);
  return (
    <>
      {rows.map((row) => (
        <ResourceMeter key={row.key} row={row} />
      ))}
      {source.requests !== null && namespaces && (
        <p className="fp-muted cl-note">
          요청량 집계 범위 {namespaces.join(", ")}
        </p>
      )}
      {source.usage === null && source.requests !== null && (
        <p className="fp-muted cl-note">
          실사용량을 읽지 못했어요. 막대는 요청량 기준이에요.
        </p>
      )}
    </>
  );
}

function MiniBar({ row }: { row: ResourceRow }) {
  const value = row.usageShare ?? row.requestShare;
  return (
    <span className={`cl-mini tone-${row.tone}`} title={row.label}>
      <i aria-hidden="true">
        <b style={{ width: `${Math.min(100, (value ?? 0) * 100)}%` }} />
      </i>
      {percent(value)}
    </span>
  );
}

function NodeTable({
  cluster,
  onSelectNode,
}: {
  cluster: ClusterInfo;
  onSelectNode: (name: string) => void;
}) {
  const nodes = [...cluster.nodes].sort(
    (a, b) =>
      Number(isControlPlane(b)) - Number(isControlPlane(a)) ||
      (a.zone ?? "").localeCompare(b.zone ?? "") ||
      a.name.localeCompare(b.name),
  );
  return (
    <table className="cl-nodes">
      <thead>
        <tr>
          <th>이름</th>
          <th>CPU</th>
          <th>메모리</th>
          <th className="num">파드</th>
        </tr>
      </thead>
      <tbody>
        {nodes.map((node) => {
          const [cpu, mem] = resourceRows(node);
          const state = nodeState(node);
          return (
            <tr key={node.name} onClick={() => onSelectNode(node.name)}>
              <td>
                <button
                  type="button"
                  className="cl-node-link"
                  title={node.name}
                  onClick={(event) => {
                    event.stopPropagation();
                    onSelectNode(node.name);
                  }}
                >
                  <StatusDot tone={state.tone}>
                    {shortNodeName(node.name)}
                  </StatusDot>
                </button>
                <span className="cl-node-sub">
                  {[roleLabel(node), node.zone, node.instance_type]
                    .filter(Boolean)
                    .join(", ")}
                </span>
              </td>
              <td>
                <MiniBar row={cpu} />
              </td>
              <td>
                <MiniBar row={mem} />
              </td>
              <td className="num">{node.pods ?? "모름"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 노드의 파드를 구성 요소별로 묶는다. */
function groupByComponent(placed: PlacedPod[]) {
  const groups = new Map<string, PlacedPod[]>();
  for (const entry of placed)
    groups.set(entry.component.id, [
      ...(groups.get(entry.component.id) ?? []),
      entry,
    ]);
  // 문제 있는 파드가 있는 구성 요소를 먼저.
  const rank = (group: PlacedPod[]) =>
    Math.min(
      ...group.map(({ pod }) => ["bad", "warn"].indexOf(podTone(pod)) >>> 0),
    );
  return [...groups.values()].sort((a, b) => rank(a) - rank(b));
}

function NodePods({
  placed,
  others,
  onSelectComponent,
}: {
  placed: PlacedPod[];
  others: number;
  onSelectComponent: (id: string) => void;
}) {
  if (placed.length === 0 && others === 0)
    return <p className="fp-muted">이 노드에서 도는 파드가 없어요.</p>;
  return (
    <>
      <ul className="cl-pod-groups">
        {groupByComponent(placed).map((group) => {
          const component = group[0].component;
          return (
            <li key={component.id}>
              <button
                type="button"
                className="cl-comp"
                onClick={() => onSelectComponent(component.id)}
              >
                <StatusDot tone={componentTone(component.status)}>
                  {component.name}
                </StatusDot>
                <span className="cl-count">{group.length}</span>
              </button>
              <ul className="cl-pod-list">
                {group.map(({ pod }) => (
                  <li key={pod.name} title={pod.name}>
                    <i className={`cl-pod-dot tone-${podTone(pod)}`} />
                    <span className="cl-pod-name">{pod.name}</span>
                    <span className="cl-pod-state">
                      {podLabel(pod)}
                      {pod.restarts > 0 ? `, 재시작 ${pod.restarts}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
      {others > 0 && (
        <p className="fp-muted cl-note">
          구성 요소에 속하지 않은 파드 {others}개(kube-system 등)
        </p>
      )}
    </>
  );
}

/**
 * 클러스터/노드 상세(오른쪽 패널). node 가 null 이면 클러스터 전체.
 * 자원 → 노드(또는 파드) → 정보. 노드 보기 다이어그램이 쓴다.
 */
export function ClusterPanel({
  env,
  node: nodeName,
  onSelectNode,
  onSelectComponent,
  onClose,
}: {
  env: FleetEnvironment;
  node: string | null;
  onSelectNode: (name: string | null) => void;
  onSelectComponent: (id: string) => void;
  onClose: () => void;
}) {
  const cluster = env.cluster ?? null;
  const envFact = {
    label: "환경",
    value: (
      <span className="cp-env">
        <ProviderLogo provider={env.provider} />
        {env.name}
      </span>
    ),
  };
  const node = cluster?.nodes.find((n) => n.name === nodeName) ?? null;

  if (nodeName !== null && cluster && node) {
    const nodes = cluster.nodes;
    const index = nodes.indexOf(node);
    const state = nodeState(node);
    const placed = podsByNode(env).placed.get(node.name) ?? [];
    return (
      <SidePanel
        kind="노드"
        id={roleLabel(node)}
        position={{ index, total: nodes.length }}
        onMove={(step) =>
          onSelectNode(nodes[(index + step + nodes.length) % nodes.length].name)
        }
        onClose={onClose}
        title={
          <>
            <span className="cl-title" title={node.name}>
              {shortNodeName(node.name, 32)}
            </span>
            <StatusLabel tone={state.tone}>{state.label}</StatusLabel>
          </>
        }
        facts={[
          envFact,
          { label: "영역", value: node.zone ?? "정보 없음" },
          { label: "유형", value: node.instance_type ?? "정보 없음" },
        ]}
        actions={
          <button
            type="button"
            className="pk-button weak"
            onClick={() => onSelectNode(null)}
          >
            클러스터 전체 보기
          </button>
        }
      >
        <PanelGroup label="자원">
          <Resources source={node} namespaces={env.observed_namespaces} />
        </PanelGroup>
        <PanelGroup label="파드">
          <p className="cl-lead">
            {node.pods === null ? "파드 수 모름" : `파드 ${node.pods}개`}
            {placed.length > 0 && `, 구성 요소 파드 ${placed.length}개`}
          </p>
          <NodePods
            placed={placed}
            others={otherPods(node, placed.length)}
            onSelectComponent={onSelectComponent}
          />
        </PanelGroup>
        <PanelGroup label="정보">
          <dl className="cl-facts">
            <dt>이름</dt>
            <dd className="cl-mono">{node.name}</dd>
            <dt>역할</dt>
            <dd>{roleLabel(node)}</dd>
            <dt>새 파드 배치</dt>
            <dd>
              {node.unschedulable === null
                ? "모름"
                : node.unschedulable
                  ? "중지됨(cordon)"
                  : "받음"}
            </dd>
            <dt>kubelet</dt>
            <dd>{node.kubelet_version ?? "모름"}</dd>
            <dt>OS</dt>
            <dd>
              {[node.os_image, node.arch].filter(Boolean).join(", ") || "모름"}
            </dd>
          </dl>
        </PanelGroup>
      </SidePanel>
    );
  }

  const state = clusterState(cluster);
  const { unplaced } = podsByNode(env);
  return (
    <SidePanel
      kind="클러스터"
      id={env.kind.toUpperCase()}
      onClose={onClose}
      title={
        <>
          {env.name}
          <StatusLabel tone={state.tone}>{state.label}</StatusLabel>
        </>
      }
      facts={[
        envFact,
        { label: "버전", value: cluster?.version ?? "모름" },
        {
          label: "노드 유형",
          value:
            [
              ...new Set(
                (cluster?.nodes ?? []).flatMap((n) =>
                  n.instance_type ? [n.instance_type] : [],
                ),
              ),
            ].join(", ") || "모름",
        },
      ]}
    >
      {!cluster ? (
        <PanelGroup label="상태">
          <p className="fp-muted">
            {env.connected
              ? "클러스터 정보를 아직 받지 못했어요."
              : "클러스터에 연결되지 않았어요."}
          </p>
        </PanelGroup>
      ) : (
        <>
          <PanelGroup label="자원">
            <Resources source={cluster} namespaces={env.observed_namespaces} />
          </PanelGroup>
          <PanelGroup label="노드" wide>
            {cluster.nodes.length === 0 ? (
              <p className="fp-muted">노드 목록을 읽지 못했어요.</p>
            ) : (
              <NodeTable
                cluster={cluster}
                onSelectNode={(name) => onSelectNode(name)}
              />
            )}
          </PanelGroup>
          {unplaced.length > 0 && (
            <PanelGroup label="배치 대기">
              <ul className="cl-pod-list">
                {unplaced.map(({ pod, component }) => (
                  <li key={pod.name}>
                    <i className={`cl-pod-dot tone-${podTone(pod)}`} />
                    <button
                      type="button"
                      className="cl-pod-name text-link"
                      onClick={() => onSelectComponent(component.id)}
                    >
                      {pod.name}
                    </button>
                    <span className="cl-pod-state">{podLabel(pod)}</span>
                  </li>
                ))}
              </ul>
            </PanelGroup>
          )}
        </>
      )}
    </SidePanel>
  );
}
