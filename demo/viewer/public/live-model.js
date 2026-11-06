'use strict';
// 브라우저와 테스트가 함께 쓴다. createIngestTracker 는 sample-app
// src/live-page.js 의 같은 함수를 옮긴 것이다. 두 레포가 코드를 공유하지 않으므로
// 계산 규칙을 바꾸면 양쪽을 함께 고친다.

/**
 * Turns successive row counts into rows per second. Counts only grow in this
 * insert-only table, so growth is measured against the highest count seen:
 * replicas answering out of step cannot inflate it.
 */
function createIngestTracker() {
  const MAX_FILLED_MILLIS = 3000;
  const REBUILT_DATABASE_RATIO = 0.9;
  const RETAINED_SECONDS = 120;
  const observedBySecond = new Map();
  let identity = null;
  let highestCount = 0;
  let lastObservedMs = 0;
  let lastWriteMs = null;
  let baselineSecond = 0;

  function startBaseline(sample) {
    identity = sample.identity;
    highestCount = sample.rowCount;
    lastObservedMs = sample.observedAtMs;
    lastWriteMs = sample.newestCreatedAtMs === null ? null :
        Math.min(sample.newestCreatedAtMs, sample.observedAtMs);
    // Seconds from here on may hold rows of the previous deployment, whose
    // clock can run ahead of this one.
    baselineSecond = Math.floor(sample.observedAtMs / 1000);
    for (const second of observedBySecond.keys()) {
      if (second >= baselineSecond) observedBySecond.delete(second);
    }
  }

  /**
   * Credits each second with its share of the interval's rows and with the
   * time observed, so a rate stays true however samples fall across seconds.
   * Only the newest part is credited: a long gap stays visibly unobserved
   * instead of being painted over with an average nobody measured.
   */
  function recordGrowth(growth, observedAtMs, environment) {
    const rowsPerMilli = growth / (observedAtMs - lastObservedMs);
    let from = Math.max(lastObservedMs, observedAtMs - MAX_FILLED_MILLIS);
    while (from < observedAtMs) {
      const second = Math.floor(from / 1000);
      const until = Math.min((second + 1) * 1000, observedAtMs);
      const bucket = observedBySecond.get(second) ||
          {rows: 0, millis: 0, environment};
      bucket.rows += rowsPerMilli * (until - from);
      bucket.millis += until - from;
      observedBySecond.set(second, bucket);
      from = until;
    }
    const oldest = Math.floor(observedAtMs / 1000) - RETAINED_SECONDS;
    for (const second of observedBySecond.keys()) {
      if (second < oldest) observedBySecond.delete(second);
    }
  }

  function buckets(endSecond, count) {
    return Array.from({length: count}, (unused, index) =>
      observedBySecond.get(endSecond - count + 1 + index));
  }

  const rateOf = (bucket) => bucket.rows * 1000 / bucket.millis;

  /** Observed buckets of the current deployment within the last seconds. */
  function bucketsSinceBaseline(endSecond, seconds) {
    const sinceBaseline = endSecond - baselineSecond + 1;
    const window = Math.max(0, Math.min(seconds, sinceBaseline));
    return buckets(endSecond, window).filter(Boolean);
  }

  return {
    /** Returns 'baseline', 'newer', or 'stale' for a sample to be ignored. */
    record(sample) {
      if (sample.identity !== identity) {
        startBaseline(sample);
        return 'baseline';
      }
      if (sample.observedAtMs <= lastObservedMs) return 'stale';
      // Replica lag is a few rows; a large fall means the database was rebuilt.
      if (sample.rowCount < highestCount * REBUILT_DATABASE_RATIO) {
        startBaseline(sample);
        return 'baseline';
      }
      const growth = Math.max(0, sample.rowCount - highestCount);
      recordGrowth(growth, sample.observedAtMs, sample.environment);
      highestCount = Math.max(highestCount, sample.rowCount);
      lastObservedMs = sample.observedAtMs;
      if (growth > 0) lastWriteMs = sample.observedAtMs;
      return 'newer';
    },
    /**
     * Drops the numbers of a deployment that was replaced by one whose rows
     * cannot be read yet. The chart keeps its bars: they show who served when.
     */
    forget() {
      identity = null;
      highestCount = 0;
      lastWriteMs = null;
      baselineSecond = Infinity;
    },
    /** Rates up to endSecond with their environment; null means unobserved. */
    bars(endSecond, count) {
      return buckets(endSecond, count).map((bucket) => (bucket ?
        {rate: rateOf(bucket), environment: bucket.environment} : null));
    },
    /** Highest rate of the current deployment; null when nothing observed. */
    peakRate(endSecond, seconds) {
      const observed = bucketsSinceBaseline(endSecond, seconds);
      return observed.length === 0 ? null : Math.max(...observed.map(rateOf));
    },
    /** Rows per second since the last baseline; null when nothing observed. */
    averageRate(endSecond, seconds) {
      const observed = bucketsSinceBaseline(endSecond, seconds);
      if (observed.length === 0) return null;
      const rows = observed.reduce((sum, bucket) => sum + bucket.rows, 0);
      const millis = observed.reduce((sum, bucket) => sum + bucket.millis, 0);
      return rows * 1000 / millis;
    },
    highestCount() { return highestCount; },
    lastWriteMs() { return lastWriteMs; },
  };
}

/**
 * Names what one poll means for a panel:
 * 'unset' | 'live' | 'no-response' | 'db-down' (the app answered, its DB did
 * not).
 */
function classifyPoll(result) {
  if (result.unset) return 'unset';
  if (!result.reachable || !result.live) return 'no-response';
  const {live, status} = result;
  // A gateway can answer JSON too, so only the app's own shape counts.
  const fromApp = typeof live.environment === 'string' &&
      typeof live.version === 'string' && typeof live.region === 'string';
  if (!fromApp) return 'no-response';
  if (status === 200 && live.database && Array.isArray(live.recent)) {
    return 'live';
  }
  return status === 503 && live.error ? 'db-down' : 'no-response';
}

/** A short reason for 'no-response'; 404 means /api/live is not deployed. */
function noResponseDetail(result) {
  if (!result.reachable) return result.error || '';
  if (result.status === 404) return 'HTTP 404: /api/live 미배포';
  return `HTTP ${result.status}`;
}

if (typeof module !== 'undefined') {
  module.exports = {createIngestTracker, classifyPoll, noResponseDetail};
}
