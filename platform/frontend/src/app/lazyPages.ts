import { lazy } from "react";

export const FleetPage = lazy(() =>
  import("../features/migrations/FleetPage").then((m) => ({
    default: m.FleetPage,
  })),
);
export const MigrationDetailPage = lazy(() =>
  import("../features/migrations/MigrationDetailPage").then((m) => ({
    default: m.MigrationDetailPage,
  })),
);
export const EnvironmentsPage = lazy(() =>
  import("../features/environments/EnvironmentsPage").then((m) => ({
    default: m.EnvironmentsPage,
  })),
);
export const SecurityPage = lazy(() =>
  import("../features/security/SecurityPage").then((m) => ({
    default: m.SecurityPage,
  })),
);
export const TeamPage = lazy(() =>
  import("../features/team/TeamPage").then((m) => ({ default: m.TeamPage })),
);
