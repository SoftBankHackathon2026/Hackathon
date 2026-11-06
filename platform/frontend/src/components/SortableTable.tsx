import { useState, type ReactNode } from "react";
import { sortRows, type SortDir as Dir, type SortValue } from "../lib/sort";

/**
 * 표 정렬(모든 표 공통). 머리를 누르면 내림차순 → 오름차순 → 원래 순서.
 * 값이 없는 행(null)은 방향과 상관없이 맨 아래로 보낸다.
 */
export function useSort<T, K extends string>(
  rows: T[],
  accessors: Record<K, (row: T) => SortValue>,
) {
  const [sort, setSort] = useState<{ key: K; dir: Dir } | null>(null);
  const sorted = sort ? sortRows(rows, accessors[sort.key], sort.dir) : rows;
  const toggle = (key: K) =>
    setSort((prev) =>
      prev?.key !== key
        ? { key, dir: "desc" }
        : prev.dir === "desc"
          ? { key, dir: "asc" }
          : null,
    );
  const th = (key: K, label: ReactNode, className?: string) => {
    const dir = sort?.key === key ? sort.dir : null;
    return (
      <th
        key={key}
        className={className}
        aria-sort={
          dir === "asc" ? "ascending" : dir === "desc" ? "descending" : "none"
        }
      >
        <button type="button" className="pk-sort" onClick={() => toggle(key)}>
          {label}
          <span className="pk-sort-mark" aria-hidden="true">
            {dir === "asc" ? "▲" : dir === "desc" ? "▼" : "↕"}
          </span>
        </button>
      </th>
    );
  };
  return { sorted, th };
}
