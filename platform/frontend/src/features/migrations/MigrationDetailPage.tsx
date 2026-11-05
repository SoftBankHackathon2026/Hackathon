import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { SourceTag } from "../../components/Badge";
import { StateLabel } from "../../components/StatusDot";
import { DashSection } from "../../components/DashSection";
import { PageActions, PageTabs } from "../../components/PageSlots";
import { Segmented } from "../../components/Segmented";
import { formatAgo } from "../../lib/format";
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
import { pollInterval, STATUS_LABELS, statusTone } from "./stages";
import { lifetimeMinutes, lifetimeSpan, lifetimeWindow } from "./timeline";
import { StageLog } from "./StageLog";
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
  const counts = {
    source:
      worker?.committed_by_target[summary.data_source] ??
      summary.writes.committed_source,
    target:
      worker?.committed_by_target[summary.data_target] ??
      summary.writes.committed_target,
  };

  return (
    <div className="dash-page">
      <PageTabs>
        <StateLabel tone={statusTone(summary.status)}>
          {STATUS_LABELS[summary.status]}
        </StateLabel>
      </PageTabs>
      <PageActions>
        <Segmented
          label="조회 범위"
          value={minutes}
          options={RANGES}
          onChange={setMinutes}
        />
        <SourceTag source={fleet?.source} />
        <span className="updated-at">갱신 {formatAgo(summary.updated_at)}</span>
      </PageActions>
      <p className="job-meta">
        {sourceName} → {targetName} · 데이터 {summary.data_source} →{" "}
        {summary.data_target}
      </p>
      <div className="detail-layout">
        <EventRail events={events.data} />
        <div className="detail-main">
          <p className="headline">
            <span>
              {sentence.parts.map((part, i) =>
                part.bad ? (
                  <strong key={i} className="text-bad">
                    {part.text}
                  </strong>
                ) : (
                  <span key={i}>{part.text}</span>
                ),
              )}
            </span>
          </p>
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
              <p className="empty-line">불러오는 중이에요…</p>
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
            ) : (
              !series.isError && (
                <p className="empty-line">불러오는 중이에요…</p>
              )
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
                <header className="graph-head">
                  <h3>데이터 검증</h3>
                </header>
                <VerifyTable
                  pre={detail.verify.pre}
                  post={detail.verify.post}
                />
              </section>
              <section aria-label="정책 게이트">
                <header className="graph-head">
                  <h3>정책 게이트</h3>
                </header>
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
