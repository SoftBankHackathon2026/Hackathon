import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { errorMessage } from "../../api/client";
import type { DataMapping } from "../../api/dataMapping";
import { Skeleton } from "../../components/QueryBoundary";
import { StatusDot } from "../../components/StatusDot";
import { formatDateTime } from "../../lib/format";
import {
  ACTION_LABELS,
  BASIS_LABELS,
  changed,
  choiceValue,
  choose,
  draftFrom,
  draftProblem,
  KIND_LABELS,
  resourceLabel,
  setReason,
  STATUS_LABELS,
  statusTone,
  updateBody,
} from "./dataMapping";
import { dataMappingQueryOptions, useConfirmDataMapping } from "./queries";

export function DataMappingStatus({ jobId }: { jobId: string }) {
  const { data } = useQuery(dataMappingQueryOptions(jobId));
  if (!data) return null;
  return (
    <StatusDot tone={statusTone(data.status)}>
      {STATUS_LABELS[data.status]}
    </StatusDot>
  );
}

export function DataMappingPanel({ jobId }: { jobId: string }) {
  const mapping = useQuery(dataMappingQueryOptions(jobId));
  if (mapping.isError)
    return (
      <p className="info-line" role="alert">
        {errorMessage(mapping.error, "데이터 자원 매핑을 불러오지 못했어요.")}
      </p>
    );
  if (!mapping.data) return <Skeleton rows={2} />;
  const data = mapping.data;
  return (
    <MappingEditor
      key={`${data.inventory_hash}:${data.status}:${data.confirmed_at ?? ""}`}
      jobId={jobId}
      mapping={data}
    />
  );
}

function MappingEditor({
  jobId,
  mapping,
}: {
  jobId: string;
  mapping: DataMapping;
}) {
  const [draft, setDraft] = useState(() => draftFrom(mapping));
  const confirm = useConfirmDataMapping(jobId);
  const names = Object.fromEntries(
    [...mapping.sources, ...mapping.targets].map((r) => [r.id, r]),
  );
  const problem = draftProblem(draft, mapping.sources);
  const dirty = changed(mapping, draft);
  const editable = mapping.status !== "unavailable";

  return (
    <div className="dm">
      <p className="info-line">
        원본 {mapping.source_env} 의 데이터 자원을 대상 {mapping.target_env}{" "}
        어디로 옮길지 정해요. 같은 종류가 하나씩이면 자동으로 짝을 짓고, 여러
        개면 이름이 비슷한 자원을 추천해요. 자원 목록은 비용 수집기가 읽은
        값이에요.
      </p>
      {mapping.blockers.map((blocker) => (
        <p
          key={blocker}
          className={
            mapping.status === "unavailable" ? "info-line" : "pk-alert"
          }
          role={mapping.status === "unavailable" ? undefined : "alert"}
        >
          {blocker}
        </p>
      ))}
      {mapping.sources.length === 0 ? (
        <p className="empty-line">
          원본 환경에서 옮길 데이터 자원을 찾지 못했어요.
        </p>
      ) : (
        <div className="table-wrap">
          <table className="pk-table dm-table">
            <thead>
              <tr>
                <th>종류</th>
                <th>원본 자원</th>
                <th>옮길 곳</th>
                <th>근거</th>
              </tr>
            </thead>
            <tbody>
              {mapping.bindings.map((view) => {
                const binding = draft[view.source];
                const source = names[view.source];
                return (
                  <tr key={view.source}>
                    <td>{KIND_LABELS[view.kind]}</td>
                    <td>{source ? resourceLabel(source) : view.source}</td>
                    <td>
                      <select
                        aria-label={`${source?.name ?? view.source} 을 옮길 곳`}
                        value={choiceValue(binding)}
                        disabled={!editable || confirm.isPending}
                        onChange={(e) =>
                          setDraft((d) =>
                            choose(d, view.source, e.target.value),
                          )
                        }
                      >
                        <option value="">대상 고르기</option>
                        {view.candidates.map((id) => (
                          <option key={id} value={id}>
                            {names[id] ? resourceLabel(names[id]) : id}
                          </option>
                        ))}
                        <option value="create">{ACTION_LABELS.create}</option>
                        <option value="exclude">{ACTION_LABELS.exclude}</option>
                      </select>
                      {binding.action === "exclude" && (
                        <input
                          type="text"
                          className="dm-reason"
                          aria-label={`${source?.name ?? view.source} 을 옮기지 않는 이유`}
                          placeholder="옮기지 않는 이유"
                          value={binding.reason ?? ""}
                          maxLength={500}
                          disabled={!editable || confirm.isPending}
                          onChange={(e) =>
                            setDraft((d) =>
                              setReason(d, view.source, e.target.value),
                            )
                          }
                        />
                      )}
                    </td>
                    <td className="dm-basis">{BASIS_LABELS[view.basis]}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {editable && mapping.sources.length > 0 && (
        <div className="ctl-actions">
          <button
            type="button"
            className="pk-button"
            disabled={confirm.isPending || problem !== null || !dirty}
            onClick={() => confirm.mutate(updateBody(mapping, draft))}
          >
            매핑 확정
          </button>
          {problem && dirty && <span className="info-line">{problem}</span>}
        </div>
      )}
      {confirm.isError && (
        <p className="pk-alert" role="alert">
          {errorMessage(confirm.error, "매핑을 저장하지 못했어요.")}
        </p>
      )}
      {mapping.status === "ready" && mapping.confirmed_at && (
        <p className="info-line" role="status">
          {formatDateTime(mapping.confirmed_at)}
          {mapping.confirmed_by ? ` ${mapping.confirmed_by}` : ""} 확정. 시작
          명령은 이 매핑을 기준으로 해요.
        </p>
      )}
    </div>
  );
}
