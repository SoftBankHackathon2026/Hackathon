import type { MigrationDetail } from "../../api/migrations.ts";
import { formatCount } from "../../lib/format.ts";
import { stateLabel, stateTone, type Tone } from "./stages.ts";

export type CdcNode = {
  key: string;
  kind: string;
  name: string;
  detail: string;
  metric: string | null;
  tone: Tone;
  flag: string | null;
};

function connectorNode(
  detail: MigrationDetail,
  role: "source" | "sink",
): CdcNode {
  const kind = role === "source" ? "Source 커넥터" : "Sink 커넥터";
  const connector = detail.connectors.find((c) => c.role === role);
  const base = { key: role, kind, metric: null };
  if (connector) {
    const failing = stateTone(connector.state) === "bad";
    return {
      ...base,
      name: connector.name,
      detail: `${stateLabel(connector.state)}, 작업 ${connector.tasks_total}개 중 ${connector.tasks_running}개 실행`,
      tone: stateTone(connector.state),
      flag: failing ? "커넥터 실패" : null,
    };
  }
  const status = detail.summary.status;
  if (status === "completed" || status === "rolled_back")
    return {
      ...base,
      name: "삭제됨",
      detail: "전환 완료 후 정리",
      tone: "idle",
      flag: null,
    };
  if (status === "pending")
    return { ...base, name: "-", detail: "시작 전", tone: "idle", flag: null };
  return {
    ...base,
    name: "찾을 수 없음",
    detail: "커넥터가 등록돼 있지 않아요",
    tone: "bad",
    flag: "커넥터 없음",
  };
}

export function cdcNodes(
  detail: MigrationDetail,
  names: { source: string; target: string },
  counts: { source: number; target: number },
): CdcNode[] {
  const { summary } = detail;
  return [
    {
      key: "source-db",
      kind: "원본 DB",
      name: names.source,
      detail: `DB ${summary.data_source}`,
      metric: `쓰기 ${formatCount(counts.source)}건`,
      tone: "ok",
      flag: null,
    },
    connectorNode(detail, "source"),
    {
      key: "topic",
      kind: "Kafka 토픽",
      name: summary.job_id,
      detail: "변경분 전달",
      metric: null,
      tone: "idle",
      flag: null,
    },
    connectorNode(detail, "sink"),
    {
      key: "target-db",
      kind: "대상 DB",
      name: names.target,
      detail: `DB ${summary.data_target}`,
      metric: `쓰기 ${formatCount(counts.target)}건`,
      tone: "ok",
      flag: null,
    },
  ];
}
