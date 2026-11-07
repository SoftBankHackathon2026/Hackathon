import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * 오른쪽 상세 패널(모든 목록 공통, Datadog 사이드 패널 방식).
 * 표 위에 열리고 다른 행을 누르면 내용만 바뀐다. ↑↓ 이전·다음, Esc·✕·바깥 누르기로 닫기.
 * 목록(data-panel-owner 가 붙은 영역) 안을 누르면 닫지 않고 그 행으로 바뀐다.
 * 섹션의 container-type 이 고정 위치를 가두지 않게 앱 최상위(.shell)로 꺼내 그린다.
 * 본문은 PanelGroup(왼쪽 라벨 + 오른쪽 내용) 묶음으로 채운다.
 */
export function SidePanel({
  kind,
  id,
  title,
  facts,
  position,
  onMove,
  onClose,
  onBack,
  wide,
  actions,
  children,
}: {
  kind: string;
  id?: string;
  title: ReactNode;
  facts?: { label: string; value: ReactNode; tone?: "bad" }[];
  position?: { index: number; total: number };
  onMove?: (step: 1 | -1) => void;
  onClose: () => void;
  onBack?: () => void;
  wide?: boolean;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, select, textarea")) return;
      if (event.key === "Escape") {
        if (onBack) {
          event.stopPropagation();
          onBack();
        } else onClose();
      } else if (onMove && (event.key === "ArrowDown" || event.key === "j")) {
        event.preventDefault();
        onMove(1);
      } else if (onMove && (event.key === "ArrowUp" || event.key === "k")) {
        event.preventDefault();
        onMove(-1);
      }
    };
    const capture = Boolean(onBack);
    window.addEventListener("keydown", onKey, { capture });
    return () => window.removeEventListener("keydown", onKey, { capture });
  }, [onClose, onBack, onMove]);
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [id]);
  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target || target.closest(".fp, [data-panel-owner]")) return;
      onClose();
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [onClose]);
  const panel = (
    <aside
      className={wide ? "fp fp-wide" : "fp"}
      role="dialog"
      aria-modal="false"
      aria-labelledby="fp-title"
    >
      <header className="fp-head">
        <div className="fp-crumb">
          {onBack && (
            <button
              type="button"
              className="fp-back"
              aria-label="뒤로"
              title="뒤로 (Esc)"
              onClick={onBack}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M10 4l-4 4 4 4" />
              </svg>
            </button>
          )}
          <span>{kind}</span>
          {id && <span className="fp-id">{id}</span>}
          {position && (
            <span className="fp-pos">
              {position.index + 1} / {position.total}
            </span>
          )}
          <span className="fp-nav">
            {onMove && (
              <>
                <button
                  type="button"
                  aria-label="이전"
                  title="이전 (↑)"
                  onClick={() => onMove(-1)}
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path d="M4 10l4-4 4 4" />
                  </svg>
                </button>
                <button
                  type="button"
                  aria-label="다음"
                  title="다음 (↓)"
                  onClick={() => onMove(1)}
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path d="M4 6l4 4 4-4" />
                  </svg>
                </button>
              </>
            )}
            <button
              type="button"
              aria-label="닫기"
              title="닫기 (Esc)"
              onClick={onClose}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M4 4l8 8M12 4l-8 8" />
              </svg>
            </button>
          </span>
        </div>
        <h2 id="fp-title" ref={headingRef} tabIndex={-1}>
          {title}
        </h2>
        {facts && (
          <dl className="fp-facts">
            {facts.map((fact) => (
              <div key={fact.label}>
                <dt>{fact.label}</dt>
                <dd className={fact.tone === "bad" ? "text-bad" : undefined}>
                  {fact.value}
                </dd>
              </div>
            ))}
          </dl>
        )}
        {actions && <div className="fp-actions">{actions}</div>}
      </header>
      <div className="fp-body">{children}</div>
      <footer className="fp-foot">
        {onMove && (
          <>
            <kbd>↑</kbd>
            <kbd>↓</kbd> 이전/다음
          </>
        )}
        <kbd>Esc</kbd> {onBack ? "뒤로" : "닫기"}
      </footer>
    </aside>
  );
  return createPortal(panel, document.querySelector(".shell") ?? document.body);
}

export function PanelGroup({
  label,
  wide,
  children,
}: {
  label: string;
  /** 그림처럼 넓은 내용은 라벨을 위에 두고 패널 너비를 다 쓴다. */
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={`fp-group${wide ? " wide" : ""}`}>
      <h3>{label}</h3>
      <div className="fp-group-body">{children}</div>
    </section>
  );
}
