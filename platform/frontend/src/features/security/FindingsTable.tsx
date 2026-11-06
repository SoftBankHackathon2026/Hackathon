import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import type { Posture } from "../../api/security";
import { controlHash, replaceHash } from "../../app/routes";
import { affected, filterFindings, type ExplorerFilters } from "./explorer";
import { useSort } from "../../components/SortableTable";
import { FindingPanel } from "./FindingPanel";
import { ProviderLogo } from "../../components/ProviderLogo";
import { CategoryIcon } from "./CategoryIcon";
import { CATEGORY_LABELS, standardId } from "./posture";
import { SeverityMeter } from "./SeverityMeter";

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
  const [selected, setSelected] = useState<string | undefined>(focus);
  const rowRef = useRef<HTMLTableRowElement | null>(null);
  const filtered = filterFindings(posture.controls, filters);
  const SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1 } as const;
  const { sorted: findings, th } = useSort(filtered, {
    severity: (c) => SEV_RANK[c.severity],
    title: (c) => c.title,
    envs: (c) => affected(c, filters).envs.length,
    resources: (c) =>
      c.scope === "environment" ? null : affected(c, filters).resources,
  });
  // 주소가 바뀌면(뒤로 가기, 링크) 패널도 따라간다. #security 로 돌아가면 닫는다.
  useEffect(() => {
    setSelected(focus);
  }, [focus]);
  useEffect(() => {
    rowRef.current?.scrollIntoView({ block: "nearest" });
    // 열린 문제를 주소에 남겨 링크로 공유할 수 있게 한다(새 기록은 만들지 않음).
    const hash = selected ? controlHash(selected) : "#security";
    replaceHash(hash);
  }, [selected]);
  const index = findings.findIndex((c) => c.id === selected);
  const current = index >= 0 ? findings[index] : undefined;
  const ids = findings.map((c) => c.id).join(",");
  const move = useCallback(
    (step: 1 | -1) => {
      const list = ids.split(",");
      const at = list.indexOf(selected ?? "");
      const next = list[Math.min(list.length - 1, Math.max(0, at + step))];
      if (next) setSelected(next);
    },
    [ids, selected],
  );
  const close = useCallback(() => {
    const id = selected;
    setSelected(undefined);
    if (id) document.getElementById(`finding-${id}`)?.focus();
  }, [selected]);
  const provider = (id: string) => fleetEnvs.find((e) => e.id === id)?.provider;
  const order = (ids: string[]) =>
    posture.environments.map((e) => e.env_id).filter((id) => ids.includes(id));
  const envName = (id: string) =>
    posture.environments.find((e) => e.env_id === id)?.name ?? id;
  const envLabel =
    filters.status.length === 1 && filters.status[0] === "fail"
      ? "실패 환경"
      : filters.status.length === 1 && filters.status[0] === "unknown"
        ? "미확인 환경"
        : "해당 환경";
  if (findings.length === 0)
    return <p className="ux-muted">조건에 맞는 항목이 없어요.</p>;
  const total = posture.controls.length;
  return (
    <>
      <p className="pk-count">
        {total}개 중 1–{findings.length}
      </p>
      <table className="pk-table sticky" data-panel-owner>
        <thead>
          <tr>
            {th("title", "통제")}
            {th("severity", "심각도", "sev-col")}
            {th("envs", envLabel)}
            {th("resources", "영향 리소스", "num")}
            <th>관련 표준</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((control) => {
            const isOpen = control.id === selected;
            const hit = affected(control, filters);
            const flip = () => setSelected(isOpen ? undefined : control.id);
            const failing = posture.environments.some(
              (e) => control.results[e.env_id]?.status === "fail",
            );
            return (
              <Fragment key={control.id}>
                <tr
                  id={`finding-${control.id}`}
                  ref={isOpen ? rowRef : undefined}
                  className={`clickable${failing ? "" : " dim"}${isOpen ? " selected" : ""}`}
                  tabIndex={0}
                  aria-selected={isOpen}
                  onClick={flip}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      flip();
                    }
                  }}
                >
                  <td>
                    <span className="rule-cell">
                      <CategoryIcon category={control.category} />
                      <span className="rule-text">
                        <span className="rule-title">{control.title}</span>
                        <span className="rule-sub">
                          {control.id}, {CATEGORY_LABELS[control.category]}
                        </span>
                      </span>
                    </span>
                  </td>
                  <td>
                    <SeverityMeter severity={control.severity} />
                  </td>
                  <td>
                    <span
                      className="env-hits"
                      title={hit.envs.map(envName).join(", ")}
                    >
                      {order(hit.envs).map((id) => (
                        <ProviderLogo key={id} provider={provider(id)} />
                      ))}
                      <span className="sub">
                        {hit.envs.length}/{filters.env.length}
                      </span>
                    </span>
                  </td>
                  <td className="num">
                    {control.scope === "environment" ? "-" : hit.resources}
                  </td>
                  <td className="sub std-ids">
                    {[...new Set(control.standard_refs.map(standardId))].join(
                      ", ",
                    )}
                  </td>
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {current && (
        <FindingPanel
          control={current}
          posture={posture}
          fleetEnvs={fleetEnvs}
          position={{ index, total: findings.length }}
          onMove={move}
          onClose={close}
        />
      )}
    </>
  );
}
