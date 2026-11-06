'use strict';
// 브라우저와 테스트가 함께 쓴다. classifyPoll 과 noResponseDetail 을 뺀 함수는
// sample-app src/live-page.js 의 같은 함수를 옮긴 것이다. 두 레포가 코드를 공유하지 않으므로
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
 * Tells what versions are answering: one, or a canary mix. A plain replacement
 * flips once and stays, so it never counts as a mix; only versions alternating
 * within the window do. A new version must answer a few times in a row first,
 * or the first canary answer would read as a finished replacement.
 */
function createVersionTracker() {
  const WINDOW_MILLIS = 10000;
  const CHANGE_NOTICE_MILLIS = 3000;
  const ANSWERS_TO_CONFIRM = 4;
  const RETAINED_MARKS = 20;
  let answers = [];
  let current = null;
  let change = null;
  const marks = [];

  const flips = () => answers.filter((answer, index) =>
    index > 0 && answer.version !== answers[index - 1].version).length;

  function trailingRun() {
    const newest = answers[answers.length - 1].version;
    let run = 0;
    for (let i = answers.length - 1; i >= 0; i--) {
      if (answers[i].version !== newest) break;
      run++;
    }
    return run;
  }

  return {
    record(version, atMs) {
      answers.push({version, atMs});
      answers = answers.filter((answer) => atMs - answer.atMs < WINDOW_MILLIS);
      const settled = flips() === 0 ||
          (flips() === 1 && trailingRun() >= ANSWERS_TO_CONFIRM);
      if (!settled || version === current) return;
      if (current !== null) {
        change = {from: current, to: version, atMs};
        marks.push({version, atMs});
        marks.splice(0, Math.max(0, marks.length - RETAINED_MARKS));
      }
      current = version;
    },
    /** Starts over for another deployment; its version is not a change. */
    reset() {
      answers = [];
      current = null;
      change = null;
    },
    /** Versions to show with their share of recent answers, largest first. */
    shares() {
      if (flips() < 2) return current ? [{version: current, percent: 100}] : [];
      const counts = new Map();
      for (const {version} of answers) {
        counts.set(version, (counts.get(version) || 0) + 1);
      }
      return [...counts]
          .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
          .map(([version, count]) => ({
            version, percent: Math.round(count * 100 / answers.length),
          }));
    },
    /** The change to announce right now, or null. */
    recentChange(nowMs) {
      return change && nowMs - change.atMs < CHANGE_NOTICE_MILLIS ?
          change : null;
    },
    marks() { return marks; },
  };
}

/**
 * Picks a version's color from its hash alone, so one version looks the same
 * on every screen. None is close to an environment color.
 */
function versionColor(version) {
  const COLORS = [
    '#c084fc', '#f472b6', '#a3e635', '#fde047', '#f5f5f4', '#c4a484',
  ];
  let hash = 2166136261;
  for (let index = 0; index < version.length; index++) {
    hash = Math.imul(hash ^ version.charCodeAt(index), 16777619) >>> 0;
  }
  return COLORS[hash % COLORS.length];
}

/** Splits chart bars into runs of one environment; a gap ends a run too. */
function splitChartSegments(bars) {
  const segments = [];
  let open = null;
  bars.forEach((bar, index) => {
    if (!bar) {
      open = null;
      return;
    }
    if (!open || open.environment !== bar.environment) {
      open = {environment: bar.environment, firstIndex: index, rates: []};
      segments.push(open);
    }
    open.rates.push(bar.rate);
  });
  return segments;
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
  module.exports = {
    classifyPoll, createIngestTracker, createVersionTracker, noResponseDetail,
    splitChartSegments, versionColor,
  };
}
