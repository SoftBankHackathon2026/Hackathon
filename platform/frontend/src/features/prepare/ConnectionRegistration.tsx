import { useState } from "react";
import { StatusDot } from "../../components/StatusDot";
import type { ConnectionSnapshot } from "./connectionTypes";
import { formatTime, PROVIDERS } from "./model";

export function CollectionSource({
  snapshot: s,
  busy,
  onCollect,
}: {
  snapshot: ConnectionSnapshot;
  busy: boolean;
  onCollect: (sourceId: string) => void;
}) {
  const [selected, setSelected] = useState("");
  return (
    <section>
      <header className="connection-title">
        <h2>정보를 수집할 대상을 확인하세요</h2>
        <p>
          보안 연결을 확인했어요. 단말에서 찾은 계정이나 Kubernetes 문맥을 직접
          선택하세요.
        </p>
      </header>
      <fieldset className="connection-discovered">
        <legend>단말에서 찾은 대상</legend>
        {s.discovered.map((source) => (
          <label
            className={selected === source.id ? "selected" : ""}
            key={source.id}
          >
            <input
              type="radio"
              name="collection-source"
              checked={selected === source.id}
              onChange={() => setSelected(source.id)}
              disabled={busy || s.collectionPending}
            />
            <span>
              <strong>{source.name}</strong>
              <small>
                {PROVIDERS.find((p) => p.value === source.kind)?.title},{" "}
                {source.context ? "문맥 " + source.context : source.identity}
              </small>
            </span>
          </label>
        ))}
      </fieldset>
      <p className="connection-note">
        선택한 대상의 자원 설정만 조회해요. 데이터/비밀 읽기와 설치/권한 변경은
        앱 선택 이후 별도 검토가 필요해요.
      </p>
      <div className="connection-actions">
        <button
          className="primary"
          disabled={busy || s.collectionPending || !selected}
          onClick={() => onCollect(selected)}
        >
          {s.collectionPending ? "환경정보 수집 중…" : "선택한 환경정보 수집"}
        </button>
      </div>
      {s.collectionPending && (
        <p role="status" className="connection-note">
          단말에서 선택한 환경을 조회하고 있어요. 결과가 도착하면 등록 단계로
          이어져요.
        </p>
      )}
    </section>
  );
}

export function RegisteredEnvironment({
  snapshot: s,
  onNext,
}: {
  snapshot: ConnectionSnapshot;
  onNext?: () => void;
}) {
  const source = s.collected.find((c) => c.id === s.selectedEnvironmentId);
  return (
    <section>
      <header className="connection-title">
        <h2>환경을 등록했어요</h2>
        <p>
          수집된 환경정보에 이름과 역할을 저장했어요. 아직 앱 선택이나 이관
          작업은 시작하지 않았어요.
        </p>
      </header>
      <dl className="connection-facts">
        <div>
          <dt>환경 이름</dt>
          <dd>{s.target?.name}</dd>
        </div>
        <div>
          <dt>조직/프로젝트</dt>
          <dd>
            {s.organization} / {s.target?.project}
          </dd>
        </div>
        <div>
          <dt>환경 종류</dt>
          <dd>{PROVIDERS.find((p) => p.value === s.target?.kind)?.title}</dd>
        </div>
        <div>
          <dt>역할</dt>
          <dd>{s.target?.role === "source" ? "출발 환경" : "도착 환경"}</dd>
        </div>
        <div>
          <dt>확인한 대상</dt>
          <dd>{source?.identity ?? "미확인"}</dd>
        </div>
        {source?.context && (
          <div>
            <dt>선택한 문맥</dt>
            <dd>{source.context}</dd>
          </div>
        )}
        <div>
          <dt>수집 시각</dt>
          <dd>{formatTime(s.collectedAt)} (한국시간)</dd>
        </div>
        <div>
          <dt>등록 시각</dt>
          <dd>{formatTime(s.registeredAt)} (한국시간)</dd>
        </div>
      </dl>
      <div className="connection-next">
        <button className="primary" disabled={!onNext} onClick={onNext}>
          다음: 앱 선택
        </button>
        <p>
          {onNext
            ? "이 환경에서 읽은 워크로드 중 옮길 앱을 고르고, 같이 가야 하는 데이터와 비밀값을 확인해요."
            : "실제 연결에서는 환경 등록까지 지원해요. 앱을 선택하거나 이관하려면 배포 대상 연동이 필요해요."}
        </p>
      </div>
    </section>
  );
}
export function CleanupResult({
  snapshot: s,
}: {
  snapshot: ConnectionSnapshot;
}) {
  if (!s.cleanup) return null;
  return (
    <section>
      <header className="connection-title">
        <h2>
          {s.cleanup.server && s.cleanup.gateway && s.cleanup.local
            ? "연결을 해제했어요"
            : "연결 해제를 확인하고 있어요"}
        </h2>
        <p>서버 차단과 단말 정리 결과를 구분해요.</p>
      </header>
      <dl className="connection-facts">
        {(
          [
            ["서버 권한 철회", s.cleanup.server],
            ["터널 차단", s.cleanup.gateway],
            ["고객 권한 정리", s.cleanup.resources],
            ["단말 파일 정리", s.cleanup.local],
          ] as const
        ).map(([label, done]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              <StatusDot tone={done ? "ok" : "idle"}>
                {done ? "확인 완료" : "미확인"}
              </StatusDot>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
