// 보안 문제 영향 경로(Wiz 공격 경로 방식): 인터넷 → 진입점 → 워크로드 → 데이터.
// 환경 토폴로지(구성 요소의 depends_on)와 통제 결과의 리소스 이름으로 만든다.
import type { EnvComponent } from "../../api/migrations.ts";

export type PathNode = {
  id: string;
  name: string;
  kind: string;
  layer: string;
  /** 실패한 리소스인지. */
  hit: boolean;
};
export type PathColumn = {
  key: "internet" | "entry" | "workload" | "data";
  title: string;
  nodes: PathNode[];
};

const WORKLOAD_LAYERS = ["app", "migration", "platform"];

export function impactPath(components: EnvComponent[], resources: string[]) {
  const byId = new Map(components.map((c) => [c.id, c]));
  const hits = components.filter((c) => resources.includes(c.name));
  const hitIds = new Set(hits.map((c) => c.id));
  const node = (c: EnvComponent): PathNode => ({
    id: c.id,
    name: c.name,
    kind: c.kind,
    layer: c.layer,
    hit: hitIds.has(c.id),
  });
  const uniq = (list: EnvComponent[]) => [
    ...new Map(list.map((c) => [c.id, c])).values(),
  ];

  const hitEntries = hits.filter((c) => c.layer === "entry");
  const hitWorkloads = hits.filter((c) => WORKLOAD_LAYERS.includes(c.layer));
  const hitData = hits.filter((c) => c.layer === "data");
  // 문제 워크로드가 기대는 진입점, 문제 진입점에 기대는 워크로드
  const entries = uniq([
    ...hitEntries,
    ...hitWorkloads.flatMap((w) =>
      w.depends_on
        .map((id) => byId.get(id))
        .filter((c): c is EnvComponent => c?.layer === "entry"),
    ),
  ]);
  const workloads = uniq([
    ...hitWorkloads,
    ...components.filter(
      (c) =>
        c.layer === "app" &&
        c.depends_on.some((id) => hitEntries.some((e) => e.id === id)),
    ),
  ]);
  const data = uniq([
    ...hitData,
    ...workloads.flatMap((w) =>
      w.depends_on
        .map((id) => byId.get(id))
        .filter((c): c is EnvComponent => c?.layer === "data"),
    ),
  ]);
  const columns: PathColumn[] = [];
  if (entries.length > 0)
    columns.push({
      key: "internet",
      title: "인터넷",
      nodes: [
        {
          id: "internet",
          name: "인터넷",
          kind: "internet",
          layer: "internet",
          hit: false,
        },
      ],
    });
  if (entries.length > 0)
    columns.push({ key: "entry", title: "진입점", nodes: entries.map(node) });
  if (workloads.length > 0)
    columns.push({
      key: "workload",
      title: "워크로드",
      nodes: workloads.map(node),
    });
  if (data.length > 0)
    columns.push({ key: "data", title: "데이터", nodes: data.map(node) });

  const edges: { from: string; to: string }[] = [];
  for (const e of entries) edges.push({ from: "internet", to: e.id });
  for (const w of workloads) {
    const linked = entries.filter((e) => w.depends_on.includes(e.id));
    // 진입점과 직접 연결이 없는 문제 워크로드는 같은 클러스터의 진입점 뒤에 둔다
    for (const e of linked.length > 0 ? linked : entries)
      edges.push({ from: e.id, to: w.id });
    for (const id of w.depends_on)
      if (data.some((d) => d.id === id)) edges.push({ from: w.id, to: id });
  }
  return { columns, edges };
}
