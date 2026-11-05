export function barPercent(value: number, max: number) {
  if (!Number.isFinite(value) || !Number.isFinite(max)) return 0;
  if (max <= 0 || value <= 0) return 0;
  return Math.max(3, Math.min(100, Math.round((value / max) * 100)));
}
