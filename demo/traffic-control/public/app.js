'use strict';
// 상태판 2초 폴링 + 서명한 명령 전송. 서버가 준 문자열은 textContent 로만 넣는다.
(function () {
  const POLL_MILLIS = 2000;
  const SPEED_WINDOW_MILLIS = 10000;
  const KEY_STORAGE = 'demo-traffic-operator-key';
  // 접수 큐(durable-ingress) 대상 이름 → 화면 문구. 모르는 이름은 그대로 보여 준다.
  const ROUTES = {
    onprem: ['온프렘 DB', '온프렘 앱 → k3s PostgreSQL'],
    aws_db: ['AWS RDS', '온프렘 앱 → AWS RDS'],
    aws_eks: ['AWS', 'AWS EKS 앱 → AWS RDS'],
    rehearsal: ['온프렘 리허설 DB', '리허설용 DB'],
  };
  const ACTIONS = {start: '시작', stop: '멈춤'};
  const STATUS = {queued: '대기', sent: '실행 중', done: '완료', rejected: '거절', expired: '만료'};
  const BLOCKED = {
    AGENT_STALE: '온프렘과 연결이 끊겨 지금은 누를 수 없습니다',
    CUTOVER_LOCK: 'DB 이전이 진행 중이라 잠겨 있습니다',
    TARGET_NOT_CONTROLLABLE: '봇이 아직 켜고 끄기를 지원하지 않습니다',
    COMMAND_IN_PROGRESS: '앞의 명령을 처리하는 중입니다',
  };

  const $ = (id) => document.getElementById(id);
  const keyInput = $('operator-key');
  let latest = null;
  let sending = false;
  const samples = [];   // [{at, committed}] 최근 저장 확인 누적치

  function storage() {
    try { return window.sessionStorage; } catch { return null; }
  }
  try { keyInput.value = storage()?.getItem(KEY_STORAGE) ?? ''; } catch { /* private mode */ }
  keyInput.addEventListener('input', () => {
    try { storage()?.setItem(KEY_STORAGE, keyInput.value); } catch { /* ignore */ }
    render();
  });
  $('forget-key').addEventListener('click', () => {
    keyInput.value = '';
    try { storage()?.removeItem(KEY_STORAGE); } catch { /* ignore */ }
    render();
  });

  function fmt(n) {
    return typeof n === 'number' ? n.toLocaleString('ko-KR') : '—';
  }

  function recordSample(bot) {
    if (typeof bot?.committed !== 'number') return;
    const now = Date.now();
    samples.push({at: now, committed: bot.committed});
    while (samples.length && now - samples[0].at > SPEED_WINDOW_MILLIS + POLL_MILLIS) samples.shift();
  }

  function speed() {
    if (samples.length < 2) return null;
    const first = samples[0];
    const last = samples[samples.length - 1];
    const seconds = (last.at - first.at) / 1000;
    if (seconds < 3) return null;
    return Math.max(0, (last.committed - first.committed) / seconds);
  }

  function renderRoute() {
    const route = latest?.route;
    if (!route?.known) {
      $('where').textContent = '확인 못 함';
      $('where-note').textContent = '접수 큐 상태를 읽지 못했습니다';
      return;
    }
    const [name, note] = ROUTES[route.target] ?? [route.target ?? '알 수 없음', ''];
    $('where').textContent = name;
    $('where-note').textContent = route.mode === 'paused' ?
      '접수 큐가 잠시 대기 중 (요청은 계속 받고 전환 뒤 저장)' : note;
  }

  function renderBot() {
    const b = latest?.bots?.bot ?? {reachable: false};
    let label = '확인 중'; let ja = ''; let cls = 'unknown'; let desc = '';
    if (latest && !b.reachable) {
      label = '봇 연결 안 됨'; ja = 'ボットに接続できません';
      desc = b.reason || '온프렘의 트래픽 봇에 닿지 않습니다';
    } else if (b.generation === 'running') {
      label = '요청 보내는 중'; ja = 'リクエスト送信中'; cls = 'running';
      desc = `초당 ${fmt(b.rate_per_second)}건씩 새 요청을 만들어 보냅니다`;
    } else if (b.generation === 'stopped') {
      label = '멈춤'; ja = '停止中'; cls = 'stopped';
      desc = '새 요청을 만들지 않습니다. 남은 요청은 끝까지 저장을 확인합니다';
    }
    $('state').textContent = label;
    $('state').className = `state ${cls}`;
    $('state-ja').textContent = ja;
    $('state-desc').textContent = desc;

    const s = speed();
    $('speed').textContent = s === null ? '측정 중' : `초당 ${s.toFixed(1)}건`;
    $('committed').textContent = fmt(b.committed);
    $('pending').textContent = fmt(b.pending);
    $('failed').textContent = fmt(b.failed);

    const blocked = latest?.blocked?.bot;
    for (const [id, action] of [['start', 'start'], ['stop', 'stop']]) {
      const already = (action === 'start' && b.generation === 'running') ||
        (action === 'stop' && b.generation === 'stopped');
      $(id).disabled = sending || !latest || Boolean(blocked) || !keyInput.value || already;
    }
    $('button-note').textContent = blocked ? (BLOCKED[blocked] ?? blocked) :
      (keyInput.value ? '' : '버튼은 운영자만 누를 수 있습니다 (아래 "운영자 전용")');
  }

  function render() {
    const fresh = latest?.agent?.fresh;
    $('agent-dot').className = `dot ${latest ? (fresh ? 'ok' : 'bad') : 'bad'}`;
    $('agent').textContent = !latest ? '제어 서버 응답 없음' :
      fresh ? '온프렘 연결됨 · オンプレ接続中' : '온프렘 연결 끊김 · 接続切れ';
    $('clock').textContent = latest ?
      `${new Date(latest.now).toLocaleTimeString('ko-KR')} 기준` : '';

    const locked = latest?.lock?.locked && fresh;
    $('lock').hidden = !locked;
    $('lock-reasons').replaceChildren(...(locked ? latest.lock.reasons : []).map((r) => {
      const li = document.createElement('li'); li.textContent = r; return li;
    }));

    renderRoute();
    renderBot();

    const commands = latest?.commands ?? [];
    $('commands').replaceChildren(...(commands.length ? commands : [null]).map((c) => {
      const tr = document.createElement('tr');
      const cells = c ? [new Date(c.created_at).toLocaleTimeString('ko-KR'), ACTIONS[c.action],
        `${STATUS[c.status] ?? c.status}${c.error ? ` · ${c.error}` : ''}`] : ['없음', '', ''];
      for (const value of cells) {
        const td = document.createElement('td'); td.textContent = value; tr.append(td);
      }
      return tr;
    }));
  }

  async function poll() {
    try {
      const response = await fetch('/api/state', {cache: 'no-store'});
      latest = response.ok ? await response.json() : null;
    } catch {
      latest = null;
    }
    recordSample(latest?.bots?.bot);
    render();
    setTimeout(poll, POLL_MILLIS);
  }

  async function sendCommand(action) {
    const body = JSON.stringify({target: 'bot', action});
    const header = window.DemoAuth.authHeader(keyInput.value,
        {role: 'operator', method: 'POST', path: '/api/commands', body});
    sending = true; render();
    try {
      const response = await fetch('/api/commands', {method: 'POST', body,
        headers: {'Content-Type': 'application/json', 'X-Demo-Auth': header}});
      if (!response.ok) {
        const error = (await response.json().catch(() => ({}))).error ?? response.status;
        window.alert(error === 'AUTH_SIGNATURE' ? '운영자 키가 맞지 않습니다' :
          `${ACTIONS[action]} 실패: ${BLOCKED[error] ?? error}`);
      }
    } catch {
      window.alert('서버에 보내지 못했습니다');
    } finally {
      sending = false;
    }
    render();
  }

  $('start').addEventListener('click', () => sendCommand('start'));
  $('stop').addEventListener('click', () => sendCommand('stop'));

  render();
  poll();
})();
