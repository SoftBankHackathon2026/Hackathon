import type { FleetEnvironment } from "../../api/migrations";
import type { Posture } from "../../api/security";
import { ProviderLogo } from "../../components/ProviderLogo";
import {
  BAND_LABELS,
  hasEnoughEvidence,
  overviewScore,
  scoreBand,
} from "./posture";

/** 환경별 점수: 카드 대신 막대 목록(Defender 의 클라우드별 점수처럼). 낮은 점수부터. */
export function EnvScores({
  environments,
  fleetEnvs,
  compact,
}: {
  environments: Posture["environments"];
  fleetEnvs: FleetEnvironment[];
  /** 위젯 안: 막대·점수·판정만, 통과/실패 수는 줄 아래로. */
  compact?: boolean;
}) {
  const rows = environments
    .map((env) => {
      const part = overviewScore(env);
      const enough = hasEnoughEvidence(env.passed + env.failed, part.total);
      const band = enough && part.score !== null ? scoreBand(part.score) : null;
      return { env, score: band ? part.score : null, band };
    })
    .sort((a, b) => (a.score ?? 101) - (b.score ?? 101));
  return (
    <ul className={`env-scores${compact ? " compact" : ""}`}>
      {rows.map(({ env, score, band }) => (
        <li key={env.env_id}>
          <span className="pk-path">
            <ProviderLogo
              provider={fleetEnvs.find((e) => e.id === env.env_id)?.provider}
            />
            {env.name}
          </span>
          <span className="env-score-track" aria-hidden="true">
            <span
              className={`band-${band ?? "none"}`}
              style={{ width: `${score ?? 0}%` }}
            />
          </span>
          <b className={`env-score band-${band ?? "none"}`}>{score ?? "-"}</b>
          <span className="sub">
            {band ? BAND_LABELS[band] : "점검 결과 부족"}
          </span>
          <span className="sub env-score-counts">
            통과 {env.passed}, 실패 {env.failed}, 미확인 {env.unknown}
          </span>
        </li>
      ))}
    </ul>
  );
}
