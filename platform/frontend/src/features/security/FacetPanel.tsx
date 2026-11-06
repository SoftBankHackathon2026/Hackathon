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
  children,
}: {
  checked: boolean;
  onChange: () => void;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <label className={`pk-option${count === 0 ? " zero" : ""}`}>
      <input type="checkbox" checked={checked} onChange={onChange} />
      <span className="pk-option-label">{children}</span>
      <span className="pk-option-count">{count}</span>
    </label>
  );
}

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
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const groups: {
    title: string;
    options: {
      key: string;
      label: string;
      checked: boolean;
      count: number;
      onChange: () => void;
    }[];
  }[] = [
    {
      title: "심각도",
      options: ALL_SEVERITIES.map((s: Severity) => ({
        key: s,
        label: SEVERITY_LABELS[s],
        checked: filters.severity.includes(s),
        count: counts.severity[s],
        onChange: () => onChange({ severity: toggle(filters.severity, s) }),
      })),
    },
    {
      title: "상태",
      options: SHOWN_STATUSES.map((s: ControlStatus) => ({
        key: s,
        label: STATUS_LABELS[s],
        checked: filters.status.includes(s),
        count: counts.status[s],
        onChange: () => onChange({ status: toggle(filters.status, s) }),
      })),
    },
    {
      title: "분류",
      options: ALL_CATEGORIES.map((c: Category) => ({
        key: c,
        label: CATEGORY_LABELS[c],
        checked: filters.category.includes(c),
        count: counts.category[c],
        onChange: () => onChange({ category: toggle(filters.category, c) }),
      })),
    },
    {
      title: "표준",
      options: posture.standards.map((std) => ({
        key: std.id,
        label: std.id,
        checked: filters.standard.includes(std.id),
        count: counts.standard[std.id] ?? 0,
        onChange: () =>
          onChange({ standard: toggle(filters.standard, std.id) }),
      })),
    },
    {
      title: "환경",
      options: posture.environments.map((env) => ({
        key: env.env_id,
        label: env.name,
        checked: filters.env.includes(env.env_id),
        count: counts.env[env.env_id] ?? 0,
        onChange: () => onChange({ env: toggle(filters.env, env.env_id) }),
      })),
    },
  ];
  const shown = groups
    .map((group) => ({
      ...group,
      options:
        !q || group.title.toLowerCase().includes(q)
          ? group.options
          : group.options.filter((o) => o.label.toLowerCase().includes(q)),
    }))
    .filter((group) => group.options.length > 0);
  return (
    <aside className="pk-facets" aria-label="필터">
      <input
        type="search"
        className="pk-facet-search"
        placeholder="필터 찾기"
        aria-label="필터 찾기"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {shown.length === 0 && <p className="pk-empty">맞는 필터가 없어요.</p>}
      {shown.map((group) => (
        <Group key={group.title} title={group.title}>
          {group.options.map((o) => (
            <Option
              key={o.key}
              checked={o.checked}
              count={o.count}
              onChange={o.onChange}
            >
              {o.label}
            </Option>
          ))}
        </Group>
      ))}
      <button type="button" className="pk-reset" onClick={onReset}>
        필터 초기화
      </button>
    </aside>
  );
}
