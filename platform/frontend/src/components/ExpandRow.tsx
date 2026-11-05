import type { KeyboardEvent, ReactNode } from "react";

export function ExpandToggle({
  open,
  label,
  onToggle,
}: {
  open: boolean;
  label: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className="expand-toggle"
      aria-expanded={open}
      aria-label={`${label} 자세히 보기`}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
    >
      <span className="dash-chevron" aria-hidden="true" />
    </button>
  );
}

export function expandableRow(open: boolean, onToggle: () => void) {
  return {
    className: open ? "clickable expanded" : "clickable",
    onClick: onToggle,
    onKeyDown: (event: KeyboardEvent) => {
      if (event.target !== event.currentTarget) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onToggle();
      }
    },
    tabIndex: 0,
  };
}

export function ExpandedBody({
  colSpan,
  children,
}: {
  colSpan: number;
  children: ReactNode;
}) {
  return (
    <tr className="expand-row">
      <td colSpan={colSpan}>
        <div className="expand-body">{children}</div>
      </td>
    </tr>
  );
}
