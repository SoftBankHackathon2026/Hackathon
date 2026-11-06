import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
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
  initialSelect,
  initialNodes,
  height = 860,
}: {
  environments: FleetEnvironment[];
  migrations: MigrationSummary[];
  initialSelect?: TileRef;
  initialNodes?: { envId?: string; node?: string };
  height?: number;
}) {
  const [overlay, setOverlay] = useState<Overlay>("infra");
  const [grouping, setGrouping] = useState<Grouping>(
    initialNodes ? "node" : "boundary",
  );
  const [mode, setMode] = useState<ViewMode>("iso");
  const [showPods, setShowPods] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<TileRef | null>(
    initialSelect ?? null,
  );
  const [node, setNode] = useState<NodeRef | null>(
    initialNodes?.envId
      ? { envId: initialNodes.envId, node: initialNodes.node ?? null }
      : null,
  );
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

  useEffect(() => {
    if (initialSelect || initialNodes)
      document
        .querySelector(".canvas-block")
        ?.scrollIntoView({ block: "start" });
  }, [initialSelect, initialNodes]);

  useEffect(() => {
    if (!selected && !node) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSelected(null);
        setNode(null);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selected, node]);
  // 패널은 하나만: 구성 요소를 고르면 노드 패널을 닫고, 노드를 고르면 구성 요소 패널을 닫는다.
  const selectComponent = (ref: TileRef | null) => {
    setNode(null);
    setSelected(ref);
  };
  const selectNode = (ref: NodeRef | null) => {
    setSelected(null);
    setNode(ref);
  };
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
                setNode(null);
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
            onClear={() => {
              setSelected(null);
              setNode(null);
            }}
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
              onClose={() => setNode(null)}
            />
          )}
          {selectedEnv && selectedComponent && (
            <ComponentPanel
              env={selectedEnv}
              component={selectedComponent}
              onClose={() => setSelected(null)}
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
            onSelect={setSelected}
          />
          {selectedEnv && selectedComponent && (
            <ComponentPanel
              env={selectedEnv}
              component={selectedComponent}
              onClose={() => setSelected(null)}
              onSelect={(id) => setSelected({ envId: selectedEnv.id, id })}
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
            onSelect={setSelected}
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
              onClose={() => setSelected(null)}
              onSelect={(id) => setSelected({ envId: selectedEnv.id, id })}
            />
          )}
        </div>
      )}
    </div>
  );
}
