import { useMemo, useSyncExternalStore } from "react";
import { createLiveConnections } from "../../api/liveConnections";
import { StatusDot } from "../../components/StatusDot";
import { connectionStatus } from "../prepare/model";

/** Connection records are not deployment providers or fabricated fleet topology. */
export function RegisteredConnections() {
  const gateway = useMemo(() => createLiveConnections(), []);
  const snapshots = useSyncExternalStore(gateway.subscribe, gateway.list);
  const registered = snapshots.filter(
    (item) => item.environmentId && item.target,
  );
  if (!registered.length) return null;
  return (
    <section className="pk-section" aria-label="직접 등록한 환경">
      <header className="pk-section-head">
        <h2>직접 등록한 환경</h2>
        <span className="pk-section-meta">
          수집한 환경정보 · 배포 연결은 별도
        </span>
      </header>
      <table className="pk-table">
        <thead>
          <tr>
            <th>환경</th>
            <th>프로젝트</th>
            <th>역할</th>
            <th>연결 상태</th>
          </tr>
        </thead>
        <tbody>
          {registered.map((item) => {
            const status = connectionStatus(item);
            return (
              <tr key={item.id}>
                <td>
                  <a href={`#prepare/${encodeURIComponent(item.id)}`}>
                    {item.target!.name}
                  </a>
                </td>
                <td>{item.target!.project}</td>
                <td>
                  {item.target!.role === "source" ? "출발 환경" : "도착 환경"}
                </td>
                <td>
                  <StatusDot tone={gateway.available ? status.tone : "idle"}>
                    {gateway.available ? status.label : "현재 연결 미확인"}
                  </StatusDot>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
