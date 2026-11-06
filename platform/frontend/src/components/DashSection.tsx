import type { ReactNode } from "react";

/**
 * 상세 화면 섹션. Section(PageKit)과 같은 모양(pk-section)을 쓴다.
 * 길어서 기본으로 접어 둘 내용(예: 단계 기록)만 defaultOpen={false} 로 접을 수 있게 한다.
 */
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
  if (defaultOpen)
    return (
      <section className="pk-section">
        <header className="pk-section-head">
          <h2>{title}</h2>
          {aside && <span className="pk-section-meta">{aside}</span>}
        </header>
        <div className="pk-section-body">{children}</div>
      </section>
    );
  return (
    <details className="pk-section pk-collapsible">
      <summary className="pk-section-head">
        <span className="pk-chevron" aria-hidden="true" />
        <h2>{title}</h2>
        {aside && <span className="pk-section-meta">{aside}</span>}
      </summary>
      <div className="pk-section-body">{children}</div>
    </details>
  );
}
