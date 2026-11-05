import type { Posture } from "../../api/security";
import { SummaryCard } from "../../components/PageKit";
import type { Tone } from "../migrations/stages";
import {
  BAND_LABELS,
  compareSummary,
  complianceBar,
  hasEnoughEvidence,
  overviewScore,
  scoreBand,
} from "./posture";

const BAND_TONES: Record<string, Tone> = {
  high: "ok",
  mid: "warn",
  low: "bad",
};

const SEGMENT_LABELS = { pass: "통과", fail: "실패", unknown: "미확인" };

export function ComplianceOverview({
  posture,
  environments,
}: {
  posture: Posture;
  environments: Posture["environments"];
}) {
  const compare = compareSummary(posture.parity, posture.controls);
  const scaleMax = Math.max(
    0,
    ...environments.map((e) => e.passed + e.failed + e.unknown),
  );
  return (
    <>
      <div className="pk-cards">
        {environments.map((env) => {
          const part = overviewScore(env);
          const enough = hasEnoughEvidence(env.passed + env.failed, part.total);
          const band = part.score === null ? null : scoreBand(part.score);
          return (
            <SummaryCard
              key={env.env_id}
              title={env.name}
              label={
                band
                  ? { tone: BAND_TONES[band], text: BAND_LABELS[band] }
                  : { tone: "idle", text: "근거 부족" }
              }
              value={enough && part.score !== null ? String(part.score) : "-"}
              unit={enough ? "점" : ""}
              lines={[
                `점검 ${part.total}개 중 통과 ${env.passed} · 실패 ${env.failed} · 미확인 ${env.unknown}`,
              ]}
            />
          );
        })}
        <SummaryCard
          title="전환 전후 비교"
          label={null}
          value={String(compare.weakened.length)}
          unit="개 새로 실패"
          lines={[
            compare.weakened.length > 0
              ? `도착 환경에서 새로 실패한 통제가 ${compare.weakened.length}개 있어요`
              : "도착 환경에서 새로 실패한 통제는 없어요",
            ...(compare.strengthened.length > 0
              ? [
                  `새로 통과한 통제: ${compare.strengthened.join(", ")}${
                    compare.moreStrengthened > 0
                      ? ` 외 ${compare.moreStrengthened}개`
                      : ""
                  }`,
                ]
              : []),
          ]}
        />
      </div>
      <div className="pk-lanes">
        <div className="pk-chart-title">
          <h3>점검 결과 분포</h3>
        </div>
        {environments.map((env) => {
          const bar = complianceBar(env, scaleMax);
          return (
            <div key={env.env_id} className="pk-dist">
              <span className="pk-lane-name">
                {env.name}
                <small>점검 {bar.total}개</small>
              </span>
              <div className="pk-dist-track">
                {bar.segments
                  .filter((s) => s.count > 0)
                  .map((s) => (
                    <span
                      key={s.key}
                      className={`pk-seg seg-${s.key}`}
                      style={{ width: `${s.width}%` }}
                    >
                      {SEGMENT_LABELS[s.key]} {s.count}
                      <small>{s.percent}%</small>
                    </span>
                  ))}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
