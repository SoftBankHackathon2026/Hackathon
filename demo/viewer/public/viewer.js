'use strict';
/* global classifyPoll, createIngestTracker, createVersionTracker */
/* global noResponseDetail, qrcode, splitChartSegments, versionColor */

const RENDER_MILLIS = 250;
const RATE_WINDOW_SECONDS = 10;
const CHART_SECONDS = 60;
// Samples arrive up to two seconds late; keep them off the chart's edge.
const CHART_LAG_SECONDS = 2;
const CHART_WIDTH = 600;
const CHART_HEIGHT = 120;
const CHART_TOP_MARGIN = 4;
const FULL_COMMIT_LENGTH = 40;
const SHORT_COMMIT_LENGTH = 7;
// The page inside the frame is laid out at this width, then scaled to fit.
const FRAME_VIRTUAL_WIDTH = 1600;
const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';
// The QR standard asks for four blank modules around the code.
const QR_QUIET_MODULES = 4;

let paused = false;

/** Draws a QR code as SVG, inside the browser: no network, works offline. */
function drawQrCode(svg, text) {
  const code = qrcode(0, 'M');
  code.addData(text);
  code.make();
  const modules = code.getModuleCount();
  const size = modules + QR_QUIET_MODULES * 2;
  let path = '';
  for (let row = 0; row < modules; row++) {
    for (let column = 0; column < modules; column++) {
      if (code.isDark(row, column)) {
        const x = column + QR_QUIET_MODULES;
        const y = row + QR_QUIET_MODULES;
        path += `M${x} ${y}h1v1h-1z`;
      }
    }
  }
  svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
  const dark = document.createElementNS(SVG_NAMESPACE, 'path');
  dark.setAttribute('d', path);
  svg.append(dark);
}

function createRow(row) {
  const tableRow = document.createElement('tr');
  const time = new Date(row.created_at).toLocaleTimeString('en-GB');
  // run_id and phase are request input: textContent keeps them inert.
  for (const text of [row.request_id_prefix, row.run_id, row.phase, time]) {
    const cell = document.createElement('td');
    cell.textContent = text;
    tableRow.append(cell);
  }
  return tableRow;
}

const shortVersion = (version) => (
  version.length === FULL_COMMIT_LENGTH ?
  version.slice(0, SHORT_COMMIT_LENGTH) : version);

function createVersionChip({version, percent}) {
  const chip = document.createElement('span');
  chip.className = 'version-chip';
  chip.style.background = versionColor(version);
  chip.textContent = percent === 100 ? shortVersion(version) :
      `${shortVersion(version)} ${percent}%`;
  return chip;
}

function createChartPath(className, path) {
  const shape = document.createElementNS(SVG_NAMESPACE, 'path');
  shape.setAttribute('class', className);
  shape.setAttribute('d', path);
  return shape;
}

/** Draws one environment's run as a line over a filled area. */
function createChartSegment({environment, firstIndex, rates}, largest) {
  const step = CHART_WIDTH / (CHART_SECONDS - 1);
  const xAt = (offset) => (firstIndex + offset) * step;
  const yAt = (rate) => CHART_HEIGHT -
      rate / largest * (CHART_HEIGHT - CHART_TOP_MARGIN);
  let line = rates.map((rate, offset) =>
    `${offset === 0 ? 'M' : 'L'}${xAt(offset)} ${yAt(rate)}`).join('');
  // A lone observed second still needs width to be visible.
  if (rates.length === 1) line += `h${step}`;
  const endX = xAt(Math.max(1, rates.length - 1));
  const area = `${line}L${endX} ${CHART_HEIGHT}L${xAt(0)} ${CHART_HEIGHT}Z`;
  const group = document.createElementNS(SVG_NAMESPACE, 'g');
  group.setAttribute('data-environment', environment);
  group.append(createChartPath('chart-area', area),
      createChartPath('chart-line', line));
  return group;
}

function createPanel(environment, pollMillis, timeoutMillis) {
  const root = document.getElementById('panel-template').content
      .firstElementChild.cloneNode(true);
  const part = (name) => root.querySelector(`[data-part="${name}"]`);
  const tracker = createIngestTracker();
  const versions = createVersionTracker();
  const lastAnswerMs = {app: Date.now(), database: Date.now()};
  let state = null;
  let shownEnvironment = null;
  let serverClockOffsetMs = -Infinity;
  let detail = '';
  let writesPaused = false;
  let shownShares = '';

  part('name').textContent = environment.name;
  part('name-ja').textContent = environment.nameJa;
  function loadFrame() {
    const frame = part('frame');
    if (frame.src) return;
    frame.src = `${environment.pageUrl}/`;
  }

  function fitFrame() {
    const box = part('frame-box');
    if (!box.clientWidth) return;
    const scale = box.clientWidth / FRAME_VIRTUAL_WIDTH;
    const frame = part('frame');
    frame.style.width = `${FRAME_VIRTUAL_WIDTH}px`;
    frame.style.height = `${Math.ceil(box.clientHeight / scale)}px`;
    frame.style.transform = `scale(${scale})`;
  }

  function renderIdentity(live) {
    root.dataset.environment = live.environment;
    part('responder').textContent = live.environment;
    part('region').textContent = live.region;
    if (live.environment !== shownEnvironment) versions.reset();
    versions.record(live.version, Date.now());
    writesPaused = live.writes_paused;
    part('banner').hidden = !live.writes_paused;
    shownEnvironment = live.environment;
  }

  function applyLive(live) {
    const observedAtMs = Date.parse(live.observed_at);
    const newest = live.recent[0];
    const outcome = tracker.record({
      identity: `${live.environment}/${live.database.name}`,
      environment: live.environment,
      rowCount: Number(live.database.row_count),
      observedAtMs,
      newestCreatedAtMs: newest ? Date.parse(newest.created_at) : null,
    });
    if (outcome === 'stale') return;
    serverClockOffsetMs = Math.max(
        outcome === 'baseline' ? -Infinity : serverClockOffsetMs,
        observedAtMs - Date.now());
    part('database').textContent = live.database.name;
    part('total').textContent = tracker.highestCount().toLocaleString('ko-KR');
    part('rows').replaceChildren(...live.recent.map(createRow));
  }

  /** A new deployment without a DB must not show the old one's data. */
  function clearDatabaseIfOtherEnvironment(live) {
    if (live.environment === shownEnvironment) return;
    part('database').textContent = '—';
    part('total').textContent = '—';
    part('rows').replaceChildren();
    tracker.forget();
  }

  function applyPoll(result) {
    state = classifyPoll(result);
    if (state === 'unset') return;
    if (state === 'no-response') {
      detail = noResponseDetail(result);
      // The app itself may be fine: an old image simply has no /api/live.
      if (result.reachable) loadFrame();
      return;
    }
    lastAnswerMs.app = Date.now();
    loadFrame();
    if (state === 'live') {
      lastAnswerMs.database = Date.now();
      applyLive(result.live);
    } else {
      clearDatabaseIfOtherEnvironment(result.live);
    }
    renderIdentity(result.live);
  }

  async function poll() {
    if (paused) {
      setTimeout(poll, pollMillis);
      return;
    }
    const startedMs = Date.now();
    let result;
    try {
      const response = await fetch(
          `/api/live?id=${encodeURIComponent(environment.id)}`, {
            cache: 'no-store',
            signal: AbortSignal.timeout(timeoutMillis + 1500),
          });
      result = await response.json();
    } catch {
      result = {reachable: false, error: '뷰어 서버 응답 없음'};
    }
    if (!paused) applyPoll(result);
    setTimeout(poll, Math.max(0, pollMillis - (Date.now() - startedMs)));
  }

  /** Draws a vertical line at each version change still on the chart. */
  function renderVersionMarks(endSecond, offsetMs) {
    const step = CHART_WIDTH / (CHART_SECONDS - 1);
    const lines = [];
    for (const {version, atMs} of versions.marks()) {
      const second = Math.floor((atMs + offsetMs) / 1000);
      const index = second - (endSecond - CHART_SECONDS + 1);
      if (index < 0 || index >= CHART_SECONDS) continue;
      const line = document.createElementNS(SVG_NAMESPACE, 'line');
      line.setAttribute('class', 'version-mark');
      line.setAttribute('x1', index * step);
      line.setAttribute('x2', index * step);
      line.setAttribute('y1', 0);
      line.setAttribute('y2', CHART_HEIGHT);
      line.setAttribute('stroke', versionColor(version));
      lines.push(line);
    }
    part('marks').replaceChildren(...lines);
  }

  function renderChart(endSecond, offsetMs) {
    const shown = tracker.bars(endSecond, CHART_SECONDS);
    const rates = shown.filter(Boolean).map((bar) => bar.rate);
    // The axis fits all it shows; its floor keeps a trickle from filling it.
    const largest = Math.max(1, ...rates);
    part('segments').replaceChildren(
        ...splitChartSegments(shown).map((segment) =>
          createChartSegment(segment, largest)));
    renderVersionMarks(endSecond, offsetMs);
    const peak = tracker.peakRate(endSecond, CHART_SECONDS);
    part('peak').textContent = peak === null ? '' :
        `최대 ${peak.toFixed(peak < 10 ? 1 : 0)}/초`;
  }

  function renderVersion() {
    const shares = versions.shares();
    const key = JSON.stringify(shares);
    if (shares.length > 0 && key !== shownShares) {
      shownShares = key;
      part('version').replaceChildren(...shares.map(createVersionChip));
    }
    const change = versions.recentChange(Date.now());
    part('version').classList.toggle('changed', change !== null);
    part('version-notice').hidden = change === null;
    if (change) {
      part('version-notice').textContent =
          `버전 변경 ${shortVersion(change.from)} → ${shortVersion(change.to)}`;
    }
  }

  function renderStatus() {
    const status = part('status');
    const seconds = (ms) => Math.floor((Date.now() - ms) / 1000);
    root.classList.toggle('unset', state === 'unset');
    root.classList.toggle('stale', state === 'no-response');
    status.classList.toggle('down',
        state === 'no-response' || state === 'db-down');
    part('detail').textContent = state === 'no-response' ? detail : '';
    if (state === 'no-response') {
      status.textContent = `응답 없음 ${seconds(lastAnswerMs.app)}초`;
    } else if (state === 'db-down') {
      status.textContent = `DB 응답 없음 ${seconds(lastAnswerMs.database)}초`;
    } else if (state === 'live') {
      status.textContent = '실시간';
    } else if (state === 'unset') {
      status.textContent = '';
    }
  }

  function render() {
    const offsetMs =
        serverClockOffsetMs === -Infinity ? 0 : serverClockOffsetMs;
    const nowMs = Date.now() + offsetMs;
    const nowSecond = Math.floor(nowMs / 1000);
    if (state !== 'no-response' && writesPaused) {
      // The banner says writes stopped; a fading average would contradict it.
      part('rate').textContent = '0.0';
    } else {
      // Nothing is being measured during an outage, so no rate is claimed.
      const rate = state === 'live' ?
          tracker.averageRate(nowSecond, RATE_WINDOW_SECONDS) : null;
      part('rate').textContent = rate === null ? '—' : rate.toFixed(1);
    }
    const lastWrite = tracker.lastWriteMs();
    part('last-write').textContent = lastWrite === null ? '—' :
        String(Math.max(0, Math.floor((nowMs - lastWrite) / 1000)));
    renderChart(nowSecond - CHART_LAG_SECONDS, offsetMs);
    renderVersion();
    renderStatus();
  }

  if (environment.publicUrl) {
    drawQrCode(part('qr'), `${environment.publicUrl}/`);
    part('local-note').hidden = true;
  } else {
    part('qr').setAttribute('hidden', '');
  }
  if (environment.pageUrl) {
    part('open').href = `${environment.pageUrl}/`;
    new ResizeObserver(fitFrame).observe(part('frame-box'));
  } else {
    part('open').hidden = true;
  }
  document.getElementById('grid').append(root);
  poll();
  return {render};
}

function setPaused(next) {
  paused = next;
  document.getElementById('paused').hidden = !paused;
}

document.addEventListener('keydown', (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  // event.code ignores the input language: a Korean IME turns P into ㅔ.
  if (event.code === 'KeyP') setPaused(!paused);
  if (event.code === 'KeyW') document.body.classList.toggle('frame-view');
  if (event.code === 'KeyF') {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  }
});

async function start() {
  const response = await fetch('/api/environments', {cache: 'no-store'});
  const config = await response.json();
  const seconds = config.pollMillis / 1000;
  document.getElementById('top-ko').textContent =
      `각 환경의 /api/live 를 ${seconds}초마다 요청한 결과입니다`;
  document.getElementById('top-ja').textContent =
      `各環境の /api/live に${seconds}秒ごとにリクエストした結果です`;
  const panels = config.environments.map((environment) =>
    createPanel(environment, config.pollMillis, config.timeoutMillis));
  setInterval(() => {
    if (!paused) panels.forEach((panel) => panel.render());
  }, RENDER_MILLIS);
}

start().catch(() => {
  document.getElementById('top-ko').textContent =
      '설정을 불러오지 못했습니다. config.json 을 확인해 주세요';
  document.getElementById('top-ja').textContent =
      '設定を読み込めませんでした。config.json を確認してください';
});
