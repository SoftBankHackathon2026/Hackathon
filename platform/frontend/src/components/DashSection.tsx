import type { ReactNode } from "react";

export function DashSection({
  title,
  aside,
  defaultOpen = true,
  children,
}: {
  title: string;
  aside?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="dash-section" open={defaultOpen}>
      <summary>
        <span className="dash-chevron" aria-hidden="true" />
        <h2>{title}</h2>
        {aside && <span className="dash-aside">{aside}</span>}
      </summary>
      <div className="dash-body">{children}</div>
    </details>
  );
}
