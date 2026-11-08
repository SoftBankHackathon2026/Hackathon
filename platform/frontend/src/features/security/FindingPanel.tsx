import type { FleetEnvironment } from "../../api/migrations";
import type { Control, Posture } from "../../api/security";
import { resourceHash } from "../../app/routes";
import { ProviderLogo } from "../../components/ProviderLogo";
import { PanelGroup, SidePanel } from "../../components/SidePanel";
import { ImpactPath } from "./ImpactPath";
import { impactPath } from "./pathModel";
import {
  CATEGORY_LABELS,
  SEVERITY_LABELS,
  SOURCE_HELP,
  SOURCE_LABELS,
  STATUS_LABELS,
  standardId,
} from "./posture";

const STATUS_DOT = {
  fail: "bad",
  pass: "ok",
  unknown: "idle",
  not_applicable: "none",
  manual: "none",
} as const;

/**
 * 보안 문제 상세(Datadog 사이드 패널처럼). 표 위에 오른쪽에서 열리고,
 * 다른 행을 누르면 내용만 바뀐다. ↑↓ 로 이전·다음 문제, Esc 로 닫는다.
 */
export function FindingPanel({
  control,
  posture,
  fleetEnvs,
  position,
  onMove,
  onClose,
}: {
  control: Control;
  posture: Posture;
  fleetEnvs: FleetEnvironment[];
  position: { index: number; total: number };
  onMove: (step: 1 | -1) => void;
  onClose: () => void;
}) {
  const results = posture.environments.map((env) => ({
    env,
    result: control.results[env.env_id],
    fleetEnv: fleetEnvs.find((e) => e.id === env.env_id),
  }));
  const paths = results
    .filter((r) => r.result?.status === "fail" && r.fleetEnv)
    .map((r) => ({
      ...r,
      path: impactPath(r.fleetEnv!.components, r.result?.resources ?? []),
    }))
    .filter((r) => r.path.columns.length > 1);
  const failing = results.filter((r) => r.result?.status === "fail").length;
  const resources = new Set(results.flatMap((r) => r.result?.resources ?? []))
    .size;

  return (
    <SidePanel
      kind="보안 통제"
      id={control.id}
      position={position}
      onMove={onMove}
      onClose={onClose}
      title={
        <>
          <span className={`sev-tag sev-${control.severity}`}>
            {SEVERITY_LABELS[control.severity]}
          </span>
          {control.title}
        </>
      }
      facts={[
        { label: "분류", value: CATEGORY_LABELS[control.category] },
        {
          label: "실패 환경",
          value: `${failing} / ${results.length}`,
          tone: failing > 0 ? "bad" : undefined,
        },
        {
          label: "영향 리소스",
          value:
            control.scope === "environment" ? "환경 단위" : `${resources}개`,
        },
      ]}
    >
      <PanelGroup label="왜 위험한가">
        <p>{control.why || control.title}</p>
      </PanelGroup>
      {paths.length > 0 && (
        <PanelGroup label="영향 경로" wide>
          {paths.map(({ env, fleetEnv, path }) => (
            <div key={env.env_id} className="ip-env">
              <span className="pk-path">
                <ProviderLogo provider={fleetEnv?.provider} />
                {env.name}
              </span>
              <ImpactPath columns={path.columns} edges={path.edges} />
            </div>
          ))}
          <p className="ip-legend">
            <i aria-hidden="true" /> 실패한 리소스
          </p>
        </PanelGroup>
      )}

      <PanelGroup label="환경별 결과">
        <ul className="fp-envs">
          {results.map(({ env, result, fleetEnv }) => (
            <li key={env.env_id}>
              <div className="fp-env-head">
                <ProviderLogo provider={fleetEnv?.provider} />
                <span className="fp-env-name">{env.name}</span>
                {result?.source && (
                  <span
                    className={`fp-source source-${result.source}`}
                    title={SOURCE_HELP[result.source]}
                  >
                    {SOURCE_LABELS[result.source]}
                  </span>
                )}
                {result ? (
                  <span
                    className={`pk-label tone-${STATUS_DOT[result.status]}`}
                  >
                    {STATUS_LABELS[result.status]}
                  </span>
                ) : (
                  <span className="pk-label tone-none">-</span>
                )}
              </div>
              {result?.detail && (
                <p className="fp-env-detail">{result.detail}</p>
              )}
              {result?.fallback_reason && (
                <p className="fp-env-detail fp-fallback">
                  실측 못 함: {result.fallback_reason}
                </p>
              )}
              {(result?.resources?.length ?? 0) > 0 && (
                <ul className="fp-resources" aria-label="영향 리소스">
                  {(result?.resources ?? []).map((name) => {
                    const found = fleetEnv?.components.find(
                      (c) => c.name === name,
                    );
                    return (
                      <li key={name}>
                        {found ? (
                          <a href={resourceHash(env.env_id, found.id)}>
                            {name}
                          </a>
                        ) : (
                          <span>{name}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          ))}
        </ul>
      </PanelGroup>

      <PanelGroup label="조치 방법">
        <p className="fp-remedy">
          {control.remediation || "등록된 조치 방법이 없어요."}
        </p>
      </PanelGroup>

      <PanelGroup label="관련 표준">
        {control.standard_refs.length === 0 ? (
          <p className="fp-muted">연결된 표준이 없어요.</p>
        ) : (
          <ul className="fp-refs">
            {control.standard_refs.map((ref) => (
              <li key={ref}>
                <code>{ref}</code>
                <span>
                  {posture.standards.find((s) => s.id === standardId(ref))
                    ?.name ?? ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </PanelGroup>
    </SidePanel>
  );
}
