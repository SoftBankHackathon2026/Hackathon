import { useEffect } from "react";
import type { Access } from "../api/types";
import { AppShell, type View } from "../components/AppShell";
import { PageBoundary } from "../components/QueryBoundary";
import { AccountControls } from "./AccountControls";
import {
  EnvironmentsPage,
  FleetPage,
  MigrationDetailPage,
  PreparePage,
  SecurityPage,
  TeamPage,
} from "./lazyPages";
import { routeTitle } from "./routes";
import { useRoute } from "./useRoute";

export function Workspace({
  access,
  onSignOut,
}: {
  access: Access;
  onSignOut: () => void;
}) {
  const { route, go } = useRoute();
  const title = routeTitle(route, access.user?.team ?? "");
  useEffect(() => {
    document.title = `${title} - Oneport`;
  }, [title]);
  const nav: View =
    route.view === "migration" || route.view === "prepare"
      ? "migrations"
      : route.view;
  return (
    <AppShell
      view={nav}
      title={title}
      crumb={
        route.view === "migration"
          ? { label: "마이그레이션", onClick: () => go("migrations") }
          : undefined
      }
      onNavigate={(v) => go(v)}
      account={<AccountControls access={access} onSignOut={onSignOut} />}
      showTeam={Boolean(access.user)}
      hideTitle={
        route.view === "migrations" ||
        route.view === "prepare" ||
        route.view === "environments" ||
        route.view === "security"
      }
    >
      <PageBoundary resetKeys={[route.view, "id" in route ? route.id : ""]}>
        {route.view === "migrations" && <FleetPage onNavigate={go} />}
        {route.view === "prepare" && (
          <PreparePage
            key={access.user?.team ?? "shared"}
            onNavigate={go}
            requestId={route.requestId}
            team={access.user?.team}
          />
        )}
        {route.view === "migration" && (
          <MigrationDetailPage key={route.id} jobId={route.id} />
        )}
        {route.view === "environments" && (
          <EnvironmentsPage select={route.select} />
        )}
        {route.view === "security" && <SecurityPage control={route.control} />}
        {route.view === "team" &&
          (access.user ? (
            <TeamPage account={access.user} />
          ) : (
            <p className="empty">로그인 모드에서만 팀을 관리할 수 있어요.</p>
          ))}
      </PageBoundary>
    </AppShell>
  );
}
