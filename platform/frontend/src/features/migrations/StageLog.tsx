import { Fragment } from "react";
import type { StageInfo } from "../../api/migrations";
import { BarCell } from "../../components/BarCell";
import {
  ExpandedBody,
  ExpandToggle,
  expandableRow,
} from "../../components/ExpandRow";
import { KeyValue } from "../../components/KeyValue";
import { StatusDot } from "../../components/StatusDot";
import { useToggleSet } from "../../components/useToggleSet";
import { formatDateTime, formatSeconds } from "../../lib/format";
import { formatEvidence } from "./evidence";
import {
  stageDurationSeconds,
  stageNotes,
  STAGE_STATUS_LABELS,
  stageTone,
} from "./stages";

export function StageLog({ stages }: { stages: StageInfo[] }) {
  const { isOpen, toggle } = useToggleSet();
  const now = Date.now();
  const durations = stages.map((s) => stageDurationSeconds(s, now) ?? 0);
  const max = Math.max(0, ...durations);
  if (stages.length === 0)
    return <p className="empty-line">단계 정보가 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table">
        <thead>
          <tr>
            <th className="toggle-col" />
            <th>단계명</th>
            <th>상태</th>
            <th>시작</th>
            <th className="num">소요</th>
            <th>비고</th>
          </tr>
        </thead>
        <tbody>
          {stages.map((stage, i) => {
            const open = isOpen(stage.name);
            const evidence = Object.entries(stage.evidence);
            return (
              <Fragment key={stage.name}>
                <tr {...expandableRow(open, () => toggle(stage.name))}>
                  <td className="toggle-col">
                    <ExpandToggle
                      open={open}
                      label={stage.title}
                      onToggle={() => toggle(stage.name)}
                    />
                  </td>
                  <td>
                    {stage.title} <span className="sub">{stage.name}</span>
                  </td>
                  <td>
                    <StatusDot tone={stageTone(stage.status)}>
                      {STAGE_STATUS_LABELS[stage.status]}
                    </StatusDot>
                  </td>
                  <td className="sub">
                    {stage.started_at ? formatDateTime(stage.started_at) : "-"}
                  </td>
                  <td className="num">
                    {stage.started_at ? (
                      <BarCell value={durations[i]} max={max}>
                        {formatSeconds(durations[i])}
                      </BarCell>
                    ) : (
                      <span className="sub">-</span>
                    )}
                  </td>
                  <td>
                    {stageNotes(stage).map((note) => (
                      <span className="tag" key={note}>
                        {note}
                      </span>
                    ))}
                  </td>
                </tr>
                {open && (
                  <ExpandedBody colSpan={6}>
                    <KeyValue
                      rows={[
                        { label: "설명", value: stage.description || "-" },
                        {
                          label: "종료",
                          value: stage.finished_at
                            ? formatDateTime(stage.finished_at)
                            : "-",
                        },
                        ...(evidence.length === 0
                          ? [
                              {
                                label: "근거",
                                value: (
                                  <span className="sub">
                                    기록된 근거가 없어요.
                                  </span>
                                ),
                              },
                            ]
                          : evidence.map(([key, value]) => ({
                              label: <span className="mono">{key}</span>,
                              value: formatEvidence(value),
                            }))),
                      ]}
                    />
                  </ExpandedBody>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
