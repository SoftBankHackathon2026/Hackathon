import { useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { RefreshStatus } from "../../components/RefreshStatus";
import { PageActions } from "../../components/PageSlots";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
} from "../../components/PageKit";
import { StatusDot } from "../../components/StatusDot";
import { fleetQueryOptions } from "../migrations/queries";
import { ArchitectureCanvas } from "./ArchitectureCanvas";
import { ComponentsList } from "./ComponentsList";
import { clusterRailRows } from "./clusterModel";
import { envRailRows, registeredRailRow } from "./model";

function useViewportHeight() {
  const [height, setHeight] = useState(() => window.innerHeight);
  useEffect(() => {
    const onResize = () => setHeight(window.innerHeight);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return height;
}

export function EnvironmentsPage({
  select,
  nodes,
}: {
  select?: { envId: string; id: string };
  nodes?: { envId?: string; node?: string };
}) {
  const { data: fleet } = useSuspenseQuery(fleetQueryOptions());
  const [envId, setEnvId] = useState("all");
  const environments = fleet.environments.filter(
    (e) => envId === "all" || e.id === envId,
  );
  const viewport = useViewportHeight();
  const issues = environments.flatMap((env) =>
    env.issues.map((issue) => ({ env, issue })),
  );
  return (
    <div className="pk-page">
      <PageActions>
        <RefreshStatus source={fleet.source} at={fleet.generated_at} />
      </PageActions>
      <PageHeader
        title="환경"
        tabs={[{ id: "architecture", label: "아키텍처" }]}
        active="architecture"
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
      </ScopeFilterRow>
      <div className="ux-page embedded">
        <aside className="ux-rail" aria-label="환경 요약">
          {environments.map((env) => (
            <section key={env.id} className="ux-block">
              <h2 className="ux-name">{env.name}</h2>
              <dl className="ux-kv">
                {[
                  ...envRailRows(env).map((row) => ({
                    ...row,
                    href: undefined as string | undefined,
                    title: undefined as string | undefined,
                  })),
                  ...clusterRailRows(env),
                  ...[registeredRailRow(env)].filter((row) => row !== null),
                ].map((row) => {
                  const body = row.tone ? (
                    <StatusDot tone={row.tone}>{row.value}</StatusDot>
                  ) : (
                    row.value
                  );
                  return (
                    <div key={row.label}>
                      <dt>{row.label}</dt>
                      <dd title={row.title}>
                        {row.href ? (
                          <a className="ux-link" href={row.href}>
                            {body}
                          </a>
                        ) : (
                          body
                        )}
                      </dd>
                    </div>
                  );
                })}
              </dl>
            </section>
          ))}
          <section className="ux-block ux-divided">
            <h2 className="ux-label">경고</h2>
            {issues.length === 0 ? (
              <p className="ux-muted">경고가 없어요.</p>
            ) : (
              <ul className="ux-issues">
                {issues.map(({ env, issue }, i) => (
                  <li key={`${env.id}-${i}`}>
                    <StatusDot
                      tone={issue.severity === "critical" ? "bad" : "warn"}
                    >
                      {issue.title}
                    </StatusDot>
                    <span className="ux-small">
                      {env.name}
                      {issue.detail ? `, ${issue.detail}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
        <div className="ux-main">
          <ArchitectureCanvas
            environments={environments}
            migrations={fleet.migrations}
            select={select}
            nodes={nodes}
            height={Math.max(520, Math.round(viewport * 0.7))}
          />
          <section className="ux-components" aria-label="구성 요소">
            <ComponentsList environments={environments} />
          </section>
        </div>
      </div>
    </div>
  );
}
