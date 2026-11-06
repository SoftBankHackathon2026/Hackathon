import { useEffect, useState } from "react";
import { formatAgo, formatDateTime } from "../lib/format";

/**
 * 위쪽 막대 오른쪽의 갱신 상태(모든 화면 공통). 점 + "N분 전 갱신".
 * 실제 수집값이면 초록 점, 시뮬레이션이면 회색 점과 "시뮬레이션" 표시. 상대 시각은 15초마다 다시 센다.
 */
export function RefreshStatus({
  source,
  at,
  verb = "갱신",
}: {
  source?: string;
  at: string | null | undefined;
  verb?: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, []);
  const simulated = source === "simulated";
  const stale = at ? now - Date.parse(at) > 15 * 60 * 1000 : true;
  const tone = simulated || !at ? "idle" : stale ? "warn" : "live";
  return (
    <span
      className={`refresh refresh-${tone}`}
      title={at ? `${formatDateTime(at)} ${verb}` : undefined}
    >
      <i aria-hidden="true" />
      {at ? `${formatAgo(at, now)} ${verb}` : `${verb} 기록 없음`}
      {simulated && <span className="refresh-sim">시뮬레이션</span>}
    </span>
  );
}
