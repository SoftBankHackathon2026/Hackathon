import type { Category } from "../../api/security";
import { CATEGORY_LABELS } from "./posture";

const GLYPHS: Record<Category, string> = {
  network:
    "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.8 2.6 2.8 15.4 0 18M12 3c-2.8 2.6-2.8 15.4 0 18",
  workload:
    "M3.5 7.5L12 3l8.5 4.5v9L12 21l-8.5-4.5zM3.5 7.5L12 12l8.5-4.5M12 12v9",
  identity:
    "M15 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM5.5 19c1-3.2 3.6-5 6.5-5s5.5 1.8 6.5 5",
  data: "M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3zM5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3",
  detection:
    "M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7-9.5-7-9.5-7zM12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
  governance: "M8 4h8v3H8zM6 5.5H5v15h14v-15h-1M8.5 12l2.5 2.5 4.5-4.5",
  japan:
    "M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 1 1 13 0c0 5.4-6.5 11-6.5 11zM12 7.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
};

/** 통제 분류 아이콘 타일(Wiz 규칙 목록처럼 행 맨 앞). 분류마다 옅은 색 면. */
export function CategoryIcon({ category }: { category: Category }) {
  return (
    <span
      className={`cat-icon cat-${category}`}
      title={CATEGORY_LABELS[category]}
      aria-hidden="true"
    >
      <svg viewBox="0 0 24 24">
        <path d={GLYPHS[category]} />
      </svg>
    </span>
  );
}
