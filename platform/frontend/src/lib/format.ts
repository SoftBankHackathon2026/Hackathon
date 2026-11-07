export function formatCount(value: number) {
  return Math.round(value).toLocaleString("ko-KR");
}

export function formatRatio(part: number, whole: number) {
  if (whole <= 0) return "-";
  const tenths = Math.max(0, Math.min(1000, Math.floor((part * 1000) / whole)));
  return `${(tenths / 10).toFixed(1)}%`;
}

export function formatSeconds(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds)) return "-";
  if (seconds <= 0) return "0초";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${Number(seconds.toFixed(1))}초`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}분 ${rest}초` : `${minutes}분`;
}

export function formatRate(perSecond: number) {
  if (!Number.isFinite(perSecond)) return "-";
  return `${Number(perSecond.toFixed(perSecond < 10 ? 1 : 0))}건/초`;
}

export function formatClock(ms: number, withSeconds = true) {
  return new Date(ms).toLocaleTimeString("ko-KR", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
  });
}

export function formatDateTime(iso: string) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "-";
  return new Date(ms).toLocaleString("ko-KR", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatAgo(iso: string | null, now = Date.now()) {
  if (iso === null) return "-";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "-";
  const diff = Math.max(0, Math.round((now - ms) / 1000));
  if (diff < 10) return "방금";
  if (diff < 60) return `${diff}초 전`;
  if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  return `${Math.floor(diff / 86400)}일 전`;
}

export function shortRevision(revision: string | null) {
  if (revision === null) return "-";
  return revision.length > 8 ? revision.slice(0, 7) : revision;
}

export function imageTag(image: string) {
  const name = image.split("/").pop() ?? image;
  return name.length > 40 ? `${name.slice(0, 37)}...` : name;
}
