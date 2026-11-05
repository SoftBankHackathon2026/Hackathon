export type Bucket = { start: number; end: number };

const STEPS = [1, 2, 3, 4, 5, 6, 8, 10];

export function niceAxis(max: number, floor = 4) {
  const top = Number.isFinite(max) ? Math.max(max, floor) : floor;
  const raw = top / 4;
  const base = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / base;
  const step = (STEPS.find((s) => s >= fraction - 1e-9) ?? 10) * base;
  const ticks = Array.from({ length: 5 }, (_, i) =>
    Number((step * i).toPrecision(12)),
  );
  return { top: ticks[4], ticks };
}

function trimmed(value: number) {
  return String(Number(value.toFixed(1)));
}

export function compactCount(value: number) {
  if (value >= 1_000_000) return `${trimmed(value / 1_000_000)}M`;
  if (value >= 1000) return `${trimmed(value / 1000)}k`;
  return trimmed(value);
}

export function spreadIndexes(length: number, count = 7) {
  if (length <= count) return Array.from({ length }, (_, i) => i);
  const picked = Array.from({ length: count }, (_, i) =>
    Math.round((i * (length - 1)) / (count - 1)),
  );
  return picked.filter((value, i) => picked.indexOf(value) === i);
}

export function bucketPosition(buckets: Bucket[], time: number) {
  if (buckets.length === 0) return 0;
  if (time <= buckets[0].start) return 0;
  const found = buckets.findIndex((b) => time >= b.start && time < b.end);
  if (found < 0) return buckets.length;
  const b = buckets[found];
  return found + (time - b.start) / Math.max(b.end - b.start, 1);
}

export function scaleLinear(
  domain: [number, number],
  range: [number, number],
): (value: number) => number {
  const span = domain[1] - domain[0];
  if (span === 0) return () => (range[0] + range[1]) / 2;
  return (value) =>
    range[0] + ((value - domain[0]) / span) * (range[1] - range[0]);
}

function point(x: number, y: number) {
  return `${Number(x.toFixed(1))} ${Number(y.toFixed(1))}`;
}

export function linePath(xs: number[], ys: number[]) {
  let drawing = false;
  const parts: string[] = [];
  xs.forEach((x, i) => {
    if (!Number.isFinite(ys[i])) {
      drawing = false;
      return;
    }
    parts.push(`${drawing ? "L" : "M"}${point(x, ys[i])}`);
    drawing = true;
  });
  return parts.join(" ");
}

export function stackValues(layers: number[][]) {
  const length = layers[0]?.length ?? 0;
  const running = new Array<number>(length).fill(0);
  return layers.map((layer) => {
    const bottom = [...running];
    const top = layer.map((value, i) => {
      running[i] += Math.max(0, value);
      return running[i];
    });
    return { bottom, top };
  });
}

export function nearestIndex(xs: number[], target: number) {
  if (xs.length === 0) return -1;
  let low = 0;
  let high = xs.length - 1;
  while (high - low > 1) {
    const mid = (low + high) >> 1;
    if (xs[mid] < target) low = mid;
    else high = mid;
  }
  return Math.abs(xs[low] - target) <= Math.abs(xs[high] - target) ? low : high;
}
