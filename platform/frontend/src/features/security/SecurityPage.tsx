import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { SourceTag } from "../../components/Badge";
import { PageActions } from "../../components/PageSlots";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
} from "../../components/PageKit";
import { formatAgo } from "../../lib/format";
import { fleetQueryOptions } from "../migrations/queries";
import { ComplianceOverview } from "./ComplianceOverview";
import { FacetPanel } from "./FacetPanel";
import { FindingsTable } from "./FindingsTable";
import { postureQueryOptions } from "./queries";
import { StandardsTable } from "./StandardsTable";
import { useFindingFilters } from "./useFindingFilters";

function scrollTo(id: string) {
  document
    .getElementById(id)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

export function SecurityPage({ control }: { control?: string }) {
  const { data: posture } = useSuspenseQuery(postureQueryOptions());
  const fleet = useQuery(fleetQueryOptions()).data;
  const { filters, set, reset } = useFindingFilters(posture, control);
  const allEnvIds = posture.environments.map((e) => e.env_id);
  const scoped = posture.environments.filter((e) =>
    filters.env.includes(e.env_id),
  );
  const envChoice = filters.env.length === 1 ? filters.env[0] : "all";
  return (
    <div className="pk-page">
      <PageActions>
        <SourceTag source={posture.source} />
        <span className="updated-at">
          평가 {formatAgo(posture.evaluated_at)}
        </span>
      </PageActions>
      <PageHeader
        title="보안"
        tabs={[
          { id: "overview", label: "개요" },
          { id: "findings", label: "문제" },
          { id: "standards", label: "표준" },
        ]}
        active="overview"
        onTab={(id) =>
          scrollTo(
            id === "overview"
              ? "compliance"
              : id === "findings"
                ? "findings"
                : "standards",
          )
        }
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
      <Section
        id="compliance"
        title="준수 현황"
        description="환경별 보안 기준 준수율"
        action={{
          label: "문제 목록 보기",
          onClick: () => scrollTo("findings"),
        }}
      >
        <ComplianceOverview posture={posture} environments={scoped} />
      </Section>
      <Section
        id="standards"
        title="표준별 준수"
        description="표준별 통제 통과율"
        action={{
          label: "문제 목록 보기",
          onClick: () => scrollTo("findings"),
        }}
      >
        <StandardsTable
          standards={posture.standards}
          controls={posture.controls}
          environments={scoped}
          onSelect={(id) => {
            set({ standard: [id] });
            scrollTo("findings");
          }}
        />
      </Section>
      <Section
        id="findings"
        title="보안 문제"
        description="실패하거나 미확인인 통제"
      >
        <div className="pk-list">
          <FacetPanel
            posture={posture}
            filters={filters}
            onChange={set}
            onReset={reset}
          />
          <div className="pk-list-main">
            <FindingsTable
              key={control ?? ""}
              posture={posture}
              fleetEnvs={fleet?.environments ?? []}
              filters={filters}
              focus={control}
            />
          </div>
        </div>
      </Section>
    </div>
  );
}
