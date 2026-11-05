import { useQueries, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { migrationHash } from "../../app/routes";
import { SourceTag } from "../../components/Badge";
import { PageActions } from "../../components/PageSlots";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
  SummaryCard,
} from "../../components/PageKit";
import { formatAgo } from "../../lib/format";
import { AppMatrix } from "./EnvironmentTables";
import { JobsTable } from "./JobsTable";
import { LaneChart } from "./LaneChart";
import { fleetQueryOptions, migrationQueryOptions } from "./queries";
import { filterJobs, overviewCards } from "./summary";

type StatusChoice = "all" | "active" | "waiting" | "done";

export function FleetPage({
  onNavigate,
}: {
  onNavigate: (hash: string) => void;
}) {
  const { data: fleet } = useSuspenseQuery(fleetQueryOptions());
  const [envId, setEnvId] = useState("all");
  const [status, setStatus] = useState<StatusChoice>("all");
  const rows = filterJobs(fleet.migrations, envId, status);
  const details = useQueries({
    queries: rows.map((m) => migrationQueryOptions(m.job_id)),
  });
  const jobs = rows.flatMap((m, i) => {
    const detail = details[i]?.data;
    return detail ? [{ jobId: m.job_id, stages: detail.stages }] : [];
  });
  return (
    <div className="pk-page">
      <PageActions>
        <SourceTag source={fleet.source} />
        <span className="updated-at">기준 {formatAgo(fleet.generated_at)}</span>
      </PageActions>
      <PageHeader
        title="마이그레이션"
        tabs={[{ id: "overview", label: "개요" }]}
        active="overview"
      />
      <ScopeFilterRow>
        <FilterSelect
          label="환경"
          value={envId}
          onChange={setEnvId}
          options={[
            { value: "all", label: "전체" },
            ...fleet.environments.map((e) => ({ value: e.id, label: e.name })),
          ]}
        />
        <FilterSelect
          label="작업 상태"
          value={status}
          onChange={(value) => setStatus(value as StatusChoice)}
          options={[
            { value: "all", label: "전체" },
            { value: "active", label: "진행 중" },
            { value: "waiting", label: "승인 대기" },
            { value: "done", label: "완료" },
          ]}
        />
      </ScopeFilterRow>
      <Section
        title="이전 현황"
        description="이전 작업의 쓰기 실패와 데이터 일치"
        action={{
          label: "작업 목록 보기",
          onClick: () =>
            document
              .getElementById("jobs")
              ?.scrollIntoView({ behavior: "smooth", block: "start" }),
        }}
      >
        <div className="pk-cards">
          {overviewCards(rows).map((card) => (
            <SummaryCard
              key={card.title}
              title={card.title}
              label={card.label}
              value={card.value}
              unit={card.unit}
              lines={card.lines}
            />
          ))}
        </div>
        <LaneChart jobs={jobs} now={Date.now()} />
      </Section>
      <Section id="jobs" title="작업" description="이전 작업별 단계와 쓰기량">
        <JobsTable
          fleet={fleet}
          rows={rows}
          onOpen={(jobId) => onNavigate(migrationHash(jobId))}
        />
      </Section>
      <Section
        title="앱 배포 상태"
        description="환경별 앱 배포와 동작 상태"
        action={{ label: "환경에서 보기", href: "#environments" }}
      >
        <AppMatrix environments={fleet.environments} />
      </Section>
    </div>
  );
}
