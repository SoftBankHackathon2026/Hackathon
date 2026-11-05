import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { FleetEnvironment, MigrationSummary } from "../../api/migrations";
import type { Severity } from "../../api/security";
import { Segmented } from "../../components/Segmented";
import { envShortName } from "../migrations/fleetView";
import { migrationQueryOptions } from "../migrations/queries";
import { STATUS_LABELS, stageTitle } from "../migrations/stages";
import { postureQueryOptions } from "../security/queries";
import { ArchitectureDiagram } from "./ArchitectureDiagram";
import {
  IsoCanvas,
  type Overlay,
  type PathView,
  type TileRef,
} from "./IsoCanvas";
import type { ViewMode } from "./iso";
import {
  InfraLegend,
  PathLegend,
  ResourceCard,
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

type Grouping = "boundary" | "layer";

const TABS: { id: Overlay; label: string }[] = [
  { id: "infra", label: "인프라" },
  { id: "path", label: "이전 경로" },
  { id: "security", label: "보안" },
];

export function ArchitectureCanvas({
  environments,
  migrations,
  initialSelect,
  height = 860,
}: {
  environments: FleetEnvironment[];
  migrations: MigrationSummary[];
  initialSelect?: TileRef;
  height?: number;
}) {
  const [overlay, setOverlay] = useState<Overlay>("infra");
  const [grouping, setGrouping] = useState<Grouping>("boundary");
  const [mode, setMode] = useState<ViewMode>("iso");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<TileRef | null>(
    initialSelect ?? null,
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
    if (initialSelect)
      document
        .querySelector(".canvas-block")
        ?.scrollIntoView({ block: "start" });
  }, [initialSelect]);

  useEffect(() => {
    if (!selected) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selected]);

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
  const selectedMarker = (() => {
    if (!selectedEnv || !selectedComponent) return undefined;
    return buildMarkers(selectedEnv, controls, { group: null, hidden: [] }).get(
      `tile:${selectedEnv.id}:${selectedComponent.id}`,
    );
  })();

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
                      {m.job_id} · {STATUS_LABELS[m.status]}
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
                { value: "layer", label: "계층" },
              ]}
              onChange={setGrouping}
            />
          </span>
          {grouping === "boundary" && (
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
      {grouping === "layer" ? (
        <>
          {overlay !== "infra" && (
            <p className="info-line">
              이전 경로와 보안은 인프라 경계 기준에서만 볼 수 있어요.
            </p>
          )}
          <div className="layer-grid">
            {environments.map((env) => {
              const other = environments.find((e) => e.id !== env.id);
              return (
                <section key={env.id} className="layer-env">
                  <h3 className="graph-head">{env.name}</h3>
                  <ArchitectureDiagram
                    env={env}
                    otherName={
                      other ? envShortName(environments, other.id) : null
                    }
                  />
                </section>
              );
            })}
          </div>
        </>
      ) : (
        <div className="canvas-frame">
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
          {selectedEnv && selectedComponent && (
            <ResourceCard
              env={selectedEnv}
              component={selectedComponent}
              overlay={overlay}
              marker={selectedMarker}
              onSelect={setSelected}
              onClose={() => setSelected(null)}
            />
          )}
        </div>
      )}
    </div>
  );
}
