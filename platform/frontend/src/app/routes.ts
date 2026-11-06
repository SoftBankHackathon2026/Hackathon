export type Route =
  | { view: "migrations" }
  | { view: "migration"; id: string }
  | { view: "prepare" }
  | {
      view: "environments";
      select?: { envId: string; id: string };
      /** 노드 보기. envId 가 있으면 그 클러스터 패널, node 까지 있으면 노드 패널을 연다. */
      nodes?: { envId?: string; node?: string };
    }
  | { view: "security"; control?: string }
  | { view: "costs"; tab?: string }
  | { view: "team" };

export function readRoute(hash = "#"): Route {
  const [head, id, extra, more] = hash.replace(/^#/, "").split("/");
  if (head === "prepare") return { view: "prepare" };
  if (head === "migrations" && id)
    return { view: "migration", id: decodeURIComponent(id) };
  if (head === "environments") {
    if (id === "nodes")
      return {
        view: "environments",
        nodes: {
          ...(extra ? { envId: decodeURIComponent(extra) } : {}),
          ...(extra && more ? { node: decodeURIComponent(more) } : {}),
        },
      };
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
  if (head === "costs")
    return id
      ? { view: "costs", tab: decodeURIComponent(id) }
      : { view: "costs" };
  if (head === "team") return { view: "team" };
  return { view: "migrations" };
}

/** 주소만 바꾸고(새 기록 없음) 화면 라우터에도 알린다. replaceState 는 hashchange 를 내지 않는다. */
export const ROUTE_EVENT = "oneport:route";
export function replaceHash(hash: string) {
  if (window.location.hash === hash) return;
  window.history.replaceState(null, "", hash);
  window.dispatchEvent(new Event(ROUTE_EVENT));
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

/** 노드 보기(클러스터/노드 패널) 주소 */
export function nodesHash(envId?: string, node?: string) {
  return [
    "#environments/nodes",
    ...(envId ? [encodeURIComponent(envId)] : []),
    ...(envId && node ? [encodeURIComponent(node)] : []),
  ].join("/");
}

export function routeTitle(route: Route, teamName: string) {
  switch (route.view) {
    case "migration":
      return route.id;
    case "prepare":
      return "새 이전 준비";
    case "environments":
      return "환경";
    case "security":
      return "보안";
    case "costs":
      return "비용";
    case "team":
      return `${teamName} 팀`;
    default:
      return "마이그레이션";
  }
}
