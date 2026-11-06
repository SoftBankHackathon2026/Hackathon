export type SortDir = "asc" | "desc";
export type SortValue = number | string | null | undefined;

/** 값이 없는 행(null·빈 문자열)은 방향과 상관없이 맨 아래로 보낸다. */
export function sortRows<T>(
  rows: T[],
  value: (row: T) => SortValue,
  dir: SortDir,
): T[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    const xMissing = x === null || x === undefined || x === "";
    const yMissing = y === null || y === undefined || y === "";
    if (xMissing || yMissing)
      return xMissing === yMissing ? 0 : xMissing ? 1 : -1;
    if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
    return String(x).localeCompare(String(y), "ko") * sign;
  });
}
