"""Failure and retry contracts for traffic-bot-owned migration batches."""
from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from migration.bot_client import HttpBotClient, BotClientError
from migration.config import BotConfig, ConfigError
from migration.cutover import ABORTED, COMPLETED, NEEDS_RECOVERY, VERIFYING, CutoverError
from tests.test_cutover import fakes, _build, _simulate_replication


@pytest.mark.parametrize('field,value', [('unknown', 1), ('paused', True), ('fatal', 'disk-full'),
    ('control_enabled', False), ('generated_total', True), ('request_endpoint', 'http://wrong/api/requests')])
def test_precheck_refuses_unhealthy_or_wrong_bot(config, state_dir, fakes, field, value):
    original = fakes['bot'].status
    fakes['bot'].status = lambda: {**original(), field: value}
    cut = _build(config, state_dir, fakes)
    with pytest.raises(CutoverError): cut.run()
    assert cut.job.stage == ABORTED and fakes['db'].calls == []


def test_unconfirmed_request_blocks_fencing(config, state_dir, fakes):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes, approve='test')
    original = fakes['bot'].status
    fakes['bot'].status = lambda: {**original(), 'pending': 1}
    with pytest.raises(CutoverError, match='traffic_bot_settled'): cut.run()
    assert 'revoke:onprem' not in fakes['db'].calls


def test_ledger_replaced_after_ready_is_not_controlled(config, state_dir, fakes):
    from migration.cutover import ApprovalRequired
    _simulate_replication(fakes)
    with pytest.raises(ApprovalRequired): _build(config, state_dir, fakes).run()
    before = len(fakes['bot'].calls)
    fakes['bot'].ledger_id = 'replacement-ledger'
    cut = _build(config, state_dir, fakes, approve='test')
    with pytest.raises(CutoverError, match='원장/run_id'): cut.run()
    assert len(fakes['bot'].calls) == before
    assert 'revoke:onprem' not in fakes['db'].calls


def test_stop_failure_never_marks_completed(config, state_dir, fakes):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes, approve='test')
    original = fakes['bot'].set_generation
    def fail(enabled, **kwargs):
        if not enabled and cut.job.stage == VERIFYING:
            raise BotClientError('stop unavailable')
        return original(enabled, **kwargs)
    fakes['bot'].set_generation = fail
    with pytest.raises(CutoverError, match='stop unavailable'): cut.run()
    assert cut.job.stage == NEEDS_RECOVERY
    assert cut.job.checkpoint['bot_stop_error'] == 'BOT_STOP_FAILED'
    fakes['bot'].set_generation = original
    assert _build(config, state_dir, fakes, approve='test').run(force_retry=True) == COMPLETED
    assert fakes['bot'].generated == 5


def test_verifying_retry_finishes_saved_batch_not_new_batch(config, state_dir, fakes):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes, approve='test')
    original = fakes['bot'].set_generation
    interrupted = False
    def partial(enabled, **kwargs):
        nonlocal interrupted
        if enabled and kwargs.get('limit') == 5 and not interrupted:
            interrupted = True
            fakes['bot'].generated += 1
            fakes['bot'].on_batch(1)
            fakes['bot'].generation = 'running'
            return fakes['bot'].status()
        if cut.job.stage == VERIFYING and enabled:
            raise BotClientError('temporary control failure')
        return original(enabled, **kwargs)
    fakes['bot'].set_generation = partial
    with pytest.raises(CutoverError, match='temporary'): cut.run()
    assert cut.job.stage == NEEDS_RECOVERY and fakes['bot'].generated == 4
    fakes['bot'].set_generation = original
    assert _build(config, state_dir, fakes, approve='test').run(force_retry=True) == COMPLETED
    assert fakes['bot'].generated == 5 and fakes['db'].rows['aws'] == fakes['db'].rows['onprem'] + 2


def test_old_failed_job_rejection_preserves_checkpoint(config, state_dir, fakes):
    cut = _build(config, state_dir, fakes)
    cut.job.checkpoint.pop('writer_mode')
    cut._save(NEEDS_RECOVERY, failed_stage=VERIFYING)
    before = cut.store.load(config.job.job_id)
    with pytest.raises(CutoverError, match='옛 DB 워커'): cut.run(force_retry=True)
    assert cut.store.load(config.job.job_id) == before


def test_http_bot_contract_and_error_redaction():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization'), None))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"generation":"stopped"}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, self.headers.get('Authorization'), body))
            self.send_response(409); self.end_headers(); self.wfile.write(b'{"error":"secret-upstream"}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = HttpBotClient(BotConfig(control_url=f'http://127.0.0.1:{server.server_port}'), {'BOT_CONTROL_TOKEN': 'test-token'})
        assert client.status()['generation'] == 'stopped'
        with pytest.raises(BotClientError, match='HTTP 409') as err:
            client.set_generation(True, expected_run_id='run', expected_ledger_id='ledger', limit=100)
        assert 'secret' not in str(err.value) and 'test-token' not in str(err.value)
        assert calls[1] == ('/control/generation', 'Bearer test-token',
            {'enabled': True, 'expected_run_id': 'run', 'expected_ledger_id': 'ledger', 'limit': 100})
    finally:
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('url', ['file:///etc/config', 'http://user:pass@bot', 'http://bot?token=secret'])
def test_bot_config_rejects_credentials_and_non_http(url):
    with pytest.raises(ConfigError): BotConfig(control_url=url)


def test_queue_restart_after_resume_reconciles_before_releasing_bot(config, state_dir, fakes, monkeypatch):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes, approve='test')
    original_save = cut._save
    def crash(stage=None, **checkpoint):
        if stage == VERIFYING:
            raise RuntimeError('crash after resume')
        original_save(stage, **checkpoint)
    monkeypatch.setattr(cut, '_save', crash)
    with pytest.raises(CutoverError): cut.run()
    fakes['ingress'].mode = 'paused'
    before = fakes['ingress'].calls.count('resume@1')
    assert _build(config, state_dir, fakes, approve='test').run(force_retry=True) == COMPLETED
    assert fakes['ingress'].calls.count('resume@1') == before + 1
    assert fakes['bot'].generated == 5


def test_snapshot_adapter_never_opens_retired_worker_ledger(config, state_dir, monkeypatch):
    from migration.cutover import VerifyAdapter
    from migration import verify
    from types import SimpleNamespace
    def run(*args, **kwargs):
        assert kwargs['state_dir'] is None
        return SimpleNamespace(passed=True, counts={}, method='digest', mismatched_buckets=[], source_count=800, target_count=800)
    monkeypatch.setattr(verify, 'run', run)
    assert VerifyAdapter(config, state_dir, state_dir).run('pre', 'snapshot', method='digest')['passed']
