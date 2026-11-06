import { useCallback, useEffect, useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import { StatusLabel } from "../../components/PageKit";
import { ProviderLogo } from "../../components/ProviderLogo";
import { useSort } from "../../components/SortableTable";
import { ComponentPanel } from "./ComponentPanel";
import { podSummary } from "./podModel";
import { primaryEndpoint } from "./endpointModel";
import {
  COMPONENT_STATUS_LABELS,
  LAYER_LABELS,
  componentRows,
  componentTone,
  filterComponentRows,
  kindLabel,
  type ComponentRow,
} from "./model";

const STATUS_RANK = {
  down: 4,
  degraded: 3,
  unknown: 2,
  idle: 1,
  healthy: 0,
} as const;

/** 구성 요소 표: 검색·정렬, 행을 누르면 오른쪽 패널(다이어그램 위치로 가는 링크 포함). */
export function ComponentsList({
  environments,
}: {
  environments: FleetEnvironment[];
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | undefined>();
  // "다이어그램에서 보기"처럼 주소가 바뀌면 표 패널은 닫는다(패널은 하나만).
  useEffect(() => {
    const close = () => setSelected(undefined);
    window.addEventListener("hashchange", close);
    return () => window.removeEventListener("hashchange", close);
  }, []);
  const rows = filterComponentRows(componentRows(environments), "all", query);
  const { sorted, th } = useSort(rows, {
    name: (r) => r.component.name,
    env: (r) => r.envName,
    layer: (r) => LAYER_LABELS[r.component.layer],
    kind: (r) => kindLabel(r.component.kind),
    pods: (r) => {
      const p = podSummary(r.component);
      return p ? p.total - p.ready + p.total / 1000 : null;
    },
    status: (r) => STATUS_RANK[r.component.status],
  });
  const key = (r: ComponentRow) => `${r.envId}:${r.component.id}`;
  const ids = sorted.map(key).join("|");
  const move = useCallback(
    (step: 1 | -1) => {
      const list = ids.split("|");
      const at = list.indexOf(selected ?? "");
      const next = list[Math.min(list.length - 1, Math.max(0, at + step))];
      if (next) setSelected(next);
    },
    [ids, selected],
  );
  const close = useCallback(() => setSelected(undefined), []);
  const index = sorted.findIndex((r) => key(r) === selected);
  const current = index >= 0 ? sorted[index] : undefined;
  return (
    <>
      <input
        type="search"
        className="pk-facet-search pk-table-search"
        aria-label="구성 요소 검색"
        placeholder="이름/종류/배치로 찾기"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {rows.length === 0 ? (
        <p className="pk-empty">맞는 구성 요소가 없어요.</p>
      ) : (
        <table className="pk-table sticky" data-panel-owner>
          <thead>
            <tr>
              {th("name", "구성 요소")}
              {th("env", "환경")}
              {th("layer", "계층")}
              {th("kind", "종류")}
              {th("pods", "파드", "num")}
              <th>배치</th>
              {th("status", "상태")}
              <th>접속</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const c = row.component;
              const isOpen = key(row) === selected;
              return (
                <tr
                  key={key(row)}
                  className={`clickable${c.status === "unknown" || c.status === "idle" ? " dim" : ""}${isOpen ? " selected" : ""}`}
                  tabIndex={0}
                  aria-selected={isOpen}
                  onClick={() => setSelected(isOpen ? undefined : key(row))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setSelected(isOpen ? undefined : key(row));
                    }
                  }}
                >
                  <td className="strong">{c.name}</td>
                  <td>
                    <span className="pk-path">
                      <ProviderLogo provider={row.provider} />
                      {row.envName}
                    </span>
                  </td>
                  <td>{LAYER_LABELS[c.layer]}</td>
                  <td>
                    {kindLabel(c.kind)}
                    {c.managed && <span className="sub"> 관리형</span>}
                  </td>
                  <td className="num">
                    {(() => {
                      const p = podSummary(c);
                      if (!p) return <span className="sub">-</span>;
                      return (
                        <>
                          <span
                            className={
                              p.ready < p.total ? "text-bad" : undefined
                            }
                          >
                            {p.ready}/{p.total}
                          </span>
                          {p.restarts !== null && p.restarts > 0 && (
                            <span className="sub text-warn">
                              {" "}
                              재시작 {p.restarts}
                            </span>
                          )}
                        </>
                      );
                    })()}
                  </td>
                  <td
                    className="sub one-line"
                    title={row.placement || undefined}
                  >
                    {row.placement || "-"}
                  </td>
                  <td>
                    <StatusLabel tone={componentTone(c.status)}>
                      {COMPONENT_STATUS_LABELS[c.status]}
                    </StatusLabel>
                  </td>
                  <td>
                    {(() => {
                      const ep = primaryEndpoint(c);
                      if (!ep) return <span className="sub">-</span>;
                      return (
                        <a
                          className="ep-open"
                          href={ep.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={ep.url}
                          onClick={(event) => event.stopPropagation()}
                          onKeyDown={(event) => event.stopPropagation()}
                        >
                          열기
                          <svg
                            className="ext-icon"
                            viewBox="0 0 16 16"
                            aria-hidden="true"
                          >
                            <path d="M9 3h4v4M13 3L7.5 8.5M11 9.5V13H3V5h3.5" />
                          </svg>
                        </a>
                      );
                    })()}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {current && (
        <ComponentPanel
          env={environments.find((e) => e.id === current.envId)!}
          component={current.component}
          showDiagramLink
          position={{ index, total: sorted.length }}
          onMove={move}
          onClose={close}
        />
      )}
    </>
  );
}
