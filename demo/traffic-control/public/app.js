'use strict';
// 상태판 2초 폴링 + 서명한 명령 전송. 서버가 준 문자열은 textContent 로만 넣는다.
(function () {
  const POLL_MILLIS = 2000;
  const KEY_STORAGE = 'demo-traffic-operator-key';
  const NAMES = {worker: 'INSERT 워커', bot: 'traffic-bot'};
  const ACTIONS = {start: '시작', stop: '멈춤'};
  const STATUS = {queued: '대기', sent: '에이전트 실행 중', done: '완료', rejected: '거절',
    expired: '만료'};
  const BLOCKED = {
    AGENT_STALE: '온프렘 에이전트 보고가 끊겼습니다',
    CUTOVER_LOCK: '전환이 진행 중입니다',
    TARGET_NOT_CONTROLLABLE: '이 봇은 지금 버튼으로 제어할 수 없습니다',
    COMMAND_IN_PROGRESS: '이전 명령을 처리하는 중입니다',
  };

  const keyInput = document.getElementById('operator-key');
  let latest = null;
  let sending = false;

  function storage() {
    try { return window.sessionStorage; } catch { return null; }
  }
  try { keyInput.value = storage()?.getItem(KEY_STORAGE) ?? ''; } catch { /* private mode */ }
  keyInput.addEventListener('input', () => {
    try { storage()?.setItem(KEY_STORAGE, keyInput.value); } catch { /* ignore */ }
    render();
  });
  document.getElementById('forget-key').addEventListener('click', () => {
    keyInput.value = '';
    try { storage()?.removeItem(KEY_STORAGE); } catch { /* ignore */ }
    render();
  });

  function fmt(n) {
    return typeof n === 'number' ? n.toLocaleString('ko-KR') : '—';
  }

  function stateLabel(b) {
    if (!b.reachable) return ['연결 안 됨', 'unknown'];
    if (b.mode === 'budget') return ['시연 예산 모드', 'unknown'];
    if (b.generation === 'running') return ['생성 중', 'running'];
    if (b.generation === 'stopped') return ['멈춤', 'stopped'];
    return ['미지원 버전', 'unknown'];
  }

  function rows(target, b) {
    const out = [];
    if (target === 'worker') {
      out.push(['DB', b.target], ['워커 상태', b.state], ['초당', fmt(b.rate_per_second)],
        ['대기열', fmt(b.pending)], ['누적 커밋', fmt(b.committed)]);
      if (b.mode === 'budget') {
        out.push(['생성 / 상한', `${fmt(b.generated_total)} / ${fmt(b.generation_limit)}`]);
      }
    } else {
      out.push(['초당', fmt(b.rate_per_second)], ['미확인', fmt(b.pending)],
        ['확인 완료', fmt(b.committed)], ['실패', fmt(b.failed)]);
    }
    return out;
  }

  function renderCard(card) {
    const target = card.dataset.target;
    const b = latest?.bots?.[target] ?? {reachable: false};
    const [label, cls] = stateLabel(b);
    const stateEl = card.querySelector('.state');
    stateEl.textContent = label;
    stateEl.className = `state ${cls}`;
    const dl = card.querySelector('dl');
    dl.replaceChildren();
    for (const [k, v] of rows(target, b)) {
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v ?? '—';
      dl.append(dt, dd);
    }
    const blocked = latest?.blocked?.[target];
    const note = card.querySelector('.note');
    note.textContent = b.reason || (blocked ? BLOCKED[blocked] ?? blocked : '') ||
      (keyInput.value ? '' : '운영자 키를 넣으면 버튼이 켜집니다');
    for (const button of card.querySelectorAll('button')) {
      const action = button.dataset.action;
      const already = (action === 'start' && b.generation === 'running') ||
        (action === 'stop' && b.generation === 'stopped');
      button.disabled = sending || !latest || Boolean(blocked) || !keyInput.value || already;
    }
  }

  function render() {
    const agent = document.getElementById('agent');
    if (!latest) {
      agent.textContent = '서버 응답 없음'; agent.className = 'pill bad';
    } else if (latest.agent.fresh) {
      agent.textContent = `에이전트 연결됨 · ${latest.agent.age_seconds}초 전 보고`;
      agent.className = 'pill ok';
    } else {
      agent.textContent = latest.agent.last_report_at ? '에이전트 보고 끊김' : '에이전트 보고 없음';
      agent.className = 'pill bad';
    }
    document.getElementById('clock').textContent = latest ?
      new Date(latest.now).toLocaleTimeString('ko-KR') : '';

    const lock = document.getElementById('lock');
    const reasons = latest?.lock?.locked ? latest.lock.reasons : [];
    lock.hidden = !latest?.lock?.locked;
    const list = document.getElementById('lock-reasons');
    list.replaceChildren(...reasons.map((r) => {
      const li = document.createElement('li'); li.textContent = r; return li;
    }));

    document.querySelectorAll('.card').forEach(renderCard);

    const tbody = document.getElementById('commands');
    const commands = latest?.commands ?? [];
    tbody.replaceChildren(...(commands.length ? commands : [null]).map((c) => {
      const tr = document.createElement('tr');
      const cells = c ? [new Date(c.created_at).toLocaleTimeString('ko-KR'), NAMES[c.target],
        ACTIONS[c.action], `${STATUS[c.status] ?? c.status}${c.error ? ` · ${c.error}` : ''}`] :
        ['없음', '', '', ''];
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
    render();
    setTimeout(poll, POLL_MILLIS);
  }

  async function sendCommand(target, action) {
    const body = JSON.stringify({target, action});
    const header = window.DemoAuth.authHeader(keyInput.value,
      {role: 'operator', method: 'POST', path: '/api/commands', body});
    sending = true; render();
    try {
      const response = await fetch('/api/commands', {method: 'POST', body,
        headers: {'Content-Type': 'application/json', 'X-Demo-Auth': header}});
      if (!response.ok) {
        const error = (await response.json().catch(() => ({}))).error ?? response.status;
        window.alert(error === 'AUTH_SIGNATURE' ? '운영자 키가 맞지 않습니다' :
          `${NAMES[target]} ${ACTIONS[action]} 실패: ${BLOCKED[error] ?? error}`);
      }
    } catch {
      window.alert('서버에 보내지 못했습니다');
    } finally {
      sending = false;
    }
    render();
  }

  document.querySelectorAll('.card').forEach((card) => {
    card.querySelectorAll('button').forEach((button) => {
      button.addEventListener('click', () =>
        sendCommand(card.dataset.target, button.dataset.action));
    });
  });

  render();
  poll();
})();
