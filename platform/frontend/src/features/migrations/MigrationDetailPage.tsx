import { Skeleton } from "../../components/QueryBoundary";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { StateLabel } from "../../components/StatusDot";
import { DashSection } from "../../components/DashSection";
import { PageActions } from "../../components/PageSlots";
import { FilterSelect, PageHeader } from "../../components/PageKit";
import { RefreshStatus } from "../../components/RefreshStatus";
import { StatTiles } from "../../components/StatTiles";
import { formatCount, formatRatio, formatSeconds } from "../../lib/format";
import { CdcPath } from "./CdcPath";
import { EnvironmentGraphs, EnvironmentTable } from "./EnvironmentWrites";
import { EventRail } from "./EventRail";
import { envShortName } from "./fleetView";
import { headline } from "./headline";
import {
  eventsQueryOptions,
  fleetQueryOptions,
  migrationQueryOptions,
  seriesQueryOptions,
} from "./queries";
import {
  pollInterval,
  stageTitle,
  STATUS_LABELS,
  stepLabel,
  statusTone,
} from "./stages";
import { lifetimeMinutes, lifetimeSpan, lifetimeWindow } from "./timeline";
import { StageLog } from "./StageLog";
import { failedWrites } from "./summary";
import { TransitionTimeline } from "./TransitionTimeline";
import { GatesTable, VerifyTable } from "./VerifyGates";

const ALL = 0;

const RANGES = [
  { value: ALL, label: "작업 전체" },
  { value: 15, label: "15분" },
  { value: 60, label: "1시간" },
  { value: 360, label: "6시간" },
  { value: 1440, label: "24시간" },
];

export function MigrationDetailPage({ jobId }: { jobId: string }) {
  const { data: detail } = useSuspenseQuery(migrationQueryOptions(jobId));
  const fleet = useQuery(fleetQueryOptions()).data;
  const [minutes, setMinutes] = useState(ALL);
  const { summary } = detail;
  const interval = pollInterval(summary.status);
  const rangeSeries = useQuery({
    ...seriesQueryOptions(jobId, interval, minutes || 60),
    enabled: minutes !== ALL,
  });
  const lifetime = useQuery(
    seriesQueryOptions(
      jobId,
      interval,
      lifetimeMinutes(
        lifetimeSpan(detail.stages)?.from ?? Date.now() - 3600000,
        Date.now(),
      ),
    ),
  );
  const whole =
    lifetime.data && lifetimeWindow(lifetime.data.points, detail.stages);
  const series =
    minutes === ALL
      ? {
          data:
            lifetime.data && whole
              ? { ...lifetime.data, points: whole.points }
              : undefined,
          isError: lifetime.isError,
        }
      : rangeSeries;
  const events = useQuery(eventsQueryOptions(jobId, interval));
  const environments = fleet?.environments ?? [];
  const sourceName = envShortName(environments, summary.source_env);
  const targetName = envShortName(environments, summary.target_env);
  const sentence = headline(detail, { source: sourceName, target: targetName });
  const worker = detail.worker;
  const failed = failedWrites(summary);
  const counts = {
    source:
      worker?.committed_by_target[summary.data_source] ??
      summary.writes.committed_source,
    target:
      worker?.committed_by_target[summary.data_target] ??
      summary.writes.committed_target,
  };

  return (
    <div className="pk-page">
      <PageActions>
        <FilterSelect
          label="기간"
          value={String(minutes)}
          options={RANGES.map((r) => ({
            value: String(r.value),
            label: r.label,
          }))}
          onChange={(value) => setMinutes(Number(value))}
        />
        <RefreshStatus source={fleet?.source} at={summary.updated_at} />
      </PageActions>
      <PageHeader
        title={summary.job_id}
        status={
          <StateLabel tone={statusTone(summary.status)}>
            {STATUS_LABELS[summary.status]}
          </StateLabel>
        }
        meta={
          <>
            {sourceName} → {targetName}, 데이터 {summary.data_source} →{" "}
            {summary.data_target}, {stageTitle(summary.stage)}{" "}
            {summary.stage_index}/{summary.stage_count}단계
          </>
        }
      />
      {sentence.parts.some((part) => part.bad) && (
        <p className="pk-alert" role="status">
          {sentence.parts.map((part, i) =>
            part.bad ? (
              <strong key={i}>{part.text}</strong>
            ) : (
              <span key={i}>{part.text}</span>
            ),
          )}
        </p>
      )}
      <StatTiles
        label="작업 요약"
        items={[
          {
            id: "stage",
            label: "단계",
            value: stepLabel(summary),
            note: stageTitle(summary.stage),
            meter: {
              ratio:
                summary.status === "completed"
                  ? 1
                  : summary.status === "pending"
                    ? 0
                    : summary.stage_index / summary.stage_count,
              tone: statusTone(summary.status) === "bad" ? "bad" : "running",
            },
          },
          {
            id: "source",
            label: `원본 쓰기: ${sourceName}`,
            value: formatCount(counts.source),
            unit: "건",
          },
          {
            id: "target",
            label: `대상 반영: ${targetName}`,
            value: formatRatio(counts.target, counts.source),
            note: `${formatCount(counts.target)}건`,
          },
          {
            id: "failed",
            label: "실패한 쓰기",
            value: formatCount(failed),
            unit: "건",
            tone: failed > 0 ? "bad" : "none",
            dim: failed === 0,
          },
          {
            id: "pause",
            label: "쓰기 중단",
            value: formatSeconds(summary.downtime.write_pause_seconds),
            note:
              summary.downtime.write_pause_seconds === null
                ? "전환 전"
                : "원본 쓰기 차단 구간",
          },
          {
            id: "verify",
            label: "데이터 불일치",
            value:
              summary.verify_mismatch === null
                ? "-"
                : formatCount(summary.verify_mismatch),
            unit: summary.verify_mismatch === null ? undefined : "건",
            tone: (summary.verify_mismatch ?? 0) > 0 ? "bad" : "none",
            note:
              summary.verify_mismatch === null ? "검증 전" : "원본/대상 비교",
          },
        ]}
      />
      <div className="detail-layout">
        <EventRail events={events.data} />
        <div className="detail-main">
          <DashSection title="전환 타임라인">
            {lifetime.data ? (
              <TransitionTimeline
                allPoints={lifetime.data.points}
                stages={detail.stages}
              />
            ) : lifetime.isError ? (
              <p className="info-line" role="alert">
                시계열을 불러오지 못했어요. 자동으로 다시 시도해요.
              </p>
            ) : (
              <Skeleton rows={2} />
            )}
          </DashSection>
          <DashSection title="환경별 쓰기">
            {series.data ? (
              <EnvironmentGraphs
                series={series.data}
                detail={detail}
                sourceName={sourceName}
                targetName={targetName}
              />
            ) : minutes === ALL && lifetime.data ? null : (
              !series.isError && <Skeleton rows={2} />
            )}
            <EnvironmentTable
              series={lifetime.data}
              detail={detail}
              sourceName={sourceName}
              targetName={targetName}
            />
          </DashSection>
          <DashSection title="CDC 경로">
            <CdcPath
              detail={detail}
              names={{ source: sourceName, target: targetName }}
              counts={counts}
            />
          </DashSection>
          <DashSection title="검증과 정책 게이트">
            <div className="two-col">
              <section aria-label="데이터 검증">
                <h3 className="sub-head">데이터 검증</h3>
                <VerifyTable
                  pre={detail.verify.pre}
                  post={detail.verify.post}
                />
              </section>
              <section aria-label="정책 게이트">
                <h3 className="sub-head">정책 게이트</h3>
                <GatesTable gates={detail.gates} />
              </section>
            </div>
          </DashSection>
          <DashSection title="단계 기록" defaultOpen={false}>
            <StageLog stages={detail.stages} />
          </DashSection>
        </div>
      </div>
    </div>
  );
}
