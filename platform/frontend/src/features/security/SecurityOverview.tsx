import type { FleetEnvironment } from "../../api/migrations";
import type { Category, Posture, Severity } from "../../api/security";
import { ProviderLogo } from "../../components/ProviderLogo";
import { Widget } from "../../components/Widget";
import { CategoryIcon } from "./CategoryIcon";
import { EnvScores } from "./EnvScores";
import { NodeIcon } from "./ImpactPath";
import { parityIds } from "./inbox";
import {
  categoryTotals,
  exposedEntries,
  severityTotals,
  topFailing,
} from "./overview";
import { CATEGORY_LABELS, SEVERITY_LABELS, standardId } from "./posture";
import { SeverityMeter } from "./SeverityMeter";

/**
 * 보안 개요 탭(Wiz 대시보드 방식). 위젯을 누르면 문제·표준 탭으로 이어진다.
 * 1행: 심각도별 문제 · 환경별 점수 · 가장 시급한 문제
 * 2행: 인터넷에 노출된 진입점 · 전환 영향
 * 3행: 분류별 문제 · 표준별 준수율
 */
export function SecurityOverview({
  posture,
  environments,
  fleetEnvs,
  onSeverity,
  onCategory,
  onControl,
  onControls,
  onParity,
  onStandards,
  onFindings,
}: {
  posture: Posture;
  environments: Posture["environments"];
  fleetEnvs: FleetEnvironment[];
  onSeverity: (severity: Severity) => void;
  onCategory: (category: Category) => void;
  onControl: (id: string) => void;
  onControls: (ids: string[]) => void;
  onParity: () => void;
  onStandards: () => void;
  onFindings: () => void;
}) {
  const envIds = environments.map((e) => e.env_id);
  const provider = (id: string) => fleetEnvs.find((e) => e.id === id)?.provider;
  const envName = (id: string) =>
    (posture.environments.find((e) => e.env_id === id)?.name ?? id).split(
      " (",
    )[0];
  const severities = severityTotals(posture.controls, envIds);
  const top = topFailing(posture.controls, envIds);
  const failingTotal = severities.reduce((n, s) => n + s.count, 0);
  const exposed = exposedEntries(posture, fleetEnvs, envIds).slice(0, 5);
  const parity = parityIds(posture, envIds);
  const regressed = parity.regressed
    .map((id) => posture.controls.find((c) => c.id === id))
    .filter((c): c is NonNullable<typeof c> => c !== undefined);
  const categories = categoryTotals(posture.controls, envIds);
  const standards = posture.standards
    .map((standard) => {
      const members = posture.controls.filter((c) =>
        c.standard_refs.some((ref) => standardId(ref) === standard.id),
      );
      const statuses = members.flatMap((c) =>
        envIds.map((id) => c.results[id]?.status),
      );
      const pass = statuses.filter((s) => s === "pass").length;
      const fail = statuses.filter((s) => s === "fail").length;
      return {
        standard,
        rate: pass + fail > 0 ? Math.round((pass / (pass + fail)) * 100) : null,
      };
    })
    .filter((row) => row.rate !== null)
    .sort((a, b) => (a.rate ?? 0) - (b.rate ?? 0));

  return (
    <div className="sec-grid">
      <Widget
        title="심각도별 실패 통제"
        sub={`실패한 통제 ${failingTotal}개`}
        className="w-sev"
      >
        <ul className="sev-stack">
          {severities.map(({ severity, count }) => (
            <li key={severity}>
              <button
                type="button"
                className={`sev-stack-item sev-${severity}${count === 0 ? " zero" : ""}`}
                onClick={() => onSeverity(severity)}
              >
                <b>{count}</b>
                <span>{SEVERITY_LABELS[severity]}</span>
              </button>
            </li>
          ))}
        </ul>
      </Widget>

      <Widget
        title="환경별 보안 점수"
        sub="낮은 점수부터"
        className="w-scores"
        action={{ label: "표준 보기", onClick: onStandards }}
      >
        <EnvScores environments={environments} fleetEnvs={fleetEnvs} compact />
      </Widget>

      <Widget
        title="먼저 고칠 통제"
        sub="심각도, 실패 환경 수 순"
        className="w-top"
        action={{ label: `전체 ${failingTotal}개`, onClick: onFindings }}
      >
        {top.length === 0 ? (
          <p className="pk-empty">실패한 통제가 없어요.</p>
        ) : (
          <ul className="w-list">
            {top.map(({ control, envs, resources }) => (
              <li key={control.id}>
                <button
                  type="button"
                  className="w-row"
                  onClick={() => onControl(control.id)}
                >
                  <CategoryIcon category={control.category} />
                  <span className="rule-text">
                    <span className="rule-title">{control.title}</span>
                    <span className="rule-sub">
                      {control.id}, 리소스{" "}
                      {control.scope === "environment"
                        ? "환경 단위"
                        : `${resources}개`}
                    </span>
                  </span>
                  <span className="env-hits">
                    {envs.map((id) => (
                      <ProviderLogo key={id} provider={provider(id)} />
                    ))}
                  </span>
                  <SeverityMeter severity={control.severity} label={false} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Widget>

      <Widget
        title="인터넷에 노출된 진입점"
        sub="실패한 통제의 영향 경로에 들어간 진입점"
        className="w-exposed"
      >
        {exposed.length === 0 ? (
          <p className="pk-empty">노출된 진입점이 없어요.</p>
        ) : (
          <table className="pk-table w-table">
            <thead>
              <tr>
                <th>진입점</th>
                <th>환경</th>
                <th className="num">뒤의 워크로드</th>
                <th className="num">닿는 데이터</th>
                <th>걸린 통제</th>
              </tr>
            </thead>
            <tbody>
              {exposed.map((row) => (
                <tr
                  key={`${row.envId}:${row.entry.id}`}
                  className="clickable"
                  onClick={() => onControls(row.controls.map((c) => c.id))}
                >
                  <td>
                    <span className="rule-cell">
                      <NodeIcon layer="entry" kind={row.entry.kind} />
                      <span className="rule-text">
                        <span className="rule-title">{row.entry.name}</span>
                        <span className="rule-sub">
                          {row.entry.hit ? "실패한 진입점" : "연결된 진입점"}
                        </span>
                      </span>
                    </span>
                  </td>
                  <td>
                    <span className="pk-path">
                      <ProviderLogo provider={provider(row.envId)} />
                      {envName(row.envId)}
                    </span>
                  </td>
                  <td className="num">{row.workloads}</td>
                  <td className="num">{row.data}</td>
                  <td>
                    <span className="w-controls">
                      <SeverityMeter severity={row.worst} label={false} />
                      {row.controls.length}개
                      <span className="sub">{row.controls[0]?.title}</span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Widget>

      <Widget
        title="전환 영향"
        sub={
          parity.targets.length > 0
            ? `도착 ${parity.targets.map(envName).join(", ")}`
            : undefined
        }
        className="w-parity"
        action={
          regressed.length > 0
            ? { label: "목록에서 보기", onClick: onParity }
            : undefined
        }
      >
        <div className="w-parity-nums">
          <div className={regressed.length > 0 ? "bad" : undefined}>
            <b>{regressed.length}</b>
            <span>새로 실패</span>
          </div>
          <div className="ok">
            <b>{parity.improved.length}</b>
            <span>새로 통과</span>
          </div>
        </div>
        <ul className="w-list">
          {regressed.slice(0, 4).map((control) => (
            <li key={control.id}>
              <button
                type="button"
                className="w-row"
                onClick={() => onControl(control.id)}
              >
                <CategoryIcon category={control.category} />
                <span className="rule-text">
                  <span className="rule-title">{control.title}</span>
                  <span className="rule-sub">{control.id}</span>
                </span>
                <SeverityMeter severity={control.severity} label={false} />
              </button>
            </li>
          ))}
        </ul>
      </Widget>

      <Widget
        title="분류별 실패 통제"
        sub="실패한 통제 / 전체 통제"
        className="w-cat"
      >
        <ul className="cat-grid">
          {categories.map(({ category, count, total }) => (
            <li key={category}>
              <button
                type="button"
                className={`cat-item${count === 0 ? " zero" : ""}`}
                onClick={() => onCategory(category)}
              >
                <CategoryIcon category={category} />
                <span>
                  <b>{count}</b>
                  <small>/{total}</small>
                </span>
                <span className="cat-name">{CATEGORY_LABELS[category]}</span>
              </button>
            </li>
          ))}
        </ul>
      </Widget>

      <Widget
        title="표준별 준수율"
        sub="낮은 순"
        className="w-std"
        action={{ label: "표준 보기", onClick: onStandards }}
      >
        <ul className="w-std-list">
          {standards.map(({ standard, rate }) => (
            <li key={standard.id}>
              <span className="rule-text">
                <span className="rule-title">{standard.name}</span>
                <span className="rule-sub">{standard.id}</span>
              </span>
              <span className="posture">
                <span className="posture-track" aria-hidden="true">
                  <span
                    className={`band-${(rate ?? 0) >= 80 ? "high" : (rate ?? 0) >= 50 ? "mid" : "low"}`}
                    style={{ width: `${rate}%` }}
                  />
                </span>
                <b>{rate}%</b>
              </span>
            </li>
          ))}
        </ul>
      </Widget>
    </div>
  );
}
