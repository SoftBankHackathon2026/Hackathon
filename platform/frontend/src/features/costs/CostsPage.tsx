import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import type {
  CostBreakdown,
  CostOverview,
  EnvironmentCost,
  MigrationCost,
} from "../../api/costs";
import { PageHeader, Section, StatusLabel } from "../../components/PageKit";
import { PageActions } from "../../components/PageSlots";
import { ProviderLogo } from "../../components/ProviderLogo";
import { RefreshStatus } from "../../components/RefreshStatus";
import { Segmented } from "../../components/Segmented";
import { useSort } from "../../components/SortableTable";
import { Widget } from "../../components/Widget";
import { formatAgo } from "../../lib/format";
import { fleetQueryOptions } from "../migrations/queries";
import {
  CHANGE_KIND_LABELS,
  categoryMatrix,
  compareRows,
  dataHealth,
  envTotals,
  mainBreakdown,
  pickJob,
  sortedChanges,
  topResources,
  envLabels,
  type EnvLabel,
  withoutDisconnected,
} from "./costView";
import {
  BASIS_LABELS,
  byCategory,
  CATEGORY_LABELS,
  percent,
  quantityText,
  signedUsd,
  unitUsd,
  usd,
} from "./model";
import { costsQueryOptions } from "./queries";

type Tab = "overview" | "migration" | "environments";

const VISIBLE_LINES = 6;

/** 분류 색: 차트 범주색(순서 고정). 주요 분류만 색, 나머지는 회색 단계. */
const CATEGORY_COLORS: Record<string, string> = {
  compute: "var(--dataviz-1)",
  database: "var(--dataviz-3)",
  messaging: "var(--dataviz-2)",
  storage: "var(--dataviz-4)",
  network: "var(--dataviz-5)",
  control_plane: "#6b7684",
  transfer: "#b0b8c1",
  onprem: "#333d4b",
};
const ENV_COLORS = [
  "var(--dataviz-1)",
  "var(--dataviz-2)",
  "var(--dataviz-3)",
  "var(--dataviz-4)",
  "var(--dataviz-5)",
];

function categoryColor(category: string) {
  return CATEGORY_COLORS[category] ?? "#8b95a1";
}

function categoryName(category: string) {
  return CATEGORY_LABELS[category] ?? category;
}

function shortName(name: string) {
  return name.split(" (")[0];
}

function deltaClass(value: number | null) {
  if (value === null || value === 0) return "";
  return value > 0 ? "up" : "down";
}

/** 장비비가 없는 온프렘 환경의 같은 사양 AWS 환산 월 비용. */
function estimateOf(overview: CostOverview) {
  return (envId: string) => {
    const env = overview.environments.find((e) => e.env_id === envId);
    if (!env) return null;
    const { breakdown, estimated } = mainBreakdown(env);
    return estimated ? (breakdown?.monthly ?? null) : null;
  };
}

function CapacityLabel({ migration }: { migration: MigrationCost }) {
  if (migration.capacity === null) return <span className="sub">-</span>;
  return (
    <StatusLabel tone={migration.capacity_fits ? "ok" : "bad"}>
      {migration.capacity_fits ? "수용 가능" : "노드 추가 필요"}
    </StatusLabel>
  );
}

/* ── 개요 ─────────────────────────────────────────────── */

function TotalWidget({
  overview,
  onEnvironments,
}: {
  overview: CostOverview;
  onEnvironments: () => void;
}) {
  const totals = envTotals(overview);
  const anyEstimated = totals.rows.some((r) => r.estimated);
  return (
    <Widget
      title="월 비용 합계"
      sub="모든 환경, 온디맨드 가격 기준"
      className="w-cost-total"
      action={{ label: "환경별 보기", onClick: onEnvironments }}
    >
      <p className="cost-big">
        {usd(totals.total)}
        <small>/월</small>
      </p>
      <dl className="cost-split">
        <div>
          <dt>클라우드</dt>
          <dd>{usd(totals.cloud)}</dd>
        </div>
        <div>
          <dt>온프렘{anyEstimated ? " (AWS 가격으로 환산)" : ""}</dt>
          <dd>{usd(totals.onprem)}</dd>
        </div>
      </dl>
      <span className="share-bar" aria-hidden="true">
        {totals.rows.map((r, i) => (
          <i
            key={r.env.env_id}
            style={{
              width: `${totals.total > 0 ? (r.monthly / totals.total) * 100 : 0}%`,
              background: ENV_COLORS[i % ENV_COLORS.length],
            }}
          />
        ))}
      </span>
      <ul className="share-legend">
        {totals.rows.map((r, i) => (
          <li key={r.env.env_id}>
            <i style={{ background: ENV_COLORS[i % ENV_COLORS.length] }} />
            <span className="share-name" title={r.env.name}>
              {r.env.name}
            </span>
            <b>{usd(r.monthly)}</b>
            <span className="share-pct">
              {percent(r.monthly, totals.total) ?? 0}%
            </span>
          </li>
        ))}
      </ul>
    </Widget>
  );
}

function MovesWidget({
  overview,
  label,
  onMigration,
}: {
  overview: CostOverview;
  label: (id: string) => EnvLabel;
  onMigration: (jobId: string) => void;
}) {
  return (
    <Widget
      title="이전 작업별 월 비용 변화"
      sub="원본과 대상 환경을 합친 월 비용, 행을 누르면 자세히"
      className="w-cost-moves"
    >
      {overview.migrations.length === 0 ? (
        <p className="pk-empty">
          등록된 이전 작업이 없어요. 마이그레이션 화면에서 작업을 만들면 비용
          변화가 계산돼요.
        </p>
      ) : (
        <div
          className="table-wrap"
          tabIndex={0}
          role="region"
          aria-label="이전 비용 요약"
        >
          <table className="pk-table w-table">
            <thead>
              <tr>
                <th>원본 → 대상</th>
                <th className="num">지금 월 비용</th>
                <th className="num">이전 후 월 비용</th>
                <th className="num">월 변화</th>
                <th className="num">일회성 비용</th>
                <th>대상 클러스터</th>
              </tr>
            </thead>
            <tbody>
              {overview.migrations.map((m) => {
                const { total } = compareRows(m, estimateOf(overview));
                const pct = percent(m.monthly_change, total.now);
                return (
                  <tr
                    key={m.job_id}
                    className="clickable"
                    tabIndex={0}
                    onClick={() => onMigration(m.job_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") onMigration(m.job_id);
                    }}
                  >
                    <td>
                      <span className="cost-route">
                        <ProviderLogo provider={label(m.source_env).provider} />
                        {shortName(label(m.source_env).name)}
                        <span className="cost-route-arrow" aria-hidden="true">
                          →
                        </span>
                        <ProviderLogo provider={label(m.target_env).provider} />
                        {shortName(label(m.target_env).name)}
                      </span>
                    </td>
                    <td className="num">{usd(total.now)}</td>
                    <td className="num strong">{usd(total.after)}</td>
                    <td
                      className={`num cost-delta ${deltaClass(m.monthly_change)}`}
                    >
                      {m.monthly_change === null
                        ? "계산 불가"
                        : signedUsd(m.monthly_change)}
                      {pct !== null && m.monthly_change !== 0 && (
                        <span className="sub"> ({pct}%)</span>
                      )}
                    </td>
                    <td className="num">{usd(m.one_time?.monthly ?? null)}</td>
                    <td>
                      <CapacityLabel migration={m} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Widget>
  );
}

function CategoryWidget({ overview }: { overview: CostOverview }) {
  const matrix = categoryMatrix(overview);
  const max = Math.max(1, ...matrix.envs.map((e) => e.total));
  return (
    <Widget
      title="분류별 월 비용"
      sub="환경마다 막대 길이 = 월 비용"
      className="w-cost-cats"
    >
      <ul className="cat-rows">
        {matrix.envs.map(({ env, total, parts }) => (
          <li key={env.env_id}>
            <span className="cat-env" title={env.name}>
              <ProviderLogo provider={env.provider} />
              {shortName(env.name)}
            </span>
            <span className="cat-track">
              <span
                className="cat-stack"
                style={{ width: `${(total / max) * 100}%` }}
                role="img"
                aria-label={parts
                  .filter((p) => p.monthly > 0)
                  .map((p) => `${categoryName(p.category)} ${usd(p.monthly)}`)
                  .join(", ")}
              >
                {parts
                  .filter((p) => p.monthly > 0)
                  .map((p) => (
                    <i
                      key={p.category}
                      title={`${categoryName(p.category)} ${usd(p.monthly)}`}
                      style={{
                        flexGrow: p.monthly,
                        background: categoryColor(p.category),
                      }}
                    />
                  ))}
              </span>
            </span>
            <b>{usd(total)}</b>
          </li>
        ))}
      </ul>
      <ul className="cat-legend">
        {matrix.categories.map((c) => (
          <li key={c}>
            <i style={{ background: categoryColor(c) }} />
            {categoryName(c)}
            <b>{usd(matrix.totals.get(c) ?? 0)}</b>
          </li>
        ))}
      </ul>
    </Widget>
  );
}

function TopWidget({ overview }: { overview: CostOverview }) {
  const top = topResources(overview, 6);
  const max = top[0]?.line.monthly ?? 1;
  return (
    <Widget
      title="월 비용이 큰 자원"
      sub="모든 환경, 상위 6개"
      className="w-cost-top"
    >
      <ol className="top-res">
        {top.map(({ env, line }, i) => (
          <li key={`${env.env_id}-${line.item}-${i}`}>
            <span className="rule-text">
              <span className="rule-title" title={line.item}>
                {line.item}
              </span>
              <span className="rule-sub">
                {categoryName(line.category)}, {shortName(env.name)}
              </span>
            </span>
            <span className="top-res-track" aria-hidden="true">
              <i
                style={{
                  width: `${((line.monthly ?? 0) / max) * 100}%`,
                  background: categoryColor(line.category),
                }}
              />
            </span>
            <b>{usd(line.monthly)}</b>
          </li>
        ))}
      </ol>
    </Widget>
  );
}

function BasisWidget({ overview }: { overview: CostOverview }) {
  const health = dataHealth(overview);
  return (
    <Widget title="계산 기준과 빠진 항목" className="w-cost-basis">
      <div className="basis-grid">
        <dl className="basis-counts">
          <div>
            <dt>가격 정보 없는 항목</dt>
            <dd className={health.unpriced > 0 ? "text-warn" : undefined}>
              {health.unpriced}개
            </dd>
          </div>
          <div>
            <dt>AWS 가격으로 환산한 환경</dt>
            <dd>{health.estimated.length}곳</dd>
          </div>
          <div>
            <dt>계산에서 빠진 항목</dt>
            <dd className={health.gaps.length > 0 ? "text-warn" : undefined}>
              {health.gaps.length}개
            </dd>
          </div>
        </dl>
        <ul className="basis-list">
          {priceSources(overview).map((text) => (
            <li key={text}>가격표 {text}</li>
          ))}
          <li>
            떠 있는 자원 × 온디맨드 가격, 월 730시간. 약정 할인, 크레딧, 세금
            제외.
          </li>
          <li>
            데이터 전송: 지난 7일 CloudWatch 측정값을 월로 환산. AWS만, 가용
            영역 간 전송 제외.
          </li>
          <li>
            온프렘: 장비비와 전기료를 입력하거나 측정기를 연결하기 전에는 같은
            사양 AWS 가격으로 계산.
          </li>
          {health.gaps.map(({ env, gap }) => (
            <li key={`${env.env_id}-${gap}`} className="gap">
              빠진 항목: {gap} ({shortName(env.name)})
            </li>
          ))}
        </ul>
      </div>
    </Widget>
  );
}

/* ── 이전 영향 ────────────────────────────────────────── */

function CompareTable({
  migration,
  overview,
  label,
}: {
  migration: MigrationCost;
  overview: CostOverview;
  label: (id: string) => EnvLabel;
}) {
  const { rows, total } = compareRows(migration, estimateOf(overview));
  const pct = percent(total.change, total.now);
  return (
    <div
      className="table-wrap"
      tabIndex={0}
      role="region"
      aria-label="이전 전후 비용"
    >
      <table className="pk-table compare-table">
        <thead>
          <tr>
            <th>환경</th>
            <th className="num">지금 월 비용</th>
            <th className="num">변화</th>
            <th className="num">이전 후 월 비용</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const env = overview.environments.find((e) => e.env_id === r.env);
            const estimated = env ? mainBreakdown(env).estimated : false;
            return (
              <tr key={r.role}>
                <td>
                  <span className="cost-env-cell">
                    <ProviderLogo provider={label(r.env).provider} />
                    <span className="rule-text">
                      <span className="rule-title">{label(r.env).name}</span>
                      <span className="rule-sub">
                        {r.role}
                        {estimated ? ", AWS 가격으로 환산" : ""}
                      </span>
                    </span>
                  </span>
                </td>
                <td className="num">{usd(r.now)}</td>
                <td className={`num cost-delta ${deltaClass(r.change)}`}>
                  {r.change === null
                    ? "-"
                    : r.change === 0
                      ? "변화 없음"
                      : signedUsd(r.change)}
                </td>
                <td className="num strong">{usd(r.after)}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>합계</td>
            <td className="num">{usd(total.now)}</td>
            <td className={`num cost-delta ${deltaClass(total.change)}`}>
              {total.change === null ? "계산 불가" : signedUsd(total.change)}
              {pct !== null && total.change !== 0 && (
                <span className="sub"> ({pct}%)</span>
              )}
            </td>
            <td className="num">{usd(total.after)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function CompareFacts({ migration }: { migration: MigrationCost }) {
  const capacity = migration.capacity;
  return (
    <dl className="compare-facts">
      <div>
        <dt>일회성 비용</dt>
        <dd>{usd(migration.one_time?.monthly ?? null)}</dd>
        <p>이중 운영 {migration.dual_run_days}일 + 데이터 복사</p>
      </div>
      <div>
        <dt>대상 클러스터 수용 가능 여부</dt>
        <dd>
          <CapacityLabel migration={migration} />
        </dd>
        {capacity && (
          <p>
            vCPU 필요 {capacity.need_vcpu}, 여유 {capacity.free_vcpu}
            <br />
            메모리 필요 {capacity.need_memory_gib} GiB, 여유{" "}
            {capacity.free_memory_gib} GiB
          </p>
        )}
      </div>
    </dl>
  );
}

function ChangesTable({ migration }: { migration: MigrationCost }) {
  const rows = sortedChanges(migration.changes);
  const { sorted, th } = useSort(rows, {
    kind: (c) => CHANGE_KIND_LABELS[c.change],
    category: (c) => categoryName(c.category),
    delta: (c) => c.delta,
  });
  if (rows.length === 0) return <p className="pk-empty">바뀌는 자원 없음</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table change-table">
        <thead>
          <tr>
            <th>지금 (원본)</th>
            <th>이전 후 (대상)</th>
            {th("kind", "변화")}
            {th("category", "분류")}
            {th("delta", "월 변화", "num")}
          </tr>
        </thead>
        <tbody>
          {sorted.map((c, i) => (
            <tr key={`${c.change}-${i}`}>
              <td className="cost-wrap">
                {c.source ?? <span className="sub">-</span>}
                {c.before !== null && (
                  <div className="sub">{usd(c.before)}/월</div>
                )}
              </td>
              <td className="cost-wrap">
                {c.target ?? <span className="sub">-</span>}
                {c.note && <div className="sub">{c.note}</div>}
              </td>
              <td>
                <span className={`change-kind kind-${c.change}`}>
                  {CHANGE_KIND_LABELS[c.change]}
                </span>
              </td>
              <td>
                <span className="cat-chip">
                  <i style={{ background: categoryColor(c.category) }} />
                  {categoryName(c.category)}
                </span>
              </td>
              <td className={`num cost-delta ${deltaClass(c.delta)}`}>
                {c.delta === null ? "가격 정보 없음" : signedUsd(c.delta)}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4}>월 변화 합계</td>
            <td
              className={`num cost-delta ${deltaClass(migration.monthly_change)}`}
            >
              {signedUsd(migration.monthly_change)}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function MigrationTab({
  overview,
  label,
  jobId,
  onJob,
}: {
  overview: CostOverview;
  label: (id: string) => EnvLabel;
  jobId: string;
  onJob: (id: string) => void;
}) {
  const name = (id: string) => label(id).name;
  const migration = overview.migrations.find((m) => m.job_id === jobId);
  if (overview.migrations.length === 0)
    return (
      <Section title="이전 영향">
        <p className="pk-empty">
          등록된 이전 작업이 없어요. 마이그레이션 화면에서 작업을 만들면 비용
          변화가 계산돼요.
        </p>
      </Section>
    );
  return (
    <>
      {overview.migrations.length > 1 && (
        <Segmented
          label="이전 작업"
          value={jobId}
          options={overview.migrations.map((m) => ({
            value: m.job_id,
            label: `${shortName(name(m.source_env))} → ${shortName(name(m.target_env))}`,
          }))}
          onChange={onJob}
        />
      )}
      {migration && (
        <>
          <Section title="월 비용 비교">
            <div className="compare-wrap">
              <CompareTable
                migration={migration}
                overview={overview}
                label={label}
              />
              <CompareFacts migration={migration} />
            </div>
          </Section>
          <Section
            title="자원별 변화"
            meta={`${migration.changes.length}개, 월 변화가 큰 순`}
          >
            <ChangesTable migration={migration} />
          </Section>
          {migration.one_time && (
            <Section title="일회성 비용" meta={usd(migration.one_time.monthly)}>
              <LinesTable breakdown={migration.one_time} total="일회성 합계" />
            </Section>
          )}
          {(migration.assumptions.length > 0 || migration.gaps.length > 0) && (
            <Section
              title="가정과 빠진 항목"
              meta={`${migration.assumptions.length + migration.gaps.length}개`}
            >
              <ul className="basis-list">
                {migration.gaps.map((text) => (
                  <li key={text} className="gap">
                    {text}
                  </li>
                ))}
                {migration.assumptions.map((text) => (
                  <li key={text}>{text}</li>
                ))}
              </ul>
            </Section>
          )}
        </>
      )}
    </>
  );
}

/* ── 환경별 ───────────────────────────────────────────── */

function LinesTable({
  breakdown,
  limit,
  total = "월 합계",
}: {
  breakdown: CostBreakdown;
  limit?: number;
  total?: string;
}) {
  const [all, setAll] = useState(false);
  if (breakdown.lines.length === 0)
    return <p className="pk-empty">항목 없음</p>;
  const sorted = [...breakdown.lines].sort(
    (a, b) => (b.monthly ?? -1) - (a.monthly ?? -1),
  );
  const shown = limit && !all ? sorted.slice(0, limit) : sorted;
  const hidden = sorted.length - shown.length;
  return (
    <div className="table-wrap">
      <table className="pk-table cost-table">
        <thead>
          <tr>
            <th>항목</th>
            <th>분류</th>
            <th className="num">수량</th>
            <th className="num">단위 가격</th>
            <th className="num">월 비용</th>
            <th>근거</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((line, i) => (
            <tr key={`${line.item}-${i}`}>
              <td className="cost-wrap">
                {line.item}
                {line.note && <div className="sub">{line.note}</div>}
              </td>
              <td>
                <span className="cat-chip">
                  <i style={{ background: categoryColor(line.category) }} />
                  {categoryName(line.category)}
                </span>
              </td>
              <td className="num">{quantityText(line)}</td>
              <td className="num">{unitUsd(line.unit_price)}</td>
              <td className="num strong">{usd(line.monthly)}</td>
              <td className={`cost-basis basis-${line.basis}`}>
                {BASIS_LABELS[line.basis]}
              </td>
            </tr>
          ))}
          {hidden > 0 && (
            <tr className="pk-more-row">
              <td colSpan={6}>
                <button
                  type="button"
                  className="cost-more"
                  onClick={() => setAll(true)}
                >
                  나머지 {hidden}개 보기
                </button>
              </td>
            </tr>
          )}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4}>
              {total}
              {breakdown.unpriced > 0 && (
                <span className="sub">
                  {" "}
                  (가격 정보 없는 {breakdown.unpriced}개 제외)
                </span>
              )}
            </td>
            <td className="num">{usd(breakdown.monthly)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

/** 분류별 비용을 큰 순서의 가로 막대로. 값은 막대 옆에 직접 쓴다. */
function CategoryBar({ breakdown }: { breakdown: CostBreakdown }) {
  const parts = byCategory(breakdown.lines).filter((p) => p.monthly > 0);
  if (breakdown.monthly <= 0 || parts.length === 0) return null;
  const top = parts[0].monthly;
  return (
    <ol className="cost-toplist" aria-label="분류별 월 비용">
      {parts.map((part) => (
        <li key={part.category}>
          <span className="cost-toplist-name">
            {categoryName(part.category)}
          </span>
          <span className="cost-toplist-track" aria-hidden="true">
            <span
              style={{
                width: `${(part.monthly / top) * 100}%`,
                background: categoryColor(part.category),
              }}
            />
          </span>
          <span className="cost-toplist-value">{usd(part.monthly)}</span>
          <span className="cost-toplist-pct">
            {percent(part.monthly, breakdown.monthly)}%
          </span>
        </li>
      ))}
    </ol>
  );
}

function EnvironmentRow({ env }: { env: EnvironmentCost }) {
  const { breakdown: main, estimated } = mainBreakdown(env);
  return (
    <section className="cost-env" aria-label={env.name}>
      <div className="cost-env-side">
        <h3 className="cost-env-name">
          <ProviderLogo provider={env.provider} />
          {env.name}
        </h3>
        <p className="cost-env-total">
          {main ? usd(main.monthly) : "-"}
          <small>/월</small>
        </p>
        <p className="cost-env-meta">
          {[
            estimated
              ? "장비비, 전기료 미입력: 같은 사양 AWS 가격으로 계산"
              : null,
            env.observed_at ? `수집 ${formatAgo(env.observed_at)}` : "수집 전",
          ]
            .filter(Boolean)
            .join(", ")}
        </p>
        {main && <CategoryBar breakdown={main} />}
        {env.gaps.length > 0 && (
          <p className="cost-note">계산에서 빠진 항목: {env.gaps.join(", ")}</p>
        )}
      </div>
      <div className="cost-env-main">
        {main ? (
          <LinesTable breakdown={main} limit={VISIBLE_LINES} />
        ) : (
          <p className="pk-empty">비용 정보 없음</p>
        )}
      </div>
    </section>
  );
}

function priceSources(overview: CostOverview) {
  const seen = new Map<string, string>();
  for (const env of overview.environments)
    if (env.prices)
      seen.set(
        `${env.prices.provider}-${env.prices.region}`,
        `${env.prices.provider.toUpperCase()} ${env.prices.region}: ${env.prices.source}, ${env.prices.fetched_at.slice(0, 10)}`,
      );
  return [...seen.values()];
}

/**
 * 비용. 개요(AWS Billing 홈, Datadog 비용 요약 방식 위젯) → 이전 영향(Infracost 방식 비교표) → 환경별.
 */
const TABS: Tab[] = ["overview", "migration", "environments"];

export function CostsPage({ tab: initial }: { tab?: string } = {}) {
  const { data: costs } = useSuspenseQuery(costsQueryOptions());
  const fleet = useQuery(fleetQueryOptions());
  const overview = withoutDisconnected(costs, fleet.data?.environments);
  const label = envLabels(overview, fleet.data?.environments);
  // 탭은 주소(#costs/<탭>)가 정한다. 같은 화면에서 주소만 바뀌어도(뒤로 가기, 링크) 따라간다.
  const tab: Tab = TABS.includes(initial as Tab)
    ? (initial as Tab)
    : "overview";
  const setTab = (next: Tab) => {
    const hash = next === "overview" ? "#costs" : `#costs/${next}`;
    if (window.location.hash !== hash) window.location.hash = hash;
  };
  const [picked, setJobId] = useState("");
  const jobId = pickJob(overview.migrations, picked);
  const totals = envTotals(overview);
  return (
    <div className="pk-page">
      <PageActions>
        <RefreshStatus at={overview.generated_at} verb="계산" />
      </PageActions>
      <PageHeader
        title="비용"
        tabs={[
          { id: "overview", label: "개요" },
          { id: "migration", label: "이전 영향" },
          { id: "environments", label: "환경별" },
        ]}
        active={tab}
        onTab={(id) => setTab(id as Tab)}
      />
      {tab === "overview" && (
        <div className="sec-grid">
          <TotalWidget
            overview={overview}
            onEnvironments={() => setTab("environments")}
          />
          <CategoryWidget overview={overview} />
          <MovesWidget
            overview={overview}
            label={label}
            onMigration={(id) => {
              setJobId(id);
              setTab("migration");
            }}
          />
          <TopWidget overview={overview} />
          <BasisWidget overview={overview} />
        </div>
      )}
      {tab === "migration" && (
        <MigrationTab
          overview={overview}
          label={label}
          jobId={jobId}
          onJob={setJobId}
        />
      )}
      {tab === "environments" && (
        <Section title="환경별 월 비용" meta={`합계 ${usd(totals.total)}/월`}>
          <div className="cost-envs">
            {overview.environments.map((env) => (
              <EnvironmentRow key={env.env_id} env={env} />
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
