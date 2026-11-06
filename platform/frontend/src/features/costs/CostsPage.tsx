import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import type {
  CostBreakdown,
  CostChange,
  CostOverview,
  EnvironmentCost,
  MigrationCost,
} from "../../api/costs";
import { PageActions } from "../../components/PageSlots";
import { PageHeader, Section } from "../../components/PageKit";
import { Segmented } from "../../components/Segmented";
import { formatAgo } from "../../lib/format";
import {
  BASIS_LABELS,
  byCategory,
  CATEGORY_LABELS,
  CHANGE_LABELS,
  percent,
  quantityText,
  signedUsd,
  unitUsd,
  usd,
} from "./model";
import { costsQueryOptions } from "./queries";

const VISIBLE_LINES = 6;

function scrollTo(id: string) {
  document
    .getElementById(id)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function categoryName(category: string) {
  return CATEGORY_LABELS[category] ?? category;
}

function deltaClass(value: number | null) {
  if (value === null || value === 0) return "";
  return value > 0 ? "up" : "down";
}

type Names = {
  name: (id: string) => string;
  estimated: (id: string) => boolean;
};

/** 월 변화 하나와 그 근거가 되는 숫자들. */
function Summary({ migration, name }: { migration: MigrationCost } & Names) {
  const increase =
    migration.target_monthly_after !== null &&
    migration.target_monthly_now !== null
      ? migration.target_monthly_after - migration.target_monthly_now
      : null;
  const pct = percent(increase, migration.target_monthly_now);
  const capacity = migration.capacity;
  const facts: [string, string][] = [
    [
      name(migration.target_env),
      `${usd(migration.target_monthly_now)} → ${usd(migration.target_monthly_after)}${pct !== null ? ` (${increase !== null && increase >= 0 ? "+" : ""}${pct}%)` : ""}`,
    ],
    [
      name(migration.source_env),
      migration.source_released
        ? `${signedUsd(-migration.source_released)} (옮기는 워크로드 몫)`
        : "변화 없음",
    ],
    ["1회성", usd(migration.one_time?.monthly ?? null)],
    [
      "대상 용량",
      capacity === null
        ? "-"
        : `${migration.capacity_fits ? "충분" : "부족"} · vCPU ${capacity.need_vcpu}/${capacity.free_vcpu} · 메모리 ${capacity.need_memory_gib}/${capacity.free_memory_gib} GiB`,
    ],
  ];
  return (
    <div className="cost-summary">
      <p className="cost-total">
        <span className={deltaClass(migration.monthly_change)}>
          {migration.monthly_change === null
            ? "계산 불가"
            : signedUsd(migration.monthly_change)}
        </span>
        {migration.monthly_change !== null && <small>/월</small>}
      </p>
      <dl className="cost-facts">
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function BeforeAfter({
  migration,
  name,
  estimated,
}: { migration: MigrationCost } & Names) {
  const sourceBefore = migration.source_monthly;
  const sourceAfter =
    sourceBefore !== null && migration.source_released !== null
      ? sourceBefore - migration.source_released
      : null;
  const rows = [
    {
      id: migration.source_env,
      role: "원본",
      before: sourceBefore,
      after: sourceAfter,
    },
    {
      id: migration.target_env,
      role: "대상",
      before: migration.target_monthly_now,
      after: migration.target_monthly_after,
    },
  ];
  const max = Math.max(
    1,
    ...rows.flatMap((r) => [r.before ?? 0, r.after ?? 0]),
  );
  return (
    <table className="cost-ba">
      <caption>이전 전후 월 비용</caption>
      <tbody>
        {rows.map((row) =>
          (["before", "after"] as const).map((when) => (
            <tr key={`${row.id}-${when}`} className={when}>
              {when === "before" && (
                <th rowSpan={2} scope="rowgroup">
                  {name(row.id)}
                  <span>
                    {row.role}
                    {estimated(row.id) ? " · AWS 환산" : ""}
                  </span>
                </th>
              )}
              <td className="cost-ba-when">
                {when === "before" ? "전" : "후"}
              </td>
              <td className="cost-ba-track">
                <span style={{ width: `${((row[when] ?? 0) / max) * 100}%` }} />
              </td>
              <td className="num">{usd(row[when])}</td>
            </tr>
          )),
        )}
      </tbody>
    </table>
  );
}

const CHANGE_MARKS = { added: "+", removed: "−", replaced: "~", moved: "→" };

function ChangeRow({ change }: { change: CostChange }) {
  return (
    <tr>
      <td className={`cost-mark change-${change.change}`} aria-hidden="true">
        {CHANGE_MARKS[change.change]}
      </td>
      <td className="cost-wrap">
        {change.source ?? <span className="muted">-</span>}
        <div className="sub">
          {CHANGE_LABELS[change.change]} · {categoryName(change.category)}
          {change.before !== null && ` · 지금 ${usd(change.before)}/월`}
        </div>
      </td>
      <td className="cost-wrap">
        {change.target ?? <span className="muted">-</span>}
        {change.note && <div className="sub">{change.note}</div>}
      </td>
      <td className={`num cost-delta ${deltaClass(change.delta)}`}>
        {change.delta === null ? "단가 없음" : signedUsd(change.delta)}
      </td>
    </tr>
  );
}

function ChangesTable({ migration }: { migration: MigrationCost }) {
  if (migration.changes.length === 0)
    return <p className="pk-empty">바뀌는 자원 없음</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table cost-table cost-changes">
        <thead>
          <tr>
            <th aria-label="변화" />
            <th>원본</th>
            <th>대상</th>
            <th className="num">월 변화</th>
          </tr>
        </thead>
        <tbody>
          {migration.changes.map((change, i) => (
            <ChangeRow key={`${change.change}-${i}`} change={change} />
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td />
            <td colSpan={2}>합계</td>
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

function LinesTable({
  breakdown,
  limit,
}: {
  breakdown: CostBreakdown;
  limit?: number;
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
            <th className="num">단가</th>
            <th className="num">월</th>
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
              <td>{categoryName(line.category)}</td>
              <td className="num">{quantityText(line)}</td>
              <td className="num">{unitUsd(line.unit_price)}</td>
              <td className="num">{usd(line.monthly)}</td>
              <td className={`cost-basis basis-${line.basis}`}>
                {BASIS_LABELS[line.basis]}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4}>
              {hidden > 0 ? (
                <button
                  type="button"
                  className="cost-more"
                  onClick={() => setAll(true)}
                >
                  나머지 {hidden}개
                </button>
              ) : (
                "합계"
              )}
              {breakdown.unpriced > 0 && (
                <span className="sub">
                  {" "}
                  · 단가 없음 {breakdown.unpriced}개 제외
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

function MigrationView(props: { migration: MigrationCost } & Names) {
  const { migration } = props;
  return (
    <div className="cost-migration">
      <Summary {...props} />
      <BeforeAfter {...props} />
      <h3 className="cost-subtitle">변경 내역</h3>
      <ChangesTable migration={migration} />
      {migration.one_time && (
        <>
          <h3 className="cost-subtitle">
            1회성 <span>{usd(migration.one_time.monthly)}</span>
          </h3>
          <LinesTable breakdown={migration.one_time} />
        </>
      )}
      {(migration.assumptions.length > 0 || migration.gaps.length > 0) && (
        <details className="cost-assumptions">
          <summary>
            가정 {migration.assumptions.length + migration.gaps.length}개
          </summary>
          <ul>
            {migration.gaps.map((text) => (
              <li key={text} className="gap">
                {text}
              </li>
            ))}
            {migration.assumptions.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function CategoryBar({ breakdown }: { breakdown: CostBreakdown }) {
  const parts = byCategory(breakdown.lines).filter((p) => p.monthly > 0);
  if (breakdown.monthly <= 0 || parts.length === 0) return null;
  return (
    <>
      <div
        className="cost-bar"
        role="img"
        aria-label={`분류별 비중: ${parts.map((p) => `${categoryName(p.category)} ${usd(p.monthly)}`).join(", ")}`}
      >
        {parts.map((part) => (
          <span
            key={part.category}
            className={`cost-seg seg-${part.category}`}
            style={{ flexGrow: part.monthly }}
          />
        ))}
      </div>
      <ul className="cost-legend">
        {parts.map((part) => (
          <li key={part.category}>
            <span className={`cost-dot seg-${part.category}`} />
            {categoryName(part.category)}
            <span className="num">{usd(part.monthly)}</span>
            <span className="num muted">
              {percent(part.monthly, breakdown.monthly)}%
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

function EnvironmentRow({ env }: { env: EnvironmentCost }) {
  const missingInput =
    env.provider === "onprem" &&
    env.current !== null &&
    env.current.unpriced > 0;
  const main = missingInput ? env.cloud_equivalent : env.current;
  return (
    <section className="cost-env" aria-label={env.name}>
      <div className="cost-env-side">
        <h3>{env.name}</h3>
        <p className="cost-env-total">
          {main ? usd(main.monthly) : "-"}
          <small>/월</small>
        </p>
        <p className="cost-env-meta">
          {[
            missingInput ? "장비비·전기료 미입력, AWS 동급 환산" : null,
            env.observed_at ? `수집 ${formatAgo(env.observed_at)}` : "수집 전",
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
        {main && <CategoryBar breakdown={main} />}
        {env.gaps.length > 0 && (
          <p className="cost-note">빠진 것: {env.gaps.join(" · ")}</p>
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

export function CostsPage() {
  const { data: overview } = useSuspenseQuery(costsQueryOptions());
  const [jobId, setJobId] = useState(overview.migrations[0]?.job_id ?? "");
  const name = (id: string) =>
    overview.environments.find((e) => e.env_id === id)?.name ?? id;
  // 온프렘 장비비·전기료가 없어 클라우드 동급 환산값을 쓰는 환경
  const estimated = (id: string) => {
    const env = overview.environments.find((e) => e.env_id === id);
    return (
      env?.provider === "onprem" &&
      (env.current?.unpriced ?? 0) > 0 &&
      env.cloud_equivalent !== null
    );
  };
  const migration = overview.migrations.find((m) => m.job_id === jobId);
  const cloudTotal = overview.environments
    .filter((env) => env.provider !== "onprem")
    .reduce((sum, env) => sum + (env.current?.monthly ?? 0), 0);
  return (
    <div className="pk-page">
      <PageActions>
        <span className="updated-at">
          계산 {formatAgo(overview.generated_at)}
        </span>
      </PageActions>
      <PageHeader
        title="비용"
        tabs={[
          { id: "migration", label: "이전 영향" },
          { id: "environments", label: "환경별" },
          { id: "basis", label: "계산 기준" },
        ]}
        active="migration"
        onTab={scrollTo}
      />
      <Section
        id="migration"
        title="이전 영향"
        description="원본 워크로드를 대상에 다시 배치한 추산, USD"
      >
        {overview.migrations.length > 0 ? (
          <>
            <Segmented
              label="이전 작업"
              value={jobId}
              options={overview.migrations.map((m) => ({
                value: m.job_id,
                label: `${name(m.source_env)} → ${name(m.target_env)}`,
              }))}
              onChange={setJobId}
            />
            {migration && (
              <MigrationView
                migration={migration}
                name={name}
                estimated={estimated}
              />
            )}
          </>
        ) : (
          <p className="pk-empty">등록된 이전 작업 없음</p>
        )}
      </Section>
      <Section
        id="environments"
        title="환경별 월 비용"
        description={`클라우드 합계 ${usd(cloudTotal)}`}
      >
        <div className="cost-envs">
          {overview.environments.map((env) => (
            <EnvironmentRow key={env.env_id} env={env} />
          ))}
        </div>
      </Section>
      <Section id="basis" title="계산 기준">
        <ul className="cost-sources">
          {priceSources(overview).map((text) => (
            <li key={text}>가격표 {text}</li>
          ))}
          <li>
            떠 있는 자원 × 온디맨드 단가. 월 730시간, 약정 할인·크레딧·세금
            제외.
          </li>
          <li>
            데이터 전송: 지난 7일 CloudWatch 측정값을 월로 환산. AWS만, 가용
            영역 간 전송 제외.
          </li>
          <li>
            온프렘: 장비비·전기료 입력 또는 측정기 연결 전에는 AWS 동급 환산.
          </li>
        </ul>
      </Section>
    </div>
  );
}
