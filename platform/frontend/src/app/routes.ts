export type Route =
  | { view: "migrations" }
  | { view: "migration"; id: string }
  | { view: "agent"; runId?: string }
  | { view: "prepare"; requestId?: string; newConnection?: true }
  | {
      view: "environments";
      select?: { envId: string; id: string };
      /** 노드 보기. envId 가 있으면 그 클러스터 패널, node 까지 있으면 노드 패널을 연다. */
      nodes?: { envId?: string; node?: string };
    }
  | { view: "security"; control?: string }
  | { view: "costs"; tab?: string }
  | { view: "team" };

function decode(part: string) {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

export function readRoute(hash = "#"): Route {
  const [head, id, extra, more] = hash.replace(/^#/, "").split("/");
  if (head === "prepare") {
    if (!id) return { view: "prepare" };
    if (id === "new") return { view: "prepare", newConnection: true };
    try {
      return { view: "prepare", requestId: decodeURIComponent(id) };
    } catch {
      return { view: "prepare", requestId: "invalid-request" };
    }
  }
  if (head === "migrations" && id) return { view: "migration", id: decode(id) };
  if (head === "agent")
    return id ? { view: "agent", runId: decode(id) } : { view: "agent" };
  if (head === "environments") {
    if (id === "nodes")
      return {
        view: "environments",
        nodes: {
          ...(extra ? { envId: decode(extra) } : {}),
          ...(extra && more ? { node: decode(more) } : {}),
        },
      };
    if (id && extra)
      return {
        view: "environments",
        select: {
          envId: decode(id),
          id: decode(extra),
        },
      };
    return { view: "environments" };
  }
  if (head === "security")
    return id
      ? { view: "security", control: decode(id) }
      : { view: "security" };
  if (head === "costs")
    return id ? { view: "costs", tab: decode(id) } : { view: "costs" };
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

export const CLOSE_PANELS_EVENT = "oneport:close-panels";

export function onRouteChange(target: EventTarget, listener: () => void) {
  target.addEventListener("hashchange", listener);
  target.addEventListener(ROUTE_EVENT, listener);
  return () => {
    target.removeEventListener("hashchange", listener);
    target.removeEventListener(ROUTE_EVENT, listener);
  };
}

export function migrationHash(jobId: string) {
  return `#migrations/${encodeURIComponent(jobId)}`;
}

export function agentHash(runId?: string) {
  return runId ? `#agent/${encodeURIComponent(runId)}` : "#agent";
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
      return "마이그레이션 준비";
    case "agent":
      return "AI 에이전트";
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
