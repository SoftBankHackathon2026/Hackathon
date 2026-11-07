import { useState, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";
import { SlotContext } from "./PageSlots";

export type View =
  "migrations" | "agent" | "environments" | "security" | "costs" | "team";
export const views: { id: View; title: string; icon: IconName }[] = [
  { id: "migrations", title: "마이그레이션", icon: "swap" },
  { id: "agent", title: "AI 에이전트", icon: "bot" },
  { id: "environments", title: "환경", icon: "layers" },
  { id: "security", title: "보안", icon: "shield" },
  { id: "costs", title: "비용", icon: "coin" },
  { id: "team", title: "팀", icon: "users" },
];

export function AppShell({
  view,
  title,
  crumb,
  onNavigate,
  account,
  showTeam = false,
  hideTitle = false,
  children,
}: {
  view: View;
  title: string;
  crumb?: { label: string; onClick: () => void };
  onNavigate: (view: View) => void;
  account: ReactNode;
  showTeam?: boolean;
  hideTitle?: boolean;
  children: ReactNode;
}) {
  const [tabs, setTabs] = useState<HTMLElement | null>(null);
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <div className="shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        본문으로 이동
      </a>
      <aside className="rail">
        <a
          className="rail-brand"
          href="#migrations"
          aria-label="Oneport 마이그레이션"
        >
          <Icon name="rocket" size={20} />
        </a>
        <nav aria-label="메뉴">
          {views
            .filter((item) => item.id !== "team" || showTeam)
            .map((item) => (
              <a
                key={item.id}
                href={`#${item.id}`}
                className={`rail-item ${view === item.id ? "on" : ""}`}
                aria-current={view === item.id ? "page" : undefined}
                aria-label={item.title}
                data-tip={item.title}
                onClick={() => onNavigate(item.id)}
              >
                <Icon name={item.icon} size={20} />
              </a>
            ))}
        </nav>
        <nav className="rail-bottom" aria-label="참고 자료">
          <a
            className="rail-item"
            href="/docs"
            target="_blank"
            rel="noreferrer"
            aria-label="API 문서"
            data-tip="API"
          >
            <Icon name="code" size={20} />
          </a>
          <a
            className="rail-item"
            href="https://github.com/SoftBankHackathon2026/Hackathon/blob/main/platform/README.md"
            target="_blank"
            rel="noreferrer"
            aria-label="문서"
            data-tip="문서"
          >
            <Icon name="book" size={20} />
          </a>
        </nav>
      </aside>
      <div className="stage">
        <header className="page-head">
          <div className="head-main">
            {!hideTitle && (
              <nav className="page-where" aria-label="현재 위치">
                {crumb && (
                  <>
                    <button className="page-crumb" onClick={crumb.onClick}>
                      {crumb.label}
                    </button>
                    <span className="page-sep" aria-hidden="true">
                      /
                    </span>
                  </>
                )}
                <span aria-current="page">{title}</span>
              </nav>
            )}
            <div className="page-tabs" ref={setTabs} />
          </div>
          <div className="head-tools">
            <div className="page-actions" ref={setActions} />
            <div className="page-account">{account}</div>
          </div>
        </header>
        <SlotContext.Provider value={{ tabs, actions }}>
          <main id="main-content" tabIndex={-1}>
            {children}
          </main>
        </SlotContext.Provider>
      </div>
    </div>
  );
}
