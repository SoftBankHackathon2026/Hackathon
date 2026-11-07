import type { MigrationDetail } from "../../api/migrations.ts";
import { formatCount, formatSeconds } from "../../lib/format.ts";
import { failedWrites, jobVerify } from "./summary.ts";
import {
  STATUS_LABELS,
  stepLabel,
  stageTitle,
  statusTone,
  type Tone,
} from "./stages.ts";

export type HeadlinePart = { text: string; bad?: boolean };

export function headline(
  detail: MigrationDetail,
  names: { source: string; target: string },
): { tone: Tone; label: string; parts: HeadlinePart[] } {
  const { summary } = detail;
  const tone = statusTone(summary.status);
  const label = STATUS_LABELS[summary.status];
  const failed = failedWrites(summary);
  const verify = jobVerify(detail.verify.pre, detail.verify.post);
  const failedPart = { text: `${formatCount(failed)}건`, bad: failed > 0 };
  const pause = formatSeconds(summary.downtime.write_pause_seconds ?? 0);
  switch (summary.status) {
    case "completed":
      return {
        tone,
        label,
        parts: [
          {
            text: `쓰기 ${formatCount(summary.writes.committed_target)}건이 ${names.target}로 넘어갔어요. `,
          },
          ...(failed > 0
            ? [{ text: "실패한 쓰기는 " }, failedPart, { text: "이고, " }]
            : [{ text: "실패한 쓰기는 없고, " }]),
          { text: `쓰기 중단은 최대 ${pause}였어요. ` },
          ...(verify.state === "none"
            ? [{ text: "데이터 검증은 아직 안 했어요." }]
            : verify.mismatch > 0
              ? [
                  { text: "데이터 불일치는 " },
                  { text: `${formatCount(verify.mismatch)}건`, bad: true },
                  { text: "이에요." },
                ]
              : [{ text: "데이터 불일치도 없어요." }]),
        ],
      };
    case "waiting_approval":
      return {
        tone,
        label,
        parts: [
          {
            text: "변경분 동기화가 끝났어요. 승인하면 쓰기 차단부터 시작해요. 전환 단계부터는 되돌릴 수 없어요.",
          },
        ],
      };
    case "running":
      return {
        tone,
        label,
        parts: [
          {
            text: `${stageTitle(summary.stage)} 단계(${stepLabel(summary)})를 진행 중이에요. `,
          },
          ...(failed > 0
            ? [{ text: "실패한 쓰기는 " }, failedPart, { text: "이에요." }]
            : [{ text: "실패한 쓰기는 없어요." }]),
        ],
      };
    case "pending":
      return {
        tone,
        label,
        parts: [{ text: "아직 시작하지 않은 작업이에요." }],
      };
    default:
      return {
        tone,
        label,
        parts: [
          { text: `${stageTitle(summary.stage)} 단계에서 멈췄어요. ` },
          ...(failed > 0
            ? [{ text: "실패한 쓰기는 " }, failedPart, { text: "이에요." }]
            : [{ text: "실패한 쓰기는 없어요." }]),
        ],
      };
  }
}
