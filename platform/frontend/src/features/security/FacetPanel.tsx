import { useState } from "react";
import type {
  Category,
  ControlStatus,
  Posture,
  Severity,
} from "../../api/security";
import {
  ALL_CATEGORIES,
  ALL_SEVERITIES,
  SHOWN_STATUSES,
  facetCounts,
  toggle,
  type ExplorerFilters,
} from "./explorer";
import { CATEGORY_LABELS, SEVERITY_LABELS, STATUS_LABELS } from "./posture";

function Group({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="pk-facet-group">
      <button
        type="button"
        className="pk-facet-head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="dash-chevron" aria-hidden="true" />
        {title}
      </button>
      {open && <div className="pk-facet-body">{children}</div>}
    </div>
  );
}

function Option({
  checked,
  onChange,
  count,
  bar,
  children,
}: {
  checked: boolean;
  onChange: () => void;
  count: number;
  bar?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="pk-option">
      <input type="checkbox" checked={checked} onChange={onChange} />
      {bar && <i className={`pk-statusbar ${bar}`} aria-hidden="true" />}
      <span className="pk-option-label">{children}</span>
      <span className="pk-option-count">{count}</span>
    </label>
  );
}

const STATUS_BARS: Record<string, string> = {
  fail: "bar-fail",
  unknown: "bar-unknown",
  pass: "bar-pass",
};

export function FacetPanel({
  posture,
  filters,
  onChange,
  onReset,
}: {
  posture: Posture;
  filters: ExplorerFilters;
  onChange: (patch: Partial<ExplorerFilters>) => void;
  onReset: () => void;
}) {
  const counts = facetCounts(posture.controls, filters);
  return (
    <aside className="pk-facets" aria-label="필터">
      <Group title="심각도">
        {ALL_SEVERITIES.map((s: Severity) => (
          <Option
            key={s}
            bar={`sev-${s}`}
            checked={filters.severity.includes(s)}
            count={counts.severity[s]}
            onChange={() => onChange({ severity: toggle(filters.severity, s) })}
          >
            {SEVERITY_LABELS[s]}
          </Option>
        ))}
      </Group>
      <Group title="상태">
        {SHOWN_STATUSES.map((s: ControlStatus) => (
          <Option
            key={s}
            bar={STATUS_BARS[s]}
            checked={filters.status.includes(s)}
            count={counts.status[s]}
            onChange={() => onChange({ status: toggle(filters.status, s) })}
          >
            {STATUS_LABELS[s]}
          </Option>
        ))}
      </Group>
      <Group title="분류">
        {ALL_CATEGORIES.map((c: Category) => (
          <Option
            key={c}
            checked={filters.category.includes(c)}
            count={counts.category[c]}
            onChange={() => onChange({ category: toggle(filters.category, c) })}
          >
            {CATEGORY_LABELS[c]}
          </Option>
        ))}
      </Group>
      <Group title="표준">
        {posture.standards.map((std) => (
          <Option
            key={std.id}
            checked={filters.standard.includes(std.id)}
            count={counts.standard[std.id] ?? 0}
            onChange={() =>
              onChange({ standard: toggle(filters.standard, std.id) })
            }
          >
            {std.id}
          </Option>
        ))}
      </Group>
      <Group title="환경">
        {posture.environments.map((env) => (
          <Option
            key={env.env_id}
            checked={filters.env.includes(env.env_id)}
            count={counts.env[env.env_id] ?? 0}
            onChange={() => onChange({ env: toggle(filters.env, env.env_id) })}
          >
            {env.name}
          </Option>
        ))}
      </Group>
      <button type="button" className="pk-reset" onClick={onReset}>
        필터 초기화
      </button>
    </aside>
  );
}
