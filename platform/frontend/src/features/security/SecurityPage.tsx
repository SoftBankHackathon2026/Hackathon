import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { FilterTiles } from "../../components/FilterTiles";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
} from "../../components/PageKit";
import { PageActions } from "../../components/PageSlots";
import { RefreshStatus } from "../../components/RefreshStatus";
import { fleetQueryOptions } from "../migrations/queries";
import { SecurityOverview } from "./SecurityOverview";
import { ALL_SEVERITIES, defaultFilters, revealFilters } from "./explorer";
import { FacetPanel } from "./FacetPanel";
import { FindingsTable } from "./FindingsTable";
import {
  activeTile,
  inboxCounts,
  parityIds,
  tilePreset,
  type TileId,
} from "./inbox";
import { postureQueryOptions } from "./queries";
import { StandardsTable } from "./StandardsTable";
import { useFindingFilters } from "./useFindingFilters";

type Tab = "overview" | "findings" | "standards";

/**
 * 보안. 개요 탭: 위젯 대시보드(Wiz 방식, 환경별 점수 포함).
 * 문제 탭: 필터 타일 한 줄 → 우선순위 목록(행 → 오른쪽 패널, Datadog Security Inbox 방식). 표준 탭: 펼치는 표준 표.
 */
export function SecurityPage({ control }: { control?: string }) {
  const { data: posture } = useSuspenseQuery(postureQueryOptions());
  const fleet = useQuery(fleetQueryOptions()).data;
  const fleetEnvs = fleet?.environments ?? [];
  const [tab, setTab] = useState<Tab>(control ? "findings" : "overview");
  // 열 문제 요청. 같은 문제를 다시 열어도 패널이 열리도록 번호를 올린다.
  const [focusReq, setFocusReq] = useState({ id: control, n: 0 });
  const { filters, set, reset } = useFindingFilters(posture, control);
  const openControl = (id: string) => {
    const patch = revealFilters(
      filters,
      posture.controls.find((c) => c.id === id),
    );
    if (patch) set(patch);
    setTab("findings");
    setFocusReq((prev) => ({ id, n: prev.n + 1 }));
  };
  useEffect(() => {
    if (control) openControl(control);
    // 주소의 문제가 바뀔 때만 연다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [control]);
  const allEnvIds = posture.environments.map((e) => e.env_id);
  const scoped = posture.environments.filter((e) =>
    filters.env.includes(e.env_id),
  );
  const envChoice = filters.env.length === 1 ? filters.env[0] : "all";
  const counts = inboxCounts(posture, filters.env);
  const active = activeTile(filters, posture, filters.env);
  const targets = parityIds(posture, filters.env)
    .targets.map(
      (id) =>
        posture.environments.find((e) => e.env_id === id)?.name.split(" (")[0],
    )
    .join(", ");
  const onTile = (id: string) => {
    if (active === id) {
      const base = defaultFilters(
        allEnvIds,
        posture.standards.map((s) => s.id),
      );
      set({ severity: base.severity, status: base.status, ids: undefined });
    } else set(tilePreset(id as TileId, posture, filters.env));
  };
  return (
    <div className="pk-page">
      <PageActions>
        <RefreshStatus
          source={posture.source}
          at={posture.evaluated_at}
          verb="평가"
        />
      </PageActions>
      <PageHeader
        title="보안"
        tabs={[
          { id: "overview", label: "개요" },
          { id: "findings", label: "발견 사항" },
          { id: "standards", label: "표준" },
        ]}
        active={tab}
        onTab={(id) => setTab(id as Tab)}
      />
      <ScopeFilterRow>
        <FilterSelect
          label="환경"
          value={envChoice}
          onChange={(value) =>
            set({ env: value === "all" ? allEnvIds : [value] })
          }
          options={[
            { value: "all", label: "전체" },
            ...posture.environments.map((e) => ({
              value: e.env_id,
              label: e.name,
            })),
          ]}
        />
      </ScopeFilterRow>
      {tab === "overview" && (
        <SecurityOverview
          posture={posture}
          environments={scoped}
          fleetEnvs={fleetEnvs}
          onSeverity={(severity) => {
            set({ severity: [severity], status: ["fail"], ids: undefined });
            setTab("findings");
          }}
          onCategory={(category) => {
            set({
              category: [category],
              severity: [...ALL_SEVERITIES],
              status: ["fail"],
              ids: undefined,
            });
            setTab("findings");
          }}
          onControl={openControl}
          onControls={(ids) => {
            set({ ids, severity: [...ALL_SEVERITIES], status: ["fail"] });
            setTab("findings");
          }}
          onParity={() => {
            set(tilePreset("regressed", posture, filters.env));
            setTab("findings");
          }}
          onStandards={() => setTab("standards")}
          onFindings={() => setTab("findings")}
        />
      )}
      {tab === "findings" && (
        <>
          <FilterTiles
            active={active}
            onSelect={onTile}
            groups={[
              {
                title: "심각도",
                note: "실패한 통제",
                tiles: [
                  {
                    id: "critical",
                    label: "치명",
                    value: counts.critical,
                    tone: "bad",
                  },
                  {
                    id: "high",
                    label: "높음",
                    value: counts.high,
                    tone: "bad",
                  },
                ],
              },
              {
                title: "결과",
                tiles: [
                  {
                    id: "fail",
                    label: "실패",
                    value: counts.fail,
                    tone: "bad",
                  },
                  {
                    id: "unknown",
                    label: "미확인",
                    value: counts.unknown,
                    tone: "warn",
                  },
                ],
              },
              {
                title: "전환 영향",
                note: targets ? `도착 ${targets}` : undefined,
                tiles: [
                  {
                    id: "regressed",
                    label: "새로 실패",
                    value: counts.regressed,
                    tone: "bad",
                  },
                  {
                    id: "improved",
                    label: "새로 통과",
                    value: counts.improved,
                    tone: "ok",
                  },
                ],
              },
            ]}
          />
          <Section id="findings" title="발견 사항">
            <div className="pk-list">
              <FacetPanel
                posture={posture}
                filters={filters}
                onChange={set}
                onReset={reset}
              />
              <div className="pk-list-main">
                <FindingsTable
                  key={`${focusReq.id ?? ""}-${focusReq.n}`}
                  posture={posture}
                  fleetEnvs={fleetEnvs}
                  filters={filters}
                  focus={focusReq.id}
                />
              </div>
            </div>
          </Section>
        </>
      )}
      {tab === "standards" && (
        <>
          <Section
            id="standards"
            title="표준별 준수"
            meta={`${posture.standards.length}개, 낮은 점수부터`}
          >
            <StandardsTable
              standards={posture.standards}
              controls={posture.controls}
              environments={scoped}
              onSelect={(id) => {
                set({ standard: [id] });
                setTab("findings");
              }}
              onControl={openControl}
            />
          </Section>
        </>
      )}
    </div>
  );
}
