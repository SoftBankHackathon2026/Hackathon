import { useState } from "react";
import { Icon } from "../../components/Icon";
import type { ConnectionSnapshot, ConnectionTarget } from "./connectionTypes";
import { canRegister, emptyTarget, PROVIDERS, targetValid } from "./model";

export function ConnectionTargetForm({
  snapshot: s,
  busy,
  onRegister,
}: {
  snapshot: ConnectionSnapshot;
  busy: boolean;
  onRegister: (sourceId: string, target: ConnectionTarget) => void;
}) {
  const [selectedId, setSelectedId] = useState(
    s.collected.length === 1 ? s.collected[0].id : "",
  );
  const selected = s.collected.find((e) => e.id === selectedId);
  const [draft, setDraft] = useState(() => ({
    project: "",
    name: s.collected.length === 1 ? s.collected[0].name : "",
    role: "source" as "source" | "target",
  }));
  const target: ConnectionTarget = {
    ...emptyTarget(s.organization),
    ...draft,
    kind: selected?.kind ?? "kubernetes",
    region: selected?.region ?? "",
  };
  return (
    <form
      className="connection-target"
      onSubmit={(e) => {
        e.preventDefault();
        if (selected && canRegister(s) && targetValid(target))
          onRegister(selected.id, target);
      }}
    >
      <header className="connection-title">
        <h2>수집한 환경을 등록하세요</h2>
        <p>
          확인된 환경정보에 이름과 역할을 지정해요. 마이그레이션 범위와 실행
          계획은 앱 선택에서 검토해요.
        </p>
      </header>
      {s.collected.length > 1 && (
        <label>
          등록할 환경
          <select
            value={selectedId}
            required
            disabled={busy}
            onChange={(e) => {
              setSelectedId(e.target.value);
              const next = s.collected.find((c) => c.id === e.target.value);
              setDraft((d) => ({ ...d, name: next?.name ?? "" }));
            }}
          >
            <option value="">수집한 환경을 선택하세요</option>
            {s.collected.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {selected && (
        <div className="connection-collected">
          <div className="connection-collected-heading">
            <Icon
              name={selected.kind === "kubernetes" ? "layers" : "cloud"}
              size={20}
            />
            <strong>
              {PROVIDERS.find((p) => p.value === selected.kind)?.title}
            </strong>
            <span>수집된 정보</span>
          </div>
          <dl className="connection-facts">
            <div>
              <dt>계정/클러스터</dt>
              <dd>{selected.identity}</dd>
            </div>
            {selected.context && (
              <div>
                <dt>선택한 문맥</dt>
                <dd>{selected.context}</dd>
              </div>
            )}
            {selected.region && (
              <div>
                <dt>리전</dt>
                <dd>{selected.region}</dd>
              </div>
            )}
            {selected.facts.map((f) => (
              <div key={f.label}>
                <dt>{f.label}</dt>
                <dd>{f.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      <div className="connection-fields">
        <label>
          조직
          <input value={s.organization} readOnly />
        </label>
        <label>
          프로젝트
          <input
            required
            maxLength={80}
            value={draft.project}
            disabled={busy}
            onChange={(e) =>
              setDraft((d) => ({ ...d, project: e.target.value }))
            }
            placeholder="예: 주문 서비스 이관"
          />
        </label>
        <label>
          환경 이름
          <input
            required
            maxLength={80}
            value={draft.name}
            disabled={busy}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            placeholder="환경을 구분할 이름"
          />
        </label>
        <label>
          환경 역할
          <select
            value={draft.role}
            disabled={busy}
            onChange={(e) =>
              setDraft((d) => ({
                ...d,
                role: e.target.value as "source" | "target",
              }))
            }
          >
            <option value="source">출발 환경</option>
            <option value="target">도착 환경</option>
          </select>
        </label>
      </div>
      {!s.permissions.registerEnvironment && (
        <p role="status">
          환경을 등록할 권한이 없어요. 조직 관리자에게 문의하세요.
        </p>
      )}
      <footer className="connection-actions">
        <button
          type="submit"
          className="primary"
          disabled={
            busy || !selected || !canRegister(s) || !targetValid(target)
          }
        >
          환경 등록 <Icon name="arrow" size={16} />
        </button>
        <span>
          이 단계에서는 설치/권한 변경이나 데이터 이관을 실행하지 않아요.
        </span>
      </footer>
    </form>
  );
}
