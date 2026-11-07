import { useEffect, useId, useState } from "react";
import "./live-app-select.css";
import type { ConnectionSnapshot } from "./connectionTypes";
import {
  classifyWorkload,
  type Workload,
  type WorkloadGroup,
} from "./workloadGroups";

const groups = [
  { id: "app", label: "앱", description: "옮길 후보로 분류된 워크로드예요." },
  {
    id: "data",
    label: "데이터·미들웨어",
    description: "앱이 쓰는 데이터베이스, 큐, 저장소예요.",
  },
  {
    id: "migration",
    label: "이전 도구",
    description: "플랫폼이 이전에 사용하는 도구예요.",
  },
  {
    id: "infrastructure",
    label: "인프라·시스템",
    description: "클러스터 운영에 쓰는 구성 요소예요.",
  },
] as const;

const collectedDate = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  dateStyle: "medium",
  timeStyle: "short",
  hourCycle: "h23",
});

const workloadId = ({ namespace, kind, name }: Workload) =>
  `${namespace}/${kind}/${name}`;

/** Live inventory only. Never substitute preview workloads or migration verdicts. */
export function LiveAppSelect({
  snapshot,
  busy,
  onRefresh,
  onSelect,
  initialSelection = [],
}: {
  snapshot: ConnectionSnapshot;
  busy: boolean;
  onRefresh: () => void;
  onSelect?: (apps: string[]) => void;
  initialSelection?: string[];
}) {
  const [selection, setSelection] = useState<{
    inventoryDigest?: string;
    ids: string[];
  }>({ inventoryDigest: snapshot.inventoryDigest, ids: initialSelection });
  if (selection.inventoryDigest !== snapshot.inventoryDigest) {
    setSelection({ inventoryDigest: snapshot.inventoryDigest, ids: [] });
  }
  const selected = selection.ids;
  const [query, setQuery] = useState("");
  const [namespace, setNamespace] = useState("");
  const [open, setOpen] = useState<Record<WorkloadGroup, boolean>>({
    app: true,
    data: false,
    migration: false,
    infrastructure: false,
  });
  const panelId = useId();
  useEffect(() => onSelect?.(selected), [selected, onSelect]);
  const inventory = snapshot.workloads;
  const pending = busy || snapshot.collectionPending;
  const ready = inventory?.coverage === "complete";
  const items = ready ? inventory.items : [];
  const namespacesByName = new Map<string, Set<string>>();
  const grouped: Record<WorkloadGroup, Workload[]> = {
    app: [],
    data: [],
    migration: [],
    infrastructure: [],
  };
  for (const item of items) {
    grouped[classifyWorkload(item)].push(item);
    const namespaces = namespacesByName.get(item.name) ?? new Set<string>();
    namespaces.add(item.namespace);
    namespacesByName.set(item.name, namespaces);
  }
  grouped.app.sort(
    (a, b) => Number(a.replicas === 0) - Number(b.replicas === 0),
  );
  const namespaces = [...new Set(items.map((item) => item.namespace))].sort();
  const activeNamespace = namespaces.includes(namespace) ? namespace : "";
  const term = query.trim().toLowerCase();
  const filtering = Boolean(term || activeNamespace);
  const results = groups.map((group) => ({
    ...group,
    total: grouped[group.id].length,
    matches: grouped[group.id].filter(
      (item) =>
        (!activeNamespace || item.namespace === activeNamespace) &&
        (!term ||
          [item.name, item.namespace, ...item.images].some((value) =>
            value.toLowerCase().includes(term),
          )),
    ),
  }));
  const visibleCount = results.reduce(
    (sum, group) => sum + (open[group.id] ? group.matches.length : 0),
    0,
  );
  const hiddenCount = results.reduce(
    (sum, group) => sum + (open[group.id] ? 0 : group.matches.length),
    0,
  );
  const toggleGroup = (group: WorkloadGroup) =>
    setOpen((current) => ({ ...current, [group]: !current[group] }));
  return (
    <section className="pk-section live-apps" aria-labelledby="live-apps-title">
      <header className="live-apps-head">
        <div className="live-apps-heading">
          <h2 id="live-apps-title">옮길 앱 고르기</h2>
          <p>
            {snapshot.target?.name} 환경
            {ready && !pending ? (
              <>
                {" · "}
                {items.length}개 수집
                {snapshot.collectedAt !== undefined && (
                  <>
                    {" · "}
                    <time
                      dateTime={new Date(snapshot.collectedAt).toISOString()}
                    >
                      {collectedDate.format(snapshot.collectedAt)}
                    </time>{" "}
                    (한국시간)
                  </>
                )}
              </>
            ) : (
              "의 앱을 선택하세요."
            )}
          </p>
        </div>
        <button
          className="secondary"
          onClick={onRefresh}
          disabled={
            pending ||
            snapshot.tunnel !== "READY" ||
            !snapshot.permissions.registerEnvironment
          }
        >
          {pending ? "앱 목록 수집 중…" : "앱 목록 다시 수집"}
        </button>
      </header>
      {pending || !ready || inventory.items.length === 0 ? (
        <div className="live-apps-empty" role="status" aria-live="polite">
          <span className="live-apps-symbol" aria-hidden="true">
            ▦
          </span>
          <h3>
            {pending
              ? "앱 목록을 수집하고 있어요"
              : !inventory
                ? "앱 목록을 먼저 수집해 주세요"
                : !ready
                  ? "앱 목록을 가져오지 못했어요"
                  : "아직 표시할 앱이 없어요"}
          </h3>
          <p>
            {pending
              ? "연결 단말에서 앱 정보를 읽고 있어요. 결과가 도착하면 목록이 표시됩니다."
              : !inventory
                ? "이전 수집 결과에는 앱 목록이 없어요. 연결 도구를 업데이트한 뒤 ‘앱 목록 다시 수집’을 눌러 주세요."
                : !ready
                  ? "연결 단말의 워크로드 조회 권한을 확인한 뒤 다시 시도해 주세요."
                  : "이 환경의 수집 범위에서 앱을 찾지 못했어요. 연결한 클러스터와 조회 권한을 확인해 주세요."}
          </p>
        </div>
      ) : (
        <>
          <div className="live-apps-filters">
            <label>
              <span>앱 검색</span>
              <input
                type="search"
                placeholder="이름, 네임스페이스, 이미지"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <label>
              <span>네임스페이스</span>
              <select
                value={activeNamespace}
                onChange={(event) => setNamespace(event.target.value)}
              >
                <option value="">전체</option>
                {namespaces.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label className="live-apps-infra-filter">
              <input
                type="checkbox"
                checked={open.infrastructure}
                onChange={() => toggleGroup("infrastructure")}
              />
              인프라도 보기
            </label>
          </div>
          {visibleCount > 0 && (
            <p className="live-apps-scroll-hint">
              표를 옆으로 밀면 이미지와 설정 참조를 볼 수 있어요.
            </p>
          )}
          <div className="live-apps-results" role="status">
            {visibleCount === 0 &&
              (hiddenCount > 0
                ? `접힌 그룹에 ${hiddenCount}개 있어요. 그룹을 펼쳐 확인하세요.`
                : "검색·필터에 맞는 항목이 없어요.")}
          </div>
          {results.map((group) => (
            <section key={group.id} className="live-apps-group">
              <h3>
                <button
                  type="button"
                  className="live-apps-group-toggle"
                  aria-expanded={open[group.id]}
                  aria-controls={`${panelId}-${group.id}`}
                  aria-labelledby={`${panelId}-${group.id}-name ${panelId}-${group.id}-count`}
                  aria-describedby={`${panelId}-${group.id}-description`}
                  onClick={() => toggleGroup(group.id)}
                >
                  <span className="live-apps-chevron" aria-hidden="true" />
                  <span
                    className="live-apps-group-name"
                    id={`${panelId}-${group.id}-name`}
                  >
                    {group.label}
                  </span>
                  <span
                    className="live-apps-group-count"
                    id={`${panelId}-${group.id}-count`}
                  >
                    {filtering
                      ? `${group.matches.length} / ${group.total}개`
                      : `${group.total}개`}
                  </span>
                  <span
                    className="live-apps-group-description"
                    id={`${panelId}-${group.id}-description`}
                  >
                    {group.description}
                  </span>
                </button>
              </h3>
              <div id={`${panelId}-${group.id}`} hidden={!open[group.id]}>
                {open[group.id] &&
                  (group.matches.length ? (
                    <WorkloadTable
                      label={group.label}
                      items={group.matches}
                      selected={selected}
                      namespacesByName={namespacesByName}
                      warnOnSelection={
                        group.id === "migration" ||
                        group.id === "infrastructure"
                      }
                      onSelect={(id, checked) =>
                        setSelection((current) => ({
                          ...current,
                          ids: checked
                            ? [...current.ids, id]
                            : current.ids.filter((value) => value !== id),
                        }))
                      }
                    />
                  ) : (
                    <p className="live-apps-group-empty">
                      이 그룹에 표시할 항목이 없어요.
                    </p>
                  ))}
              </div>
            </section>
          ))}
        </>
      )}
      {ready && !pending && inventory.items.length > 0 && (
        <footer className="live-apps-footer">
          <strong role="status">선택한 앱 {selected.length}개</strong>
          <span>
            총 {inventory.items.length}개 · 작업을 만들어도 이전은 작업 화면에서
            시작해요.
          </span>
        </footer>
      )}
      <details className="live-apps-help">
        <summary>앱 수집 범위와 다음 단계</summary>
        <p>
          Deployment·StatefulSet·DaemonSet을 조회합니다.{" "}
          {inventory?.rolloutsAvailable
            ? "Argo Rollout도 수집했어요."
            : "Argo Rollout 수집 여부는 아직 확인되지 않았어요."}
        </p>
        <p>
          그룹은 앱을 찾기 위한 분류예요. 이미지 접근과 데이터 의존성, 실제 이전
          가능 여부는 별도 점검이 필요해요.
        </p>
      </details>
    </section>
  );
}

function WorkloadTable({
  label,
  items,
  selected,
  namespacesByName,
  warnOnSelection,
  onSelect,
}: {
  label: string;
  items: Workload[];
  selected: string[];
  namespacesByName: Map<string, Set<string>>;
  warnOnSelection: boolean;
  onSelect: (id: string, checked: boolean) => void;
}) {
  const tableId = useId();
  return (
    <div className="live-apps-table-wrap">
      <table className="pk-table" aria-label={`${label} 목록`}>
        <thead>
          <tr>
            <th scope="col">선택</th>
            <th scope="col">네임스페이스 / 앱</th>
            <th scope="col">종류 · 수</th>
            <th scope="col">이미지</th>
            <th scope="col">설정·저장소 참조</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item, index) => {
            const id = workloadId(item);
            const checked = selected.includes(id);
            const warning = checked && warnOnSelection;
            const warningId = `${tableId}-warning-${index}`;
            const duplicate = (namespacesByName.get(item.name)?.size ?? 0) > 1;
            return (
              <tr key={id}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`${id} 선택`}
                    aria-describedby={warning ? warningId : undefined}
                    checked={checked}
                    onChange={(event) => onSelect(id, event.target.checked)}
                  />
                </td>
                <td>
                  <span className="live-apps-workload-name">
                    {duplicate ? (
                      <strong>{item.namespace}</strong>
                    ) : (
                      item.namespace
                    )}
                    {" / "}
                    {item.name}
                  </span>
                  {warning && (
                    <p
                      className="live-apps-warning"
                      id={warningId}
                      role="status"
                    >
                      이 항목은 옮기는 대상이 아닐 수 있어요.
                    </p>
                  )}
                </td>
                <td>
                  {item.kind} · {item.replicas}
                  {item.replicas === 0 && (
                    <span className="live-apps-stopped">정지됨</span>
                  )}
                </td>
                <td>{item.images.join(", ")}</td>
                <td>
                  <WorkloadReferences item={item} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function WorkloadReferences({ item }: { item: Workload }) {
  const references = [
    { label: "Secret", values: item.secrets },
    { label: "ConfigMap", values: item.configMaps },
    { label: "PVC", values: item.pvcs },
  ].filter(({ values }) => values.length);
  if (!references.length) return "없음";
  return (
    <details className="live-apps-references">
      <summary>
        {references
          .map(({ label, values }) => `${label} ${values.length}개`)
          .join(" · ")}
      </summary>
      <dl>
        {references.map(({ label, values }) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{values.join(", ")}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
