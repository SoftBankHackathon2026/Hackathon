import type { ReactNode } from "react";
import type { Tone } from "../features/migrations/stages";

/**
 * 모든 화면의 머리. 화면 제목(24) + 상태 + 오른쪽 동작, 그 아래 한 줄 설명(선택), 맨 아래 탭.
 * 상세 화면은 crumb 로 상위 화면을 건다. 위쪽 고정 막대에는 위치와 갱신 상태만 둔다.
 */
export function PageHeader({
  title,
  crumb,
  status,
  meta,
  actions,
  tabs,
  active,
  onTab,
}: {
  title: string;
  crumb?: { label: string; href: string };
  status?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  tabs?: { id: string; label: string }[];
  active?: string;
  onTab?: (id: string) => void;
}) {
  return (
    <header className={`pk-header${tabs ? " has-tabs" : ""}`}>
      {crumb && (
        <a className="pk-crumb" href={crumb.href}>
          {crumb.label}
        </a>
      )}
      <div className="pk-title-row">
        <h1>{title}</h1>
        {status}
        {actions && <div className="pk-header-actions">{actions}</div>}
      </div>
      {meta && <p className="pk-header-meta">{meta}</p>}
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
    <div className="pk-scope" role="group" aria-label="필터">
      {children}
    </div>
  );
}

type SectionAction = { label: string; href?: string; onClick?: () => void };

/**
 * 섹션 = 제목 + 개수나 상태(meta) + 동작 하나(글자 링크).
 * description 은 사용자가 따라야 할 안내가 있을 때만 쓴다. 제목을 다시 말하는 부제로 쓰지 않는다.
 */
export function Section({
  id,
  title,
  meta,
  description,
  action,
  children,
}: {
  id?: string;
  title: string;
  meta?: ReactNode;
  description?: ReactNode;
  action?: SectionAction;
  children: ReactNode;
}) {
  return (
    <section className="pk-section" id={id}>
      <header className="pk-section-head">
        <h2>{title}</h2>
        {meta !== undefined && <span className="pk-section-meta">{meta}</span>}
        {action &&
          (action.href ? (
            <a className="pk-action" href={action.href}>
              {action.label}
            </a>
          ) : (
            <button
              type="button"
              className="pk-action"
              onClick={action.onClick}
            >
              {action.label}
            </button>
          ))}
      </header>
      {description && <p className="pk-section-help">{description}</p>}
      <div className="pk-section-body">{children}</div>
    </section>
  );
}

/** 상태는 점 + 1~2단어 글자. 배경을 채우지 않는다. */
export function StatusLabel({
  tone,
  children,
}: {
  tone: Tone | "none";
  children: ReactNode;
}) {
  return <span className={`pk-label tone-${tone}`}>{children}</span>;
}

/** 숫자 요약 한 줄. 큰 숫자 카드 대신 쓴다. 문제가 있는 항목만 tone 으로 색을 준다. */
export function SummaryRow({
  items,
}: {
  items: {
    label: string;
    value: string;
    unit?: string;
    tone?: Tone;
    note?: ReactNode;
  }[];
}) {
  return (
    <dl className="pk-summary">
      {items.map((item) => (
        <div
          key={item.label}
          className={item.tone ? `tone-${item.tone}` : undefined}
        >
          <dt>{item.label}</dt>
          <dd>
            <span className="pk-summary-value">{item.value}</span>
            {item.unit && <small>{item.unit}</small>}
            {item.note && <span className="pk-summary-note">{item.note}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
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
