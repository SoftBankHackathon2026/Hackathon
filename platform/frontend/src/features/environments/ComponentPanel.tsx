import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type {
  ComponentEndpoint,
  EnvComponent,
  FleetEnvironment,
  MigrationStatus,
} from "../../api/migrations";
import { controlHash, migrationHash, resourceHash } from "../../app/routes";
import { StatusLabel } from "../../components/PageKit";
import { ProviderLogo } from "../../components/ProviderLogo";
import { PanelGroup, SidePanel } from "../../components/SidePanel";
import { PodLogPanel } from "./PodLogPanel";
import { StatusDot } from "../../components/StatusDot";
import { costsQueryOptions } from "../costs/queries";
import { usd } from "../costs/model";
import { fleetQueryOptions } from "../migrations/queries";
import { STATUS_LABELS, stageTitle, type Tone } from "../migrations/stages";
import { postureQueryOptions } from "../security/queries";
import { SeverityMeter } from "../security/SeverityMeter";
import { LEVEL_COLORS, LEVEL_LABELS } from "./blockStyle";
import {
  componentCost,
  migrationRoles,
  placementTrail,
  readiness,
} from "./insight";
import {
  COMPONENT_STATUS_LABELS,
  LAYER_LABELS,
  componentTone,
  dependents,
  kindLabel,
} from "./model";
import { buildMarkers } from "./securityModel";
import {
  SOURCE_LABELS,
  canOpen,
  displayUrl,
  endpointWarning,
  primaryEndpoint,
  sortedEndpoints,
} from "./endpointModel";
import {
  podAge,
  podCells,
  podLabel,
  podSummary,
  podTone,
  type PodSummary,
} from "./podModel";

const JOB_TONES: Partial<Record<MigrationStatus, Tone>> = {
  completed: "ok",
  running: "running",
  waiting_approval: "warn",
  needs_recovery: "bad",
  aborted: "bad",
};

function ComponentLinks({
  env,
  items,
  onSelect,
}: {
  env: FleetEnvironment;
  items: EnvComponent[];
  onSelect?: (id: string) => void;
}) {
  if (items.length === 0) return <p className="fp-muted">없음</p>;
  return (
    <ul className="cp-links">
      {items.map((c) => {
        const body = (
          <>
            <StatusDot tone={componentTone(c.status)}>{c.name}</StatusDot>
            <span className="cp-sub">{kindLabel(c.kind)}</span>
          </>
        );
        return (
          <li key={c.id}>
            {onSelect ? (
              <button type="button" onClick={() => onSelect(c.id)}>
                {body}
              </button>
            ) : (
              <a href={resourceHash(env.id, c.id)}>{body}</a>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ExternalIcon() {
  return (
    <svg className="ext-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M9 3h4v4M13 3L7.5 8.5M11 9.5V13H3V5h3.5" />
    </svg>
  );
}

/** 바깥에서 열 수 있는 주소는 늘 보이고, 클러스터 내부 주소는 많으면 접어 둔다. */
function EndpointList({ endpoints }: { endpoints: ComponentEndpoint[] }) {
  const outside = endpoints.filter((e) => e.source !== "cluster_ip");
  const inside = endpoints.filter((e) => e.source === "cluster_ip");
  const [open, setOpen] = useState(outside.length === 0 && inside.length <= 2);
  return (
    <>
      {outside.length > 0 && (
        <ul className="cp-endpoints">
          {outside.map((endpoint) => (
            <EndpointRow key={endpoint.url} endpoint={endpoint} />
          ))}
        </ul>
      )}
      {outside.length === 0 && (
        <p className="fp-muted">클러스터 안에서만 접속할 수 있어요.</p>
      )}
      {inside.length > 0 &&
        (open ? (
          <ul className="cp-endpoints inside">
            {inside.map((endpoint) => (
              <EndpointRow key={endpoint.url} endpoint={endpoint} />
            ))}
          </ul>
        ) : (
          <button
            type="button"
            className="cp-more"
            onClick={() => setOpen(true)}
          >
            클러스터 내부 주소 {inside.length}개 보기
          </button>
        ))}
    </>
  );
}

/** 접속 주소 한 줄: 주소(열기 링크) + 복사, 아래에 출처, 공개 범위, 용도, 경고. */
function EndpointRow({ endpoint }: { endpoint: ComponentEndpoint }) {
  const [copied, setCopied] = useState(false);
  const warning = endpointWarning(endpoint);
  const copy = () => {
    void navigator.clipboard?.writeText(endpoint.url).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  };
  return (
    <li>
      <div className="cp-ep-main">
        {canOpen(endpoint) ? (
          <a
            className="cp-ep-url"
            href={endpoint.url}
            target="_blank"
            rel="noopener noreferrer"
            title={endpoint.url}
          >
            <span className="cp-ep-text">{displayUrl(endpoint.url)}</span>
            <ExternalIcon />
          </a>
        ) : (
          <span className="cp-ep-url plain" title={endpoint.url}>
            <span className="cp-ep-text">{displayUrl(endpoint.url)}</span>
          </span>
        )}
        <button
          type="button"
          className="cp-ep-copy"
          onClick={copy}
          aria-label={`${endpoint.url} 복사`}
        >
          {copied ? "복사됨" : "복사"}
        </button>
      </div>
      <p className="cp-ep-sub">
        {[
          endpoint.label,
          SOURCE_LABELS[endpoint.source],
          endpoint.exposure === "public" ? "공개" : "내부망",
        ]
          .filter(Boolean)
          .join(", ")}
        {warning && <span className="text-warn">, {warning}</span>}
      </p>
    </li>
  );
}

/** 파드: 칸(파드 하나 = 한 칸, 상태색) + 준비 수, 파드별 목록(이름, 상태, 노드, 시작 후, 재시작, 컨테이너). */
function PodsGroup({
  component,
  summary,
  onLogs,
}: {
  component: EnvComponent;
  summary: PodSummary;
  onLogs?: (pod: string) => void;
}) {
  const { cells, hidden } = podCells(component, 40);
  return (
    <PanelGroup label="파드">
      <div className="cp-pods-head">
        <span className="cp-pod-cells" aria-hidden="true">
          {cells.map((cell) => (
            <i
              key={cell.key}
              className={`tone-${cell.tone}`}
              title={cell.title}
            />
          ))}
          {hidden > 0 && <em>+{hidden}</em>}
        </span>
        <span>
          준비{" "}
          <b className={summary.ready < summary.total ? "text-bad" : undefined}>
            {summary.ready}/{summary.total}
          </b>
          {summary.restarts !== null && summary.restarts > 0 && (
            <>
              , 재시작 <b className="text-warn">{summary.restarts}</b>
            </>
          )}
        </span>
      </div>
      {component.pods && component.pods.length > 0 ? (
        <ul className="cp-pods">
          {component.pods.map((pod) => {
            const ready = pod.containers.filter((c) => c.ready).length;
            return (
              <li key={pod.name}>
                <div className="cp-pod-main">
                  <StatusDot tone={podTone(pod)}>
                    <span className="cp-pod-name" title={pod.name}>
                      {pod.name}
                    </span>
                  </StatusDot>
                  <span
                    className={`cp-pod-restarts${pod.restarts > 0 ? " text-warn" : ""}`}
                  >
                    재시작 {pod.restarts}
                  </span>
                  {onLogs && (
                    <button
                      type="button"
                      className="cp-pod-logs"
                      aria-label={`${pod.name} 로그 보기`}
                      onClick={() => onLogs(pod.name)}
                    >
                      로그
                    </button>
                  )}
                </div>
                <p className="cp-pod-sub">
                  {[
                    podLabel(pod),
                    pod.node ? `노드 ${pod.node}` : null,
                    podAge(pod.started_at)
                      ? `${podAge(pod.started_at)} 전 시작`
                      : null,
                    pod.containers.length > 0
                      ? `컨테이너 ${ready}/${pod.containers.length}`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(", ")}
                </p>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="fp-muted">
          파드별 정보(노드, 재시작 횟수)는 아직 받지 못했어요.
        </p>
      )}
    </PanelGroup>
  );
}

/**
 * 구성 요소 상세(오른쪽 패널, Cloudcraft/Datadog 리소스 패널 방식).
 * 배치 → 상태 → 연결 → 이전 작업 → 보안 → 월 비용. 다이어그램, 계층 트리, 구성 요소 표가 같이 쓴다.
 */
export function ComponentPanel({
  env,
  component,
  position,
  onMove,
  onClose,
  onSelect,
  showDiagramLink,
}: {
  env: FleetEnvironment;
  component: EnvComponent;
  position?: { index: number; total: number };
  onMove?: (step: 1 | -1) => void;
  onClose: () => void;
  /** 연결된 구성 요소를 누르면 그 구성 요소로 옮겨 간다. 없으면 다이어그램 링크. */
  onSelect?: (id: string) => void;
  showDiagramLink?: boolean;
}) {
  const [logPod, setLogPod] = useState<string | null>(null);
  const fleet = useQuery(fleetQueryOptions()).data;
  const posture = useQuery(postureQueryOptions()).data;
  const costs = useQuery(costsQueryOptions()).data;
  const byId = (id: string) => env.components.find((c) => c.id === id);
  const uses = component.depends_on
    .map(byId)
    .filter((c): c is EnvComponent => c !== undefined);
  const usedBy = dependents(env.components, component.id);
  const trail = placementTrail(env, component);
  const state = readiness(component.detail);
  const roles = fleet
    ? migrationRoles(env, component, fleet.migrations, fleet.environments)
    : [];
  const controls =
    buildMarkers(env, posture?.controls ?? [], {
      group: null,
      hidden: [],
    }).get(`tile:${env.id}:${component.id}`)?.controls ?? [];
  const envCost = costs?.environments.find((e) => e.env_id === env.id);
  const cost = componentCost(component, envCost);
  const inCluster = trail.some((t) => t.level === "cluster");
  const tone = componentTone(component.status);
  const pods = podSummary(component);
  const endpoints = sortedEndpoints(component);
  const primary = primaryEndpoint(component);
  const logTarget = logPod
    ? component.pods?.find((pod) => pod.name === logPod)
    : undefined;
  if (logTarget)
    return (
      <PodLogPanel
        env={env}
        component={component}
        pod={logTarget}
        onBack={() => setLogPod(null)}
        onClose={onClose}
      />
    );
  return (
    <SidePanel
      kind="구성 요소"
      id={kindLabel(component.kind)}
      position={position}
      onMove={onMove}
      onClose={onClose}
      title={
        <>
          {component.name}
          <StatusLabel tone={tone}>
            {COMPONENT_STATUS_LABELS[component.status]}
          </StatusLabel>
        </>
      }
      facts={[
        {
          label: "환경",
          value: (
            <span className="cp-env">
              <ProviderLogo provider={env.provider} />
              {env.name}
            </span>
          ),
        },
        { label: "계층", value: LAYER_LABELS[component.layer] },
        {
          label: "운영",
          value: component.managed ? "관리형 서비스" : "클러스터 워크로드",
        },
      ]}
      actions={
        primary || showDiagramLink ? (
          <>
            {primary && (
              <a
                className="pk-button"
                href={primary.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                열기
                <ExternalIcon />
              </a>
            )}
            {showDiagramLink && (
              <a
                className="pk-button weak"
                href={resourceHash(env.id, component.id)}
              >
                다이어그램에서 보기
              </a>
            )}
          </>
        ) : undefined
      }
    >
      {endpoints.length > 0 && (
        <PanelGroup label="접속 주소">
          <EndpointList key={component.id} endpoints={endpoints} />
        </PanelGroup>
      )}
      <PanelGroup label="배치">
        {trail.length === 0 ? (
          <p className="fp-muted">배치 정보 없음</p>
        ) : (
          <ol className="cp-trail">
            {trail.map((t) => (
              <li key={t.label} title={LEVEL_LABELS[t.level]}>
                <i style={{ borderColor: LEVEL_COLORS[t.level] }} />
                {t.label}
              </li>
            ))}
          </ol>
        )}
      </PanelGroup>
      {(state.workloads !== null || state.notes.length > 0 || !pods) && (
        <PanelGroup label="상태">
          {state.workloads !== null && <p>워크로드 {state.workloads}개</p>}
          {state.notes.map((note) => (
            <p key={note} className="cp-note">
              {note}
            </p>
          ))}
          {state.workloads === null && state.notes.length === 0 && (
            <p>{COMPONENT_STATUS_LABELS[component.status]}</p>
          )}
        </PanelGroup>
      )}
      {pods && (
        <PodsGroup
          component={component}
          summary={pods}
          onLogs={component.logs ? setLogPod : undefined}
        />
      )}
      <PanelGroup label="연결">
        <div className="cp-conn">
          <h4>
            쓰는 구성 요소 <b>{uses.length}</b>
          </h4>
          <ComponentLinks env={env} items={uses} onSelect={onSelect} />
          <h4>
            이 구성 요소를 쓰는 곳 <b>{usedBy.length}</b>
          </h4>
          <ComponentLinks env={env} items={usedBy} onSelect={onSelect} />
        </div>
      </PanelGroup>
      {roles.length > 0 && (
        <PanelGroup label="이전 작업">
          <ul className="cp-jobs">
            {roles.map(({ job, role }) => (
              <li key={job.job_id}>
                <span className="cp-job-head">
                  <a href={migrationHash(job.job_id)}>{job.job_id}</a>
                  <span className="cp-sub">{role}</span>
                </span>
                <StatusDot tone={JOB_TONES[job.status] ?? "idle"}>
                  {STATUS_LABELS[job.status]}
                  {job.stage ? `, ${stageTitle(job.stage)}` : ""}
                </StatusDot>
              </li>
            ))}
          </ul>
        </PanelGroup>
      )}
      <PanelGroup label="보안">
        {posture === undefined ? (
          <p className="fp-muted">불러오는 중</p>
        ) : controls.length === 0 ? (
          <p className="fp-muted">실패한 통제 없음</p>
        ) : (
          <ul className="cp-controls">
            {controls.map((control) => (
              <li key={control.id}>
                <SeverityMeter severity={control.severity} label={false} />
                <a href={controlHash(control.id)}>{control.title}</a>
              </li>
            ))}
          </ul>
        )}
      </PanelGroup>
      <PanelGroup label="월 비용">
        {cost ? (
          <>
            <p className="cp-cost">
              {usd(cost.monthly)}
              <small>/월</small>
            </p>
            <ul className="cp-cost-lines">
              {cost.lines.map((line) => (
                <li key={line.item}>
                  <span>{line.item.split(" / ")[0]}</span>
                  <b>{usd(line.monthly)}</b>
                </li>
              ))}
            </ul>
          </>
        ) : inCluster ? (
          <p>
            클러스터 노드 비용에 포함.{" "}
            <a className="text-link" href="#costs/environments">
              환경별 비용 보기
            </a>
          </p>
        ) : (
          <p className="fp-muted">가격 정보 없음</p>
        )}
      </PanelGroup>
    </SidePanel>
  );
}
