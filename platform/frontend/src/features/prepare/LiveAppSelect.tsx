import { useState } from "react";
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
    <section className="pk-section">
      <header className="pk-section-head">
        <h2>옮길 앱 고르기</h2>
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
      <p>
        등록한 {snapshot.target?.name} 환경에서 수집한
        Deployment·StatefulSet·DaemonSet 목록이에요. 선택은 이 화면에서만
        유지되며 이전을 실행하지 않아요.
      </p>
      <p>
        {inventory?.rolloutsAvailable
          ? "Argo Rollout도 수집했어요."
          : "Argo Rollout은 미확인 상태예요."}{" "}
        다른 사용자 정의 워크로드와 이미지 접근·DB 의존성과 이전 가능 여부는
        별도 점검이 필요해요.
      </p>
      {pending ? (
        <p role="status">연결 단말의 수집 결과를 기다리고 있어요.</p>
      ) : !ready ? (
        <p role="status">
          {inventory
            ? "앱 목록을 읽지 못했어요. 연결 단말의 Kubernetes 워크로드 조회 권한을 확인하고 다시 수집하세요."
            : "이전 수집 결과에는 앱 목록이 없어요. 워크로드 수집을 지원하는 연결 도구로 업데이트한 뒤 앱 목록을 다시 수집하세요."}
        </p>
      ) : inventory.items.length === 0 ? (
        <p role="status">수집 범위에서 앱을 찾지 못했어요.</p>
      ) : (
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
      )}
      <p role="status">선택한 앱 {selected.length}개</p>
    </section>
  );
}
