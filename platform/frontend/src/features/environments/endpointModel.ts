// 접속 주소 모델: 구성 요소가 노출한 주소(인그레스, 로드 밸런서, 노드 포트)를 정렬하고 열 수 있는지 판단한다.
import type {
  ComponentEndpoint,
  EndpointSource,
  EnvComponent,
} from "../../api/migrations.ts";

export const SOURCE_LABELS: Record<EndpointSource, string> = {
  ingress: "인그레스",
  load_balancer: "로드 밸런서",
  node_port: "노드 포트",
  cluster_ip: "클러스터 내부",
};

const SOURCE_ORDER: EndpointSource[] = [
  "ingress",
  "load_balancer",
  "node_port",
  "cluster_ip",
];

/** 링크로 열 수 있는 주소: http(s) 만, 클러스터 내부 주소는 복사만. */
export function canOpen(endpoint: ComponentEndpoint) {
  return endpoint.source !== "cluster_ip" && /^https?:\/\//i.test(endpoint.url);
}

export function isEncrypted(endpoint: ComponentEndpoint) {
  return /^https:\/\//i.test(endpoint.url);
}

/** 공개 주소인데 HTTP 면 경고. */
export function endpointWarning(endpoint: ComponentEndpoint) {
  if (endpoint.exposure === "public" && /^http:\/\//i.test(endpoint.url))
    return "암호화 안 됨(HTTP)";
  return null;
}

/** 공개 → 내부망, HTTPS → HTTP, 인그레스 → 로드 밸런서 → 노드 포트 → 클러스터 내부. */
export function sortedEndpoints(component: EnvComponent) {
  return [...(component.endpoints ?? [])].sort(
    (a, b) =>
      (a.exposure === "public" ? 0 : 1) - (b.exposure === "public" ? 0 : 1) ||
      Number(isEncrypted(b)) - Number(isEncrypted(a)) ||
      SOURCE_ORDER.indexOf(a.source) - SOURCE_ORDER.indexOf(b.source),
  );
}

/** 표와 패널 머리의 "열기" 버튼이 여는 주소. */
export function primaryEndpoint(component: EnvComponent) {
  return sortedEndpoints(component).find(canOpen) ?? null;
}

/** 화면에 보일 주소: 끝 슬래시를 뗀다. */
export function displayUrl(url: string) {
  return url.replace(/\/$/, "");
}
