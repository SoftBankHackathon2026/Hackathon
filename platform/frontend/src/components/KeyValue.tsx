import type { ReactNode } from "react";

export function KeyValue({
  rows,
}: {
  rows: { label: ReactNode; value: ReactNode }[];
}) {
  return (
    <dl className="kv">
      {rows.map((row, i) => (
        <div className="kv-row" key={i}>
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}
