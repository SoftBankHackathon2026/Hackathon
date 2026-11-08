import { Fragment, useMemo, useState } from "react";
import "./app-select.css";

/**
 * 마이그레이션 준비 2단계 앱 선택 (1차, 시연 데이터).
 *
 * 왼쪽: 판정별로 묶인 워크로드 표(체크 + 이름 + 한 줄 이유). 오른쪽: 고른 앱 하나의 상세와 선택 요약.
 * 데이터는 온프렘 k3s hackathon 네임스페이스(2026-11-06)를 손으로 옮긴 시연값이다.
 * 다음 단계에서 워크로드 분석기(workload_inventory.analyze) 결과를 API 로 받아 INVENTORY 를 대체한다.
 */

type Verdict = "data" | "move" | "hold" | "infra";
type DbMode = "cdc" | "snapshot" | "shared";

type Workload = {
  name: string;
  kind: "Rollout" | "Deployment" | "StatefulSet";
  image: string;
  replicas: number;
  cpu: string;
  memory: string;
  service?: string;
  secrets: string[];
  configMaps: string[];
  pvc?: string;
  database?: { name: string; table: string; via: string };
  inEcr: boolean;
  verdict: Verdict;
  reason: string;
  follows?: string[];
};

const VERDICT: Record<
  Verdict,
  { label: string; tone: "warn" | "ok" | "bad" | "idle"; help: string }
> = {
  data: {
    label: "데이터 포함",
    tone: "warn",
    help: "DB나 PVC가 같이 가야 해요",
  },
  move: { label: "옮김", tone: "ok", help: "이미지·설정만 옮기면 돼요" },
  hold: { label: "보류", tone: "bad", help: "지금은 옮길 수 없어요" },
  infra: {
    label: "플랫폼 인프라",
    tone: "idle",
    help: "앱이 아니라 안 옮겨요",
  },
};
const ORDER: Verdict[] = ["data", "move", "hold", "infra"];

const INVENTORY: Workload[] = [
  {
    name: "sample-app",
    kind: "Rollout",
    image: "docker.io/kty1021/sample-app:6ce6e5f",
    replicas: 2,
    cpu: "100m",
    memory: "128Mi",
    service: "sample-app:80",
    secrets: ["sample-app-db", "sample-app-db-ca"],
    configMaps: ["sample-app-control"],
    database: {
      name: "migration_demo_return",
      table: "public.migration_demo",
      via: "DATABASE_URL",
    },
    inEcr: true,
    verdict: "data",
    reason: "DATABASE_URL 로 PostgreSQL 에 써요",
    follows: [
      "DB migration_demo_return 한 테이블",
      "Secret sample-app-db, sample-app-db-ca",
      "traffic-bot 의 대상 주소",
    ],
  },
  {
    name: "traffic-bot",
    kind: "Deployment",
    image: "docker.io/kty1021/sample-app:6ce6e5f",
    replicas: 1,
    cpu: "50m",
    memory: "64Mi",
    secrets: [],
    configMaps: ["traffic-bot-target"],
    pvc: "traffic-bot-ledger 1Gi",
    inEcr: true,
    verdict: "data",
    reason: "PVC 에 요청 원장(ledger)을 보관해요",
    follows: ["PVC ledger 복사, 또는 새 run_id 로 시작"],
  },
  {
    name: "db-insert-worker",
    kind: "Deployment",
    image: "ghcr.io/softbankhackathon2026/migration-tools:67005b5",
    replicas: 1,
    cpu: "50m",
    memory: "64Mi",
    service: "db-insert-worker:8080",
    secrets: ["migration-db-credentials"],
    configMaps: ["migration-config"],
    pvc: "db-insert-worker-state 1Gi",
    database: {
      name: "migration_demo_return",
      table: "public.migration_demo",
      via: "설정 target",
    },
    inEcr: false,
    verdict: "hold",
    reason: "이전 도구 자체라 제어 클러스터에 둬요",
  },
  {
    name: "migration-policy",
    kind: "Deployment",
    image: "ghcr.io/softbankhackathon2026/migration-policy:main",
    replicas: 2,
    cpu: "50m",
    memory: "64Mi",
    service: "migration-policy:8080",
    secrets: [],
    configMaps: [],
    inEcr: false,
    verdict: "hold",
    reason: "이미지 태그가 main 이고 도착에서 못 받아요",
  },
  {
    name: "migration-connect",
    kind: "Deployment",
    image: "ghcr.io/softbankhackathon2026/migration-connect",
    replicas: 1,
    cpu: "250m",
    memory: "1Gi",
    service: "migration-connect:8083",
    secrets: ["migration-connect-secrets"],
    configMaps: [],
    inEcr: false,
    verdict: "infra",
    reason: "Kafka Connect, CDC 파이프라인",
  },
  {
    name: "openbao",
    kind: "StatefulSet",
    image: "quay.io/openbao/openbao:2.7.2",
    replicas: 1,
    cpu: "100m",
    memory: "256Mi",
    service: "openbao:8200",
    secrets: ["openbao-init"],
    configMaps: ["openbao-config"],
    pvc: "data-openbao-0 1Gi",
    inEcr: false,
    verdict: "infra",
    reason: "비밀 저장소, 도착은 Secrets Manager",
  },
  {
    name: "postgres",
    kind: "StatefulSet",
    image: "docker.io/library/postgres:16",
    replicas: 1,
    cpu: "500m",
    memory: "1Gi",
    service: "postgres:5432",
    secrets: ["review-db-credentials", "postgres-tls"],
    configMaps: [],
    pvc: "data-postgres-0 10Gi",
    inEcr: false,
    verdict: "infra",
    reason: "플랫폼 DB, 데이터만 CDC 로 옮겨요",
  },
];

const milli = (v: string) =>
  v.endsWith("m") ? Number(v.slice(0, -1)) : Number(v) * 1000;
const mi = (v: string) =>
  v.endsWith("Gi") ? Number(v.slice(0, -2)) * 1024 : Number(v.slice(0, -2));

export function AppSelectStep({
  sourceName,
  namespace,
  picked,
  onPick,
}: {
  sourceName: string;
  namespace: string;
  picked: string[];
  onPick: (apps: string[]) => void;
}) {
  const [focus, setFocus] = useState<string>("sample-app");
  const [dbMode, setDbMode] = useState<DbMode>("cdc");
  const [showInfra, setShowInfra] = useState(false);

  const chosen = INVENTORY.filter((w) => picked.includes(w.name));
  const detail = INVENTORY.find((w) => w.name === focus) ?? INVENTORY[0];
  const sum = useMemo(() => {
    const cpu = chosen.reduce((s, w) => s + milli(w.cpu) * w.replicas, 0);
    const mem = chosen.reduce((s, w) => s + mi(w.memory) * w.replicas, 0);
    const dbs = [
      ...new Set(chosen.map((w) => w.database?.name).filter(Boolean)),
    ] as string[];
    const secrets = [...new Set(chosen.flatMap((w) => w.secrets))];
    const pvcs = chosen.filter((w) => w.pvc).map((w) => w.name);
    const missing = chosen.filter((w) => !w.inEcr).map((w) => w.name);
    return { cpu, mem, dbs, secrets, pvcs, missing };
  }, [chosen]);

  function toggle(name: string) {
    onPick(
      picked.includes(name)
        ? picked.filter((app) => app !== name)
        : [...picked, name],
    );
    setFocus(name);
  }

  const groups = ORDER.filter((v) => showInfra || v !== "infra").map((v) => ({
    verdict: v,
    items: INVENTORY.filter((w) => w.verdict === v),
  }));

  return (
    <div className="appsel">
      <section className="pk-section appsel-list">
        <header className="pk-section-head">
          <h2>옮길 앱 고르기</h2>
          <span className="pk-section-meta">
            {sourceName} · {namespace} · {INVENTORY.length}개 읽음
          </span>
          <button
            type="button"
            className="pk-action"
            onClick={() => setShowInfra((v) => !v)}
          >
            {showInfra ? "인프라 숨기기" : "인프라 3개 보기"}
          </button>
        </header>
        <div className="appsel-body">
          <table className="pk-table appsel-table">
            <colgroup>
              <col className="appsel-col-check" />
              <col className="appsel-col-app" />
              <col className="appsel-col-verdict" />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th>
                  <span className="sr-only">선택</span>
                </th>
                <th>앱</th>
                <th>판정</th>
                <th>이유</th>
              </tr>
            </thead>
            <tbody>
              {groups.map(({ verdict, items }) =>
                items.length === 0 ? null : (
                  <Fragment key={verdict}>
                    <tr className="appsel-group">
                      <td colSpan={4}>
                        <span
                          className={`pk-label tone-${VERDICT[verdict].tone}`}
                        >
                          {VERDICT[verdict].label}
                        </span>
                        <span className="appsel-group-help">
                          {items.length}개 · {VERDICT[verdict].help}
                        </span>
                      </td>
                    </tr>
                    {items.map((w) => {
                      const on = picked.includes(w.name);
                      const locked = w.verdict === "infra";
                      return (
                        <tr
                          key={w.name}
                          className={`appsel-row${focus === w.name ? " is-focus" : ""}${on ? " is-on" : ""}${locked ? " is-locked" : ""}`}
                          onClick={() => setFocus(w.name)}
                        >
                          <td className="appsel-check">
                            <label
                              className="appsel-cb"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <input
                                type="checkbox"
                                checked={on}
                                disabled={locked}
                                onChange={() => toggle(w.name)}
                                aria-label={`${w.name} 선택`}
                              />
                              <span
                                className="appsel-cb-box"
                                aria-hidden="true"
                              />
                            </label>
                          </td>
                          <td className="appsel-app">
                            <span className="appsel-name">{w.name}</span>
                            <span className="appsel-kind">
                              {w.kind} ×{w.replicas}
                            </span>
                          </td>
                          <td>
                            <span
                              className={`pk-label tone-${VERDICT[w.verdict].tone}`}
                            >
                              {VERDICT[w.verdict].label}
                            </span>
                          </td>
                          <td className="appsel-reason">{w.reason}</td>
                        </tr>
                      );
                    })}
                  </Fragment>
                ),
              )}
            </tbody>
          </table>
        </div>
      </section>

      <aside className="appsel-side">
        <section className="pk-section appsel-detail">
          <header className="pk-section-head">
            <h2>{detail.name}</h2>
            <span className={`pk-label tone-${VERDICT[detail.verdict].tone}`}>
              {VERDICT[detail.verdict].label}
            </span>
          </header>
          <dl className="appsel-facts">
            <dt>이미지</dt>
            <dd>
              <code>{detail.image.replace(/^docker\.io\//, "")}</code>
              <span className={`pk-label tone-${detail.inEcr ? "ok" : "bad"}`}>
                {detail.inEcr ? "도착(ECR)에 있음" : "도착(ECR)에 없음"}
              </span>
            </dd>
            <dt>자원</dt>
            <dd>
              {detail.cpu} CPU · {detail.memory} · {detail.replicas}개
              {detail.service && <> · {detail.service}</>}
            </dd>
            {detail.database && (
              <>
                <dt>DB</dt>
                <dd>
                  {detail.database.name}{" "}
                  <span className="sub">
                    {detail.database.table}, {detail.database.via}
                  </span>
                </dd>
              </>
            )}
            {detail.pvc && (
              <>
                <dt>PVC</dt>
                <dd>{detail.pvc}</dd>
              </>
            )}
            {(detail.secrets.length > 0 || detail.configMaps.length > 0) && (
              <>
                <dt>참조</dt>
                <dd className="sub">
                  {detail.secrets
                    .map((s) => `Secret ${s}`)
                    .concat(detail.configMaps.map((c) => `ConfigMap ${c}`))
                    .join(" · ")}
                </dd>
              </>
            )}
            <dt>판정 이유</dt>
            <dd>{detail.reason}</dd>
          </dl>

          {picked.includes(detail.name) && detail.follows && (
            <div className="appsel-follow">
              <h3>같이 옮겨지는 것</h3>
              <ul>
                {detail.follows.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
              {detail.database && (
                <div
                  className="appsel-mode"
                  role="radiogroup"
                  aria-label="DB 이전 방식"
                >
                  {(
                    [
                      [
                        "cdc",
                        "CDC 복제",
                        "초기 복사 뒤 따라가기. 쓰기 정지 짧음",
                      ],
                      ["snapshot", "스냅샷만", "쓰기 멈추고 한 번 복사"],
                      ["shared", "공유 유지", "DB 는 안 옮김"],
                    ] as const
                  ).map(([v, label, help]) => (
                    <label key={v} className={dbMode === v ? "is-on" : ""}>
                      <input
                        type="radio"
                        name="db-mode"
                        checked={dbMode === v}
                        onChange={() => setDbMode(v)}
                      />
                      <span className="appsel-radio" aria-hidden="true" />
                      <span className="appsel-mode-text">
                        <b>{label}</b>
                        <small>{help}</small>
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>

        <section className="pk-section appsel-summary">
          <header className="pk-section-head">
            <h2>선택 요약</h2>
            <span className="pk-section-meta">사전 점검 입력</span>
          </header>
          <dl className="appsel-summary-list">
            <div>
              <dt>앱</dt>
              <dd>
                <b>{chosen.length}</b>
                <span>{chosen.map((w) => w.name).join(", ") || "없음"}</span>
              </dd>
            </div>
            <div>
              <dt>자원</dt>
              <dd>
                <b>{(sum.cpu / 1000).toFixed(1)} vCPU</b>
                <span>
                  {sum.mem} Mi · PVC {sum.pvcs.length}개
                </span>
              </dd>
            </div>
            <div className={sum.dbs.length ? "tone-warn" : ""}>
              <dt>DB</dt>
              <dd>
                <b>{sum.dbs.length}</b>
                <span>
                  {sum.dbs.length
                    ? `${sum.dbs.join(", ")} · ${{ cdc: "CDC 복제", snapshot: "스냅샷", shared: "공유 유지" }[dbMode]}`
                    : "없음"}
                </span>
              </dd>
            </div>
            <div>
              <dt>Secret</dt>
              <dd>
                <b>{sum.secrets.length}</b>
                <span>
                  {sum.secrets.length
                    ? "도착 비밀 저장소에 같은 이름으로"
                    : "없음"}
                </span>
              </dd>
            </div>
            <div className={sum.missing.length ? "tone-bad" : ""}>
              <dt>이미지</dt>
              <dd>
                <b>
                  {chosen.length - sum.missing.length}/{chosen.length}
                </b>
                <span>
                  {sum.missing.length
                    ? `${sum.missing.join(", ")} 은 도착에 없음 → 점검에서 막힘`
                    : chosen.length
                      ? "전부 도착에서 받을 수 있음"
                      : "없음"}
                </span>
              </dd>
            </div>
          </dl>
        </section>
      </aside>
    </div>
  );
}
