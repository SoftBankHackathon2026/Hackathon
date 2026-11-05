import { useState } from "react";
import type { FleetEnvironment } from "../../api/migrations";
import {
  COMPONENT_STATUS_LABELS,
  LAYER_LABELS,
  componentRows,
  componentTone,
  filterComponentRows,
  kindLabel,
} from "./model";

export function ComponentsList({
  environments,
}: {
  environments: FleetEnvironment[];
}) {
  const [query, setQuery] = useState("");
  const rows = filterComponentRows(componentRows(environments), "all", query);
  return (
    <section className="ux-section">
      <div className="ux-bar">
        <input
          type="search"
          aria-label="구성 요소 검색"
          placeholder="구성 요소 검색"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <span className="ux-count">{rows.length}개</span>
      </div>
      <table className="ux-table">
        <thead>
          <tr>
            <th className="dot-col" />
            <th>구성 요소</th>
            <th>환경</th>
            <th>계층</th>
            <th>종류</th>
            <th>배치</th>
            <th>상태</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const c = row.component;
            return (
              <tr
                key={`${row.envId}:${c.id}`}
                className={c.status === "unknown" ? "dim" : undefined}
              >
                <td className="dot-col">
                  <i
                    className={`dot dot-${componentTone(c.status)}`}
                    aria-hidden="true"
                  />
                </td>
                <td>{c.name}</td>
                <td>{row.envName}</td>
                <td>{LAYER_LABELS[c.layer]}</td>
                <td>
                  {kindLabel(c.kind)}
                  {c.managed ? " (관리형)" : ""}
                </td>
                <td>{row.placement || "-"}</td>
                <td>{COMPONENT_STATUS_LABELS[c.status]}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
