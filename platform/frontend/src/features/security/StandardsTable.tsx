import { Fragment, useState } from "react";
import type { Control, EnvironmentScore, Standard } from "../../api/security";
import { StatusLabel } from "../../components/PageKit";
import type { Tone } from "../migrations/stages";
import { CategoryIcon } from "./CategoryIcon";
import {
  BAND_LABELS,
  COVERAGE_BAND_LABELS,
  COVERAGE_LABEL,
  isCoverageStandard,
  scoreBand,
  standardId,
} from "./posture";
import { SeverityMeter } from "./SeverityMeter";

const BAND_TONES: Record<string, Tone> = {
  high: "ok",
  mid: "warn",
  low: "bad",
};

function tally(controls: Control[], envIds: string[]) {
  const statuses = controls.flatMap((c) =>
    envIds.map((id) => c.results[id]?.status),
  );
  const pass = statuses.filter((s) => s === "pass").length;
  const fail = statuses.filter((s) => s === "fail").length;
  const unknown = statuses.filter((s) => s === "unknown").length;
  return {
    pass,
    fail,
    unknown,
    rate: pass + fail > 0 ? Math.round((pass / (pass + fail)) * 100) : null,
  };
}

function Counts({ pass, fail }: { pass: number; fail: number }) {
  return (
    <span className="pf-counts">
      <span className="pf-pass">
        <i aria-hidden="true" />
        {pass}
      </span>
      <span className={`pf-fail${fail > 0 ? "" : " zero"}`}>
        <i aria-hidden="true" />
        {fail}
      </span>
    </span>
  );
}

function Posture({ rate, caption }: { rate: number | null; caption?: string }) {
  return (
    <span className="posture" title={caption}>
      <span className="posture-track" aria-hidden="true">
        {rate !== null && (
          <span
            className={`band-${scoreBand(rate)}`}
            style={{ width: `${rate}%` }}
          />
        )}
      </span>
      <b>{rate === null ? "-" : `${rate}%`}</b>
      {caption && <small className="posture-caption">{caption}</small>}
    </span>
  );
}

/**
 * 표준별 준수(Wiz 컴플라이언스 방식): 표준 행 = 통과·실패 결과 수, 준수 막대, 판정.
 * 행을 펼치면 그 표준의 통제가 나오고, 통제를 누르면 문제 탭의 상세 패널로 간다. 낮은 준수율부터.
 */
export function StandardsTable({
  standards,
  controls,
  environments,
  onSelect,
  onControl,
}: {
  standards: Standard[];
  controls: Control[];
  environments: EnvironmentScore[];
  onSelect: (standardId: string) => void;
  onControl: (controlId: string) => void;
}) {
  const envIds = environments.map((e) => e.env_id);
  const rows = standards
    .map((standard) => {
      const members = controls.filter((c) =>
        c.standard_refs.some((ref) => standardId(ref) === standard.id),
      );
      return { standard, members, ...tally(members, envIds) };
    })
    .filter((row) => row.members.length > 0)
    .sort((a, b) => (a.rate ?? 101) - (b.rate ?? 101));
  const [open, setOpen] = useState<string[]>(
    rows[0] ? [rows[0].standard.id] : [],
  );
  if (rows.length === 0)
    return <p className="pk-empty">연결된 보안 표준이 없어요.</p>;
  return (
    <div
      className="table-wrap"
      tabIndex={0}
      role="region"
      aria-label="보안 표준 목록"
    >
      <table className="pk-table std-table">
        <thead>
          <tr>
            <th>표준, 통제</th>
            <th>심각도</th>
            <th className="num">결과(통과/실패)</th>
            <th>준수율</th>
            <th>판정</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isOpen = open.includes(row.standard.id);
            const band = row.rate === null ? null : scoreBand(row.rate);
            const toggle = () =>
              setOpen(
                isOpen
                  ? open.filter((id) => id !== row.standard.id)
                  : [...open, row.standard.id],
              );
            return (
              <Fragment key={row.standard.id}>
                <tr
                  className="clickable std-group"
                  aria-expanded={isOpen}
                  tabIndex={0}
                  onClick={toggle}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggle();
                    }
                  }}
                >
                  <td>
                    <span className="std-name">
                      <span
                        className={`pk-chevron${isOpen ? " open" : ""}`}
                        aria-hidden="true"
                      />
                      <span>
                        <span className="rule-title">{row.standard.name}</span>
                        <span className="rule-sub">
                          {row.standard.id}
                          {row.standard.version
                            ? ` (${row.standard.version})`
                            : ""}
                          , 통제 {row.members.length}개
                        </span>
                        {isCoverageStandard(row.standard) &&
                          row.standard.note && (
                            <span className="rule-sub std-note">
                              {row.standard.note}
                            </span>
                          )}
                      </span>
                    </span>
                  </td>
                  <td />
                  <td className="num">
                    <Counts pass={row.pass} fail={row.fail} />
                  </td>
                  <td>
                    <Posture
                      rate={row.rate}
                      caption={
                        isCoverageStandard(row.standard)
                          ? COVERAGE_LABEL
                          : undefined
                      }
                    />
                  </td>
                  <td>
                    <span className="std-verdict">
                      {band ? (
                        <StatusLabel tone={BAND_TONES[band]}>
                          {isCoverageStandard(row.standard)
                            ? COVERAGE_BAND_LABELS[band]
                            : BAND_LABELS[band]}
                        </StatusLabel>
                      ) : (
                        <StatusLabel tone="idle">점검 결과 부족</StatusLabel>
                      )}
                      {row.fail > 0 && (
                        <button
                          type="button"
                          className="pk-action"
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelect(row.standard.id);
                          }}
                        >
                          실패 통제 보기
                        </button>
                      )}
                    </span>
                  </td>
                </tr>
                {isOpen &&
                  row.members.map((control) => {
                    const t = tally([control], envIds);
                    const tone: Tone =
                      t.fail > 0 ? "bad" : t.unknown > 0 ? "idle" : "ok";
                    const ref = control.standard_refs.find(
                      (r) => standardId(r) === row.standard.id,
                    );
                    return (
                      <tr
                        key={control.id}
                        className="clickable std-child"
                        tabIndex={0}
                        onClick={() => onControl(control.id)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") onControl(control.id);
                        }}
                      >
                        <td>
                          <span className="rule-cell">
                            <CategoryIcon category={control.category} />
                            <span className="rule-text">
                              <span className="rule-title">
                                {control.title}
                              </span>
                              <span className="rule-sub">
                                {control.id}
                                {ref ? `, ${ref}` : ""}
                              </span>
                            </span>
                          </span>
                        </td>
                        <td>
                          <SeverityMeter severity={control.severity} />
                        </td>
                        <td className="num">
                          <Counts pass={t.pass} fail={t.fail} />
                        </td>
                        <td>
                          <Posture rate={t.rate} />
                        </td>
                        <td>
                          <StatusLabel tone={tone}>
                            {tone === "bad"
                              ? "실패"
                              : tone === "idle"
                                ? "미확인"
                                : "통과"}
                          </StatusLabel>
                        </td>
                      </tr>
                    );
                  })}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
