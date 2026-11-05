import { Fragment, useEffect, useRef, useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import type { Posture } from "../../api/security";
import { resourceHash } from "../../app/routes";
import {
  affected,
  filterFindings,
  toggle,
  type ExplorerFilters,
} from "./explorer";
import {
  CATEGORY_LABELS,
  SEVERITY_LABELS,
  STATUS_LABELS,
  standardId,
} from "./posture";

export function FindingsTable({
  posture,
  fleetEnvs,
  filters,
  focus,
}: {
  posture: Posture;
  fleetEnvs: FleetEnvironment[];
  filters: ExplorerFilters;
  focus: string | undefined;
}) {
  const [open, setOpen] = useState<string[]>(focus ? [focus] : []);
  const rowRef = useRef<HTMLTableRowElement | null>(null);
  const findings = filterFindings(posture.controls, filters);
  useEffect(() => {
    if (focus)
      setOpen((prev) => (prev.includes(focus) ? prev : [...prev, focus]));
  }, [focus]);
  useEffect(() => {
    rowRef.current?.scrollIntoView({ block: "start" });
  }, [focus]);
  if (findings.length === 0)
    return <p className="ux-muted">조건에 맞는 항목이 없어요.</p>;
  const columns = 4 + posture.environments.length;
  const total = posture.controls.length;
  return (
    <>
      <p className="pk-count">
        {total}개 중 1–{findings.length}
      </p>
      <table className="pk-table">
        <thead>
          <tr>
            <th className="pk-status-col">심각도</th>
            <th>통제</th>
            <th>분류</th>
            {posture.environments.map((env) => (
              <th key={env.env_id}>{env.name.split(" ")[0]}</th>
            ))}
            <th className="num">영향 리소스</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((control) => {
            const isOpen = open.includes(control.id);
            const hit = affected(control, filters);
            const flip = () => setOpen(toggle(open, control.id));
            const failing = posture.environments.some(
              (e) => control.results[e.env_id]?.status === "fail",
            );
            return (
              <Fragment key={control.id}>
                <tr
                  id={`finding-${control.id}`}
                  ref={control.id === focus ? rowRef : undefined}
                  className={`clickable${failing ? "" : " dim"}${isOpen ? " open" : ""}`}
                  tabIndex={0}
                  aria-expanded={isOpen}
                  onClick={flip}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      flip();
                    }
                  }}
                >
                  <td className="pk-status-col">
                    <span className={`pk-label sev-${control.severity}`}>
                      {SEVERITY_LABELS[control.severity]}
                    </span>
                  </td>
                  <td>
                    {control.title} <span className="ux-id">{control.id}</span>
                  </td>
                  <td>{CATEGORY_LABELS[control.category]}</td>
                  {posture.environments.map((env) => {
                    const status = control.results[env.env_id]?.status;
                    return (
                      <td key={env.env_id}>
                        {status ? (
                          <span className={`ux-state st-${status}`}>
                            <i
                              className={`dot dot-${status === "fail" ? "bad" : status === "pass" ? "ok" : "idle"}`}
                              aria-hidden="true"
                            />
                            {STATUS_LABELS[status]}
                          </span>
                        ) : (
                          "-"
                        )}
                      </td>
                    );
                  })}
                  <td className="num">
                    {control.scope === "environment" ? "-" : hit.resources}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="pk-expand">
                    <td colSpan={columns}>
                      <dl className="ux-detail">
                        <div>
                          <dt>위험</dt>
                          <dd>
                            {control.why || control.title}{" "}
                            <span className="ux-id">
                              {SEVERITY_LABELS[control.severity]}
                            </span>
                          </dd>
                        </div>
                        <div>
                          <dt>영향 범위</dt>
                          <dd>
                            {posture.environments.map((env) => {
                              const result = control.results[env.env_id];
                              const fleetEnv = fleetEnvs.find(
                                (e) => e.id === env.env_id,
                              );
                              const resources = result?.resources ?? [];
                              return (
                                <p key={env.env_id}>
                                  {env.name}{" "}
                                  <span className="ux-id">
                                    {result
                                      ? STATUS_LABELS[result.status]
                                      : "-"}
                                  </span>
                                  {result?.detail ? ` · ${result.detail}` : ""}
                                  {resources.length > 0 && (
                                    <>
                                      {" · "}
                                      {resources.map((name, i) => {
                                        const found = fleetEnv?.components.find(
                                          (c) => c.name === name,
                                        );
                                        return (
                                          <span key={name}>
                                            {i > 0 && ", "}
                                            {found ? (
                                              <a
                                                className="chip-link"
                                                href={resourceHash(
                                                  env.env_id,
                                                  found.id,
                                                )}
                                              >
                                                {name}
                                              </a>
                                            ) : (
                                              name
                                            )}
                                          </span>
                                        );
                                      })}
                                    </>
                                  )}
                                </p>
                              );
                            })}
                          </dd>
                        </div>
                        <div>
                          <dt>관련 표준</dt>
                          <dd>
                            {control.standard_refs.length === 0
                              ? "연결된 표준이 없어요."
                              : control.standard_refs
                                  .map((ref) => {
                                    const name = posture.standards.find(
                                      (s) => s.id === standardId(ref),
                                    )?.name;
                                    return name ? `${ref} (${name})` : ref;
                                  })
                                  .join(", ")}
                          </dd>
                        </div>
                        <div>
                          <dt>조치 방법</dt>
                          <dd>{control.remediation || "-"}</dd>
                        </div>
                      </dl>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
