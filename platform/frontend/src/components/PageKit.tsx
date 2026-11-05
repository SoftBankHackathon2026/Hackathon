import { useState, type ReactNode } from "react";
import type { Tone } from "../features/migrations/stages";

export function PageHeader({
  title,
  tabs,
  active,
  onTab,
}: {
  title: string;
  tabs?: { id: string; label: string }[];
  active?: string;
  onTab?: (id: string) => void;
}) {
  return (
    <header className="pk-header">
      <h1>{title}</h1>
      {tabs && (
        <nav className="pk-tabs" aria-label={`${title} 보기`}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              aria-current={active === tab.id ? "page" : undefined}
              onClick={() => onTab?.(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      )}
    </header>
  );
}

export function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="pk-filter">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ScopeFilterRow({ children }: { children: ReactNode }) {
  return (
    <div className="pk-scope" role="group" aria-label="이 페이지 필터">
      <span className="pk-scope-title">이 페이지 필터</span>
      {children}
    </div>
  );
}

export function Section({
  id,
  title,
  description,
  action,
  children,
}: {
  id?: string;
  title: string;
  description?: string;
  action?: { label: string; href?: string; onClick?: () => void };
  children: ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section className="pk-section" id={id}>
      <header className="pk-section-head">
        <button
          type="button"
          className="pk-chevron"
          aria-expanded={open}
          aria-label={`${title} ${open ? "접기" : "펼치기"}`}
          onClick={() => setOpen(!open)}
        >
          <span className="dash-chevron" aria-hidden="true" />
        </button>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
        {action &&
          (action.href ? (
            <a className="pk-action" href={action.href}>
              {action.label} →
            </a>
          ) : (
            <button
              type="button"
              className="pk-action"
              onClick={action.onClick}
            >
              {action.label} →
            </button>
          ))}
      </header>
      {open && <div className="pk-section-body">{children}</div>}
    </section>
  );
}

export function StatusLabel({
  tone,
  children,
}: {
  tone: Tone | "none";
  children: ReactNode;
}) {
  return <span className={`pk-label tone-${tone}`}>{children}</span>;
}

export function SummaryCard({
  title,
  label,
  value,
  unit,
  lines,
}: {
  title: string;
  label: { tone: Tone; text: string } | null;
  value: string;
  unit: string;
  lines: ReactNode[];
}) {
  return (
    <div className="pk-card">
      <div className="pk-card-head">
        <h3>{title}</h3>
        {label && <StatusLabel tone={label.tone}>{label.text}</StatusLabel>}
      </div>
      <div className="pk-card-value">
        <span>{value}</span>
        {unit && <small>{unit}</small>}
      </div>
      {lines.map((line, i) => (
        <div key={i} className="pk-card-line">
          {line}
        </div>
      ))}
    </div>
  );
}
