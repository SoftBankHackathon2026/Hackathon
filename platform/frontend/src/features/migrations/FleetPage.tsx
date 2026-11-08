import { migrationHash } from "../../app/routes";
import { useQueries, useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { AttentionStrip } from "../../components/AttentionStrip";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
} from "../../components/PageKit";
import { PageActions } from "../../components/PageSlots";
import { RefreshStatus } from "../../components/RefreshStatus";
import { carriedNotice, dropNotice } from "./deletion";
import { AppMatrix } from "./EnvironmentTables";
import { appProblems, attentionItems } from "./flow";
import { JobsTable } from "./JobsTable";
import { LaneChart } from "./LaneChart";
import { buildLanes } from "./lanes";
import { MigrationFlow } from "./MigrationFlow";
import { fleetQueryOptions, migrationQueryOptions } from "./queries";
import { filterJobs } from "./summary";

type StatusChoice = "all" | "active" | "waiting" | "done";

function scrollTo(id: string) {
  document
    .getElementById(id)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/**
 * 마이그레이션 개요. 위에서부터 먼저 볼 것(타일) → 이전 흐름(어떻게·어디까지) → 작업 표 → 앱 상태.
 */
export function FleetPage({
  onNavigate,
}: {
  onNavigate: (hash: string) => void;
}) {
  const { data: fleet } = useSuspenseQuery(fleetQueryOptions());
  const [envId, setEnvId] = useState("all");
  const [status, setStatus] = useState<StatusChoice>("all");
  const rows = filterJobs(fleet.migrations, envId, status);
  const [notice, setNotice] = useState<string | null>(carriedNotice);
  useEffect(dropNotice, []);
  const scoped = fleet.environments.filter(
    (e) => envId === "all" || e.id === envId,
  );
  const queries = useQueries({
    queries: rows.map((m) => migrationQueryOptions(m.job_id)),
  });
  const details = Object.fromEntries(
    rows.map((m, i) => [m.job_id, queries[i]?.data]),
  );
  const lanes = rows.flatMap((m) => {
    const detail = details[m.job_id];
    return detail ? [{ jobId: m.job_id, stages: detail.stages }] : [];
  });
  const hasLanes = buildLanes(lanes, Date.now()) !== null;
  const problems = appProblems(scoped).length;
  const onTile = (id: string) => {
    if (id === "apps") return scrollTo("apps");
    if (id === "active") setStatus("active");
    else if (id === "approval") setStatus("waiting");
    else setStatus("all");
    scrollTo("jobs");
  };
  return (
    <div className="pk-page">
      <PageActions>
        <RefreshStatus source={fleet.source} at={fleet.generated_at} />
      </PageActions>
      <PageHeader
        title="마이그레이션"
        actions={
          <a className="pk-button" href="#prepare">
            마이그레이션 준비
          </a>
        }
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
      <AttentionStrip items={attentionItems(rows, scoped)} onSelect={onTile} />
      <Section
        id="flow"
        title="이전 흐름"
        meta={`작업 ${rows.length}개`}
        action={{ label: "준비 방법 보기", href: "#prepare" }}
      >
        <MigrationFlow migrations={rows} environments={scoped} />
        {hasLanes && <LaneChart jobs={lanes} now={Date.now()} />}
      </Section>
      <Section id="jobs" title="작업" meta={rows.length}>
        <JobsTable
          fleet={fleet}
          rows={rows}
          onOpen={(jobId) => onNavigate(migrationHash(jobId))}
          notice={notice}
          onPick={() => setNotice(null)}
          onDeleted={(_ids, text) => setNotice(text)}
        />
      </Section>
      <Section
        id="apps"
        title="앱 배포 상태"
        meta={problems > 0 ? `비정상 앱 ${problems}개` : "모두 정상"}
        action={{ label: "환경 보기", href: "#environments" }}
      >
        <AppMatrix environments={scoped} />
      </Section>
    </div>
  );
}
