import type { ReactNode } from "react";

/**
 * 대시보드 위젯(개요 화면 전용, Wiz 대시보드 방식): 흰 면 + 머리(제목 14 굵게 · 보조 줄 · 글자 링크) + 구분선 + 내용.
 * 개요 탭처럼 여러 판단을 한 화면에 모을 때만 쓴다. 목록 화면은 Section.
 */
export function Widget({
  title,
  sub,
  action,
  className,
  children,
}: {
  title: string;
  sub?: ReactNode;
  action?: { label: string; onClick: () => void };
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`pk-widget${className ? ` ${className}` : ""}`}>
      <header className="pk-widget-head">
        <div>
          <h2>{title}</h2>
          {sub && <p>{sub}</p>}
        </div>
        {action && (
          <button type="button" className="pk-action" onClick={action.onClick}>
            {action.label}
          </button>
        )}
      </header>
      <div className="pk-widget-body">{children}</div>
    </section>
  );
}
