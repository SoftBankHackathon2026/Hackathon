import type {
  BindingAction,
  BindingBasis,
  DataBinding,
  DataKind,
  DataMapping,
  DataResource,
  MappingStatus,
} from "../../api/dataMapping.ts";
import type { Tone } from "./stages.ts";

export const KIND_LABELS: Record<DataKind, string> = {
  postgres: "PostgreSQL",
  kafka: "Kafka",
  object_storage: "오브젝트 스토리지",
};

export const ACTION_LABELS: Record<BindingAction, string> = {
  map: "기존 자원으로",
  create: "새로 만들기",
  exclude: "옮기지 않음",
};

export const BASIS_LABELS: Record<BindingBasis, string> = {
  only_pair: "자동: 같은 종류가 하나씩",
  no_target: "자동: 대상에 같은 종류 없음",
  name: "추천: 이름이 가장 비슷함",
  none: "직접 골라야 함",
  confirmed: "확정됨",
};

export const STATUS_LABELS: Record<MappingStatus, string> = {
  ready: "준비됨",
  needs_review: "확정 필요",
  stale: "다시 확인 필요",
  unavailable: "자원 목록 없음",
};

export function statusTone(status: MappingStatus): Tone {
  if (status === "ready") return "ok";
  if (status === "unavailable") return "idle";
  return "warn";
}

export type Draft = Record<string, DataBinding>;

export function draftFrom(mapping: DataMapping): Draft {
  return Object.fromEntries(
    mapping.bindings.map((b) => [
      b.source,
      {
        source: b.source,
        action: b.action,
        target: b.target,
        reason: b.reason,
      },
    ]),
  );
}

export function choose(draft: Draft, source: string, value: string): Draft {
  const current = draft[source];
  if (value === "create" || value === "exclude")
    return {
      ...draft,
      [source]: { ...current, action: value, target: null },
    };
  return {
    ...draft,
    [source]: { ...current, action: "map", target: value || null },
  };
}

export function choiceValue(binding: DataBinding) {
  return binding.action === "map" ? (binding.target ?? "") : binding.action;
}

export function setReason(draft: Draft, source: string, reason: string): Draft {
  return { ...draft, [source]: { ...draft[source], reason } };
}

export function draftProblem(
  draft: Draft,
  sources: DataResource[],
): string | null {
  for (const source of sources) {
    const binding = draft[source.id];
    if (!binding) return `${source.name} 를 정해야 해요.`;
    if (binding.action === "map" && !binding.target)
      return `${source.name} 를 옮길 대상을 골라야 해요.`;
    if (binding.action === "exclude" && !(binding.reason ?? "").trim())
      return `${source.name} 를 옮기지 않는 이유를 적어야 해요.`;
  }
  return null;
}

export function updateBody(mapping: DataMapping, draft: Draft) {
  return {
    inventory_hash: mapping.inventory_hash,
    bindings: mapping.sources.map((s) => {
      const b = draft[s.id];
      return {
        source: s.id,
        action: b.action,
        target: b.action === "map" ? b.target : null,
        reason: b.action === "exclude" ? (b.reason ?? "").trim() : null,
      };
    }),
  };
}

export function changed(mapping: DataMapping, draft: Draft) {
  if (mapping.status !== "ready") return true;
  const saved = draftFrom(mapping);
  return mapping.sources.some((s) => {
    const a = saved[s.id];
    const b = draft[s.id];
    return (
      !a ||
      !b ||
      a.action !== b.action ||
      a.target !== b.target ||
      (a.reason ?? "") !== (b.reason ?? "")
    );
  });
}

export function basisText(basis: BindingBasis, note: string | null) {
  return note ? `${BASIS_LABELS[basis]} (${note})` : BASIS_LABELS[basis];
}

export function resourceLabel(resource: DataResource) {
  const where =
    (resource.origin === "managed" ? "관리형" : "클러스터") +
    (resource.infrastructure ? ", 인프라" : "");
  return resource.detail
    ? `${resource.name} (${where}, ${resource.detail})`
    : `${resource.name} (${where})`;
}
