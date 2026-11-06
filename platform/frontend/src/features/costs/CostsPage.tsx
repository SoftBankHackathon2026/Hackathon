import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import type {
  CostBreakdown,
  CostOverview,
  EnvironmentCost,
  MigrationCost,
} from "../../api/costs";
import { PageActions } from "../../components/PageSlots";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
  SummaryCard,
} from "../../components/PageKit";
import { formatAgo } from "../../lib/format";
import {
  BASIS_LABELS,
  byCategory,
  CATEGORY_LABELS,
  quantityText,
  signedUsd,
  unitUsd,
  usd,
} from "./model";
import { costsQueryOptions } from "./queries";

function scrollTo(id: string) {
  document
    .getElementById(id)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function EnvironmentCard({ env }: { env: EnvironmentCost }) {
  const current = env.current;
  const incomplete = current !== null && current.unpriced > 0;
  const headline =
    env.provider === "onprem" && incomplete && env.cloud_equivalent
      ? env.cloud_equivalent
      : current;
  const lines = [
    headline && headline !== current
      ? `${headline.label} (장비비·전기료 미입력)`
      : current?.label,
    env.cloud_equivalent && headline !== env.cloud_equivalent
      ? `${env.cloud_equivalent.label} ${usd(env.cloud_equivalent.monthly)}`
      : null,
    env.observed_at ? `수집 ${formatAgo(env.observed_at)}` : null,
    ...env.gaps,
  ].filter((line): line is string => Boolean(line));
  return (
    <SummaryCard
      title={env.name}
      label={
        headline === null
          ? { tone: "idle", text: env.observed_at ? "읽지 못함" : "수집 전" }
          : headline === env.cloud_equivalent
            ? { tone: "warn", text: "동급 환산" }
            : headline.unpriced > 0
              ? { tone: "warn", text: `단가 없음 ${headline.unpriced}` }
              : { tone: "ok", text: "공식 단가" }
      }
      value={headline ? usd(headline.monthly) : "-"}
      unit={headline ? "/월" : ""}
      lines={lines}
    />
  );
}

function MigrationCard({
  migration,
  name,
}: {
  migration: MigrationCost;
  name: (id: string) => string;
}) {
  const change = migration.monthly_change;
  return (
    <SummaryCard
      title={`${migration.job_id} · ${name(migration.source_env)} → ${name(migration.target_env)}`}
      label={
        change === null
          ? { tone: "idle", text: "추산 불가" }
          : change > 0
            ? { tone: "warn", text: "월 비용 증가" }
            : { tone: "ok", text: change < 0 ? "월 비용 절감" : "변화 없음" }
      }
      value={signedUsd(change)}
      unit="/월"
      lines={[
        `대상 ${usd(migration.target_monthly_now)} → ${usd(migration.target_monthly_after)}`,
        `원본에서 줄어드는 비용 ${usd(migration.source_released)}`,
        `전환 기간 ${migration.dual_run_days}일 이중 운영 ${usd(migration.dual_run_cost)}`,
        migration.capacity
          ? `대상 남는 용량 ${migration.capacity_fits ? "안에 들어감" : "부족"} · 필요 vCPU ${migration.capacity.need_vcpu} · 메모리 ${migration.capacity.need_memory_gib} GiB`
          : "원본 자원 목록 없음",
        ...migration.gaps,
      ]}
    />
  );
}

function LinesTable({ breakdown }: { breakdown: CostBreakdown }) {
  if (breakdown.lines.length === 0)
    return <p className="pk-empty">비용 항목이 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table cost-table">
        <thead>
          <tr>
            <th>분류</th>
            <th>항목</th>
            <th className="num">수량</th>
            <th className="num">단가/월</th>
            <th className="num">월 비용</th>
            <th>근거</th>
          </tr>
        </thead>
        <tbody>
          {breakdown.lines.map((line, i) => (
            <tr key={`${line.item}-${i}`}>
              <td>{CATEGORY_LABELS[line.category] ?? line.category}</td>
              <td>
                {line.item}
                {line.note && <div className="sub">{line.note}</div>}
              </td>
              <td className="num">{quantityText(line)}</td>
              <td className="num">{unitUsd(line.unit_price)}</td>
              <td className="num">{usd(line.monthly)}</td>
              <td>
                <span className={`cost-basis basis-${line.basis}`}>
                  {BASIS_LABELS[line.basis]}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4}>
              합계
              {breakdown.unpriced > 0 && (
                <span className="sub">
                  {" "}
                  · 단가 없는 {breakdown.unpriced}줄 제외
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

function CategoryBar({ breakdown }: { breakdown: CostBreakdown }) {
  const parts = byCategory(breakdown.lines).filter((p) => p.monthly > 0);
  if (breakdown.monthly <= 0 || parts.length === 0) return null;
  return (
    <div
      className="cost-bar"
      role="img"
      aria-label={`${breakdown.label} 분류별 비중`}
    >
      {parts.map((part) => (
        <span
          key={part.category}
          className={`cost-seg seg-${part.category}`}
          style={{ flexGrow: part.monthly }}
          title={`${CATEGORY_LABELS[part.category] ?? part.category} ${usd(part.monthly)}`}
        >
          {part.monthly / breakdown.monthly >= 0.12 &&
            `${CATEGORY_LABELS[part.category] ?? part.category} ${usd(part.monthly)}`}
        </span>
      ))}
    </div>
  );
}

function EnvironmentDetail({ env }: { env: EnvironmentCost }) {
  const parts = [env.current, env.cloud_equivalent].filter(
    (b): b is CostBreakdown => b !== null,
  );
  return (
    <div className="cost-detail">
      <h3>{env.name}</h3>
      {parts.length === 0 && (
        <p className="pk-empty">
          {env.gaps.join(" · ") || "비용 정보가 없어요."}
        </p>
      )}
      {parts.map((breakdown) => (
        <div key={breakdown.label} className="cost-block">
          <div className="cost-block-head">
            <strong>{breakdown.label}</strong>
            <span>{usd(breakdown.monthly)}/월</span>
          </div>
          <CategoryBar breakdown={breakdown} />
          <LinesTable breakdown={breakdown} />
        </div>
      ))}
      {env.sources.length > 0 && (
        <p className="cost-note">읽은 곳: {env.sources.join(" · ")}</p>
      )}
    </div>
  );
}

function MigrationDetail({
  migration,
  name,
}: {
  migration: MigrationCost;
  name: (id: string) => string;
}) {
  return (
    <div className="cost-detail">
      <h3>
        {migration.job_id} · {name(migration.source_env)} →{" "}
        {name(migration.target_env)}
      </h3>
      {migration.added ? (
        <div className="cost-block">
          <div className="cost-block-head">
            <strong>{migration.added.label}</strong>
            <span>{usd(migration.added.monthly)}/월</span>
          </div>
          <CategoryBar breakdown={migration.added} />
          <LinesTable breakdown={migration.added} />
        </div>
      ) : (
        <p className="pk-empty">
          대상이 온프렘이라 단가 대신 남는 용량으로 판단해요.
        </p>
      )}
      {migration.assumptions.length > 0 && (
        <ul className="cost-assumptions">
          {migration.assumptions.map((text) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function priceSources(overview: CostOverview) {
  const seen = new Map<string, string>();
  for (const env of overview.environments)
    if (env.prices)
      seen.set(
        `${env.prices.provider}-${env.prices.region}`,
        `${env.prices.provider.toUpperCase()} ${env.prices.region} · ${env.prices.source} · ${env.prices.fetched_at.slice(0, 10)} 기준`,
      );
  return [...seen.values()];
}

export function CostsPage() {
  const { data: overview } = useSuspenseQuery(costsQueryOptions());
  const [envChoice, setEnvChoice] = useState("all");
  const name = (id: string) =>
    overview.environments.find((e) => e.env_id === id)?.name ?? id;
  const envs = overview.environments.filter(
    (e) => envChoice === "all" || e.env_id === envChoice,
  );
  const migrations = overview.migrations.filter(
    (m) =>
      envChoice === "all" ||
      m.source_env === envChoice ||
      m.target_env === envChoice,
  );
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
          { id: "environments", label: "환경별" },
          { id: "migrations", label: "이전 추산" },
          { id: "details", label: "상세" },
        ]}
        active="environments"
        onTab={scrollTo}
      />
      <ScopeFilterRow>
        <FilterSelect
          label="환경"
          value={envChoice}
          onChange={setEnvChoice}
          options={[
            { value: "all", label: "전체" },
            ...overview.environments.map((e) => ({
              value: e.env_id,
              label: e.name,
            })),
          ]}
        />
      </ScopeFilterRow>
      <Section
        id="environments"
        title="환경별 월 비용"
        description="지금 떠 있는 자원 × 공식 온디맨드 단가 (USD)"
      >
        <div className="pk-cards">
          {envs.map((env) => (
            <EnvironmentCard key={env.env_id} env={env} />
          ))}
        </div>
      </Section>
      <Section
        id="migrations"
        title="이전 비용 추산"
        description="원본 워크로드를 대상에 다시 배치했을 때 월 증감"
        action={{ label: "상세 보기", onClick: () => scrollTo("details") }}
      >
        <div className="pk-cards">
          {migrations.map((migration) => (
            <MigrationCard
              key={migration.job_id}
              migration={migration}
              name={name}
            />
          ))}
        </div>
      </Section>
      <Section id="details" title="상세" description="항목별 수량·단가·근거">
        {envs.map((env) => (
          <EnvironmentDetail key={env.env_id} env={env} />
        ))}
        {migrations.map((migration) => (
          <MigrationDetail
            key={migration.job_id}
            migration={migration}
            name={name}
          />
        ))}
        <ul className="cost-sources">
          {priceSources(overview).map((text) => (
            <li key={text}>가격표: {text}</li>
          ))}
          <li>
            약정 할인·크레딧·세금·데이터 전송·요청 단위 요금(LCU, NAT 처리 GB)은
            넣지 않았어요.
          </li>
        </ul>
      </Section>
    </div>
  );
}
