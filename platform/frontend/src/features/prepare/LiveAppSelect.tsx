import { useState } from "react";
import "./live-app-select.css";
import type { ConnectionSnapshot } from "./connectionTypes";

/** Live inventory only. Never substitute preview workloads or migration verdicts. */
export function LiveAppSelect({
  snapshot,
  busy,
  onRefresh,
}: {
  snapshot: ConnectionSnapshot;
  busy: boolean;
  onRefresh: () => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const inventory = snapshot.workloads;
  const pending = busy || snapshot.collectionPending;
  const ready = inventory?.coverage === "complete";
  return (
    <section className="pk-section live-apps" aria-labelledby="live-apps-title">
      <header className="live-apps-head">
        <div className="live-apps-heading">
          <h2 id="live-apps-title">옮길 앱 고르기</h2>
          <p>{snapshot.target?.name} 환경의 앱을 선택하세요.</p>
        </div>
        <button
          className="secondary"
          onClick={onRefresh}
          disabled={
            pending ||
            snapshot.tunnel !== "READY" ||
            !snapshot.permissions.registerEnvironment
          }
        >
          {pending ? "앱 목록 수집 중…" : "앱 목록 다시 수집"}
        </button>
      </header>
      {pending || !ready || inventory.items.length === 0 ? (
        <div className="live-apps-empty" role="status" aria-live="polite">
          <span className="live-apps-symbol" aria-hidden="true">
            ▦
          </span>
          <h3>
            {pending
              ? "앱 목록을 수집하고 있어요"
              : !inventory
                ? "앱 목록을 먼저 수집해 주세요"
                : !ready
                  ? "앱 목록을 가져오지 못했어요"
                  : "아직 표시할 앱이 없어요"}
          </h3>
          <p>
            {pending
              ? "연결 단말에서 앱 정보를 읽고 있어요. 결과가 도착하면 목록이 표시됩니다."
              : !inventory
                ? "이전 수집 결과에는 앱 목록이 없어요. 연결 도구를 업데이트한 뒤 ‘앱 목록 다시 수집’을 눌러 주세요."
                : !ready
                  ? "연결 단말의 워크로드 조회 권한을 확인한 뒤 다시 시도해 주세요."
                  : "이 환경의 수집 범위에서 앱을 찾지 못했어요. 연결한 클러스터와 조회 권한을 확인해 주세요."}
          </p>
        </div>
      ) : (
        <div className="live-apps-table-wrap">
          <table className="pk-table">
            <thead>
              <tr>
                <th>선택</th>
                <th>네임스페이스 / 앱</th>
                <th>종류 · 수</th>
                <th>이미지</th>
                <th>설정·저장소 참조</th>
              </tr>
            </thead>
            <tbody>
              {inventory.items.map((w) => {
                const id = `${w.namespace}/${w.kind}/${w.name}`;
                return (
                  <tr key={id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`${id} 선택`}
                        checked={selected.includes(id)}
                        onChange={(e) =>
                          setSelected((old) =>
                            e.target.checked
                              ? [...old, id]
                              : old.filter((x) => x !== id),
                          )
                        }
                      />
                    </td>
                    <td>
                      {w.namespace} / {w.name}
                    </td>
                    <td>
                      {w.kind} · {w.replicas}
                    </td>
                    <td>{w.images.join(", ")}</td>
                    <td>
                      Secret: {w.secrets.join(", ") || "없음"}
                      <br />
                      ConfigMap: {w.configMaps.join(", ") || "없음"}
                      <br />
                      PVC: {w.pvcs.join(", ") || "없음"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {ready && !pending && inventory.items.length > 0 && (
        <footer className="live-apps-footer">
          <strong role="status">선택한 앱 {selected.length}개</strong>
          <span>
            총 {inventory.items.length}개 · 선택해도 이전이 바로 실행되지는
            않아요.
          </span>
        </footer>
      )}
      <details className="live-apps-help">
        <summary>앱 수집 범위와 다음 단계</summary>
        <p>
          Deployment·StatefulSet·DaemonSet을 조회합니다.{" "}
          {inventory?.rolloutsAvailable
            ? "Argo Rollout도 수집했어요."
            : "Argo Rollout 수집 여부는 아직 확인되지 않았어요."}
        </p>
        <p>
          이미지 접근·DB 의존성·이전 가능 여부는 별도 점검이 필요해요. 현재
          선택은 이 화면에서만 유지됩니다.
        </p>
      </details>
    </section>
  );
}
