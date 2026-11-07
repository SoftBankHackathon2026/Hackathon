import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  CLOSE_PANELS_EVENT,
  nodesHash,
  replaceHash,
  resourceHash,
} from "../../app/routes";
import type { FleetEnvironment, MigrationSummary } from "../../api/migrations";
import type { Severity } from "../../api/security";
import { Segmented } from "../../components/Segmented";
import { migrationQueryOptions } from "../migrations/queries";
import { STATUS_LABELS, stageTitle } from "../migrations/stages";
import { postureQueryOptions } from "../security/queries";
import { ClusterPanel } from "./ClusterPanel";
import { ComponentPanel } from "./ComponentPanel";
import { NodeCanvas, type NodeRef } from "./NodeCanvas";
import { ResourceTree } from "./ResourceTree";
import {
  IsoCanvas,
  type Overlay,
  type PathView,
  type TileRef,
} from "./IsoCanvas";
import type { ViewMode } from "./iso";
import {
  BoundaryKey,
  InfraLegend,
  PathLegend,
  SecurityBar,
} from "./CanvasPanels";
import { layoutCanvas } from "./canvasLayout";
import { migrationPath } from "./canvasModel";
import {
  buildMarkers,
  groupCounts,
  severityCountsFor,
  type Marker,
  type OverlayGroup,
} from "./securityModel";

type Grouping = "boundary" | "node" | "layer";

const TABS: { id: Overlay; label: string }[] = [
  { id: "infra", label: "인프라" },
  { id: "path", label: "이전 경로" },
  { id: "security", label: "보안" },
];

export function ArchitectureCanvas({
  environments,
  migrations,
  select,
  nodes,
  height = 860,
}: {
  environments: FleetEnvironment[];
  migrations: MigrationSummary[];
  /** 주소의 구성 요소(#environments/<환경>/<구성 요소>) */
  select?: TileRef;
  /** 주소의 노드 보기(#environments/nodes/<환경>/<노드>) */
  nodes?: { envId?: string; node?: string };
  height?: number;
}) {
  const [overlay, setOverlay] = useState<Overlay>("infra");
  const [grouping, setGrouping] = useState<Grouping>(
    nodes ? "node" : "boundary",
  );
  const [mode, setMode] = useState<ViewMode>("iso");
  const [showPods, setShowPods] = useState(false);
  const [query, setQuery] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Severity[]>([]);
  const [secGroup, setSecGroup] = useState<OverlayGroup | null>(null);
  const posture = useQuery(postureQueryOptions()).data;
  const job =
    migrations.find((m) => m.job_id === jobId) ?? migrations[0] ?? null;
  const detail = useQuery({
    ...migrationQueryOptions(job?.job_id ?? ""),
    enabled: overlay === "path" && job !== null,
  }).data;

  // 주소로 들어왔을 때만 다이어그램으로 내려간다(같은 화면에서 고를 때는 스크롤하지 않는다).
  useEffect(() => {
    if (select || nodes)
      document
        .querySelector(".canvas-block")
        ?.scrollIntoView({ block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 열린 패널은 주소(#environments/<환경>/<구성 요소>, #environments/nodes/<환경>/<노드>)가 정한다.
  // 상태를 따로 들고 주소와 서로 맞추면 반영이 한 박자 어긋날 때 둘이 서로를 되돌리며 끝없이 바뀐다.
  // 그래서 패널은 주소에서 읽기만 하고, 고르거나 닫을 때는 주소만 바꾼다.
  const selected: TileRef | null = select ?? null;
  const node: NodeRef | null = nodes?.envId
    ? { envId: nodes.envId, node: nodes.node ?? null }
    : null;
  // 보기(그룹 기준)는 화면 상태. 노드 주소로 오면 노드 보기, 아무것도 고르지 않은 주소로 오면 노드 보기를 벗어난다.
  const nodeRoute = nodes !== undefined;
  const plainRoute = !select && !nodes;
  useEffect(() => {
    if (nodeRoute) setGrouping("node");
    else if (plainRoute) setGrouping((g) => (g === "node" ? "boundary" : g));
  }, [nodeRoute, plainRoute]);
  const baseHash = grouping === "node" ? nodesHash() : "#environments";
  const clearPanels = () => replaceHash(baseHash);
  useEffect(() => {
    const close = () => replaceHash(baseHash);
    window.addEventListener(CLOSE_PANELS_EVENT, close);
    return () => window.removeEventListener(CLOSE_PANELS_EVENT, close);
  }, [baseHash]);
  // 패널은 하나만: 구성 요소를 고르면 노드 패널이 닫히고, 노드를 고르면 구성 요소 패널이 닫힌다(주소가 하나라서).
  const selectComponent = (ref: TileRef | null) =>
    replaceHash(ref ? resourceHash(ref.envId, ref.id) : baseHash);
  const selectNode = (ref: NodeRef | null) =>
    replaceHash(ref ? nodesHash(ref.envId, ref.node ?? undefined) : baseHash);

  useEffect(() => {
    if (!selected && !node) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") clearPanels();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });
  const nodeEnv = node
    ? environments.find((e) => e.id === node.envId)
    : undefined;

  const layout = layoutCanvas(environments);
  const controls = posture?.controls ?? [];
  const markers = new Map<string, Marker>();
  if (overlay === "security")
    for (const env of environments)
      for (const [key, value] of buildMarkers(env, controls, {
        group: secGroup,
        hidden,
      }))
        markers.set(key, value);
  const path: PathView =
    overlay === "path" && job
      ? (() => {
          const result = migrationPath(
            job,
            environments,
            detail?.worker?.target ?? null,
          );
          return {
            cdc: result.cdc,
            worker: result.worker,
            write: result.write,
            writing: result.writing,
            chip: {
              jobId: job.job_id,
              stage: stageTitle(job.stage),
              status: STATUS_LABELS[job.status],
            },
          };
        })()
      : null;
  const selectedEnv = selected
    ? environments.find((e) => e.id === selected.envId)
    : undefined;
  const selectedComponent =
    selectedEnv?.components.find((c) => c.id === selected?.id) ?? null;
  return (
    <div className="canvas-block">
      <div className="canvas-toolbar">
        <div className="overlay-tabs" role="tablist" aria-label="오버레이">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={overlay === tab.id}
              onClick={() => setOverlay(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <div className="canvas-tools">
          {overlay === "path" &&
            grouping === "boundary" &&
            migrations.length > 0 && (
              <label className="tool-select">
                작업
                <select
                  value={job?.job_id ?? ""}
                  onChange={(event) => setJobId(event.target.value)}
                >
                  {migrations.map((m) => (
                    <option key={m.job_id} value={m.job_id}>
                      {m.job_id}, {STATUS_LABELS[m.status]}
                    </option>
                  ))}
                </select>
              </label>
            )}
          <span className="tool-group">
            <span className="sub">그룹 기준</span>
            <Segmented<Grouping>
              label="그룹 기준"
              value={grouping}
              options={[
                { value: "boundary", label: "인프라 경계" },
                { value: "node", label: "노드" },
                { value: "layer", label: "계층" },
              ]}
              onChange={(next) => {
                setGrouping(next);
                // 노드 패널은 노드 보기에서만 연다. 구성 요소 패널은 보기를 바꿔도 그대로 둔다.
                if (!selected)
                  replaceHash(next === "node" ? nodesHash() : "#environments");
              }}
            />
          </span>
          {grouping === "boundary" && (
            <button
              type="button"
              className={`tool-toggle${showPods ? " on" : ""}`}
              aria-pressed={showPods}
              onClick={() => setShowPods((v) => !v)}
            >
              <span className="tool-switch" aria-hidden="true" />
              파드 보기
            </button>
          )}
          {grouping !== "layer" && (
            <input
              type="search"
              aria-label="다이어그램에서 찾기"
              placeholder="다이어그램에서 찾기"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          )}
        </div>
      </div>
      {grouping === "node" ? (
        <div className="canvas-frame" data-panel-owner>
          {overlay !== "infra" && (
            <p className="tree-note canvas-note">
              이전 경로와 보안은 인프라 경계 보기에서 볼 수 있어요.
            </p>
          )}
          <NodeCanvas
            environments={environments}
            mode={mode}
            onModeChange={setMode}
            query={query}
            selectedComponent={selected}
            selectedNode={node}
            onSelectComponent={selectComponent}
            onSelectNode={selectNode}
            onClear={clearPanels}
            height={height}
          />
          {nodeEnv && node && (
            <ClusterPanel
              env={nodeEnv}
              node={node.node}
              onSelectNode={(name) =>
                selectNode({ envId: nodeEnv.id, node: name })
              }
              onSelectComponent={(id) =>
                selectComponent({ envId: nodeEnv.id, id })
              }
              onClose={clearPanels}
            />
          )}
          {selectedEnv && selectedComponent && (
            <ComponentPanel
              env={selectedEnv}
              component={selectedComponent}
              onClose={clearPanels}
              onSelect={(id) => selectComponent({ envId: selectedEnv.id, id })}
            />
          )}
        </div>
      ) : grouping === "layer" ? (
        <div className="canvas-frame tree-frame" data-panel-owner>
          {overlay !== "infra" && (
            <p className="tree-note">
              이전 경로와 보안은 인프라 경계 보기에서 볼 수 있어요.
            </p>
          )}
          <ResourceTree
            environments={environments}
            selected={selected}
            onSelect={selectComponent}
          />
          {selectedEnv && selectedComponent && (
            <ComponentPanel
              env={selectedEnv}
              component={selectedComponent}
              onClose={clearPanels}
              onSelect={(id) => selectComponent({ envId: selectedEnv.id, id })}
            />
          )}
        </div>
      ) : (
        <div className="canvas-frame" data-panel-owner>
          <IsoCanvas
            layout={layout}
            environments={environments}
            mode={mode}
            onModeChange={setMode}
            overlay={overlay}
            query={query}
            selected={selected}
            onSelect={selectComponent}
            markers={markers}
            path={path}
            height={height}
            showPods={showPods}
          />
          {overlay === "infra" && <InfraLegend environments={environments} />}
          {overlay === "path" && <PathLegend />}
          {overlay === "security" && (
            <SecurityBar
              groupCounts={groupCounts(environments, controls)}
              group={secGroup}
              onGroup={setSecGroup}
              severityCounts={severityCountsFor(
                environments,
                controls,
                secGroup,
              )}
              hidden={hidden}
              onToggle={(severity) =>
                setHidden((prev) =>
                  prev.includes(severity)
                    ? prev.filter((s) => s !== severity)
                    : [...prev, severity],
                )
              }
            />
          )}
          {overlay === "infra" && <BoundaryKey environments={environments} />}
          {selectedEnv && selectedComponent && (
            <ComponentPanel
              env={selectedEnv}
              component={selectedComponent}
              onClose={clearPanels}
              onSelect={(id) => selectComponent({ envId: selectedEnv.id, id })}
            />
          )}
        </div>
      )}
    </div>
  );
}
