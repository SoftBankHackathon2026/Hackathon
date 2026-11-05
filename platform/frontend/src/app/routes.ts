export type Route =
  | { view: "migrations" }
  | { view: "migration"; id: string }
  | { view: "environments"; select?: { envId: string; id: string } }
  | { view: "security"; control?: string }
  | { view: "team" };

export function readRoute(hash = "#"): Route {
  const [head, id, extra] = hash.replace(/^#/, "").split("/");
  if (head === "migrations" && id)
    return { view: "migration", id: decodeURIComponent(id) };
  if (head === "environments") {
    if (id && extra)
      return {
        view: "environments",
        select: {
          envId: decodeURIComponent(id),
          id: decodeURIComponent(extra),
        },
      };
    return { view: "environments" };
  }
  if (head === "security")
    return id
      ? { view: "security", control: decodeURIComponent(id) }
      : { view: "security" };
  if (head === "team") return { view: "team" };
  return { view: "migrations" };
}

export function migrationHash(jobId: string) {
  return `#migrations/${encodeURIComponent(jobId)}`;
}

export function controlHash(controlId: string) {
  return `#security/${encodeURIComponent(controlId)}`;
}

export function resourceHash(envId: string, componentId: string) {
  return `#environments/${encodeURIComponent(envId)}/${encodeURIComponent(componentId)}`;
}

export function routeTitle(route: Route, teamName: string) {
  switch (route.view) {
    case "migration":
      return route.id;
    case "environments":
      return "환경";
    case "security":
      return "보안";
    case "team":
      return `${teamName} 팀`;
    default:
      return "마이그레이션";
  }
}
