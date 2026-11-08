#!/usr/bin/env python3
"""Repeated onprem -> AWS demos via application controls; no kubectl apply/scale/delete.

Deploy the demo supervisor and full migration GitOps overlays once before use.
Run from the workspace root, or supply both kubeconfig paths explicitly.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request


def validate_cycle(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", value):
        raise ValueError("cycle must be 1-48 lowercase letters, digits or underscores")
    return value


class Demo:
    def __init__(self, args):
        self.args = args
        self.file = Path(args.state_dir) / (args.cycle + '.json')
        self.state = json.loads(self.file.read_text()) if self.file.exists() else {}

    def save(self, **changes):
        self.state.update(changes)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix('.tmp')
        with tmp.open('w') as out:
            json.dump(self.state, out); out.flush(); os.fsync(out.fileno())
        tmp.replace(self.file)

    def api(self, route, body=None):
        headers = {'Content-Type': 'application/json', 'Origin': self.args.platform.rstrip('/')}
        if os.environ.get('ONEPORT_OPERATOR_TOKEN'):
            headers['Authorization'] = 'Bearer ' + os.environ['ONEPORT_OPERATOR_TOKEN']
        request = urllib.request.Request(self.args.platform.rstrip('/') + '/api/v1/' + route,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def exec(self, role, command, input=None):
        cloud = role == 'destination'
        deployment = {'bot': 'traffic-bot', 'source': 'durable-ingress',
                      'destination': 'service-receipt', 'runner': 'live-db-cutover'}[role]
        argv = ['kubectl', '--kubeconfig', self.args.aws if cloud else self.args.onprem,
                '--request-timeout=180s', '-n', 'service-migration' if cloud else 'hackathon',
                'exec', '-i', 'deploy/' + deployment, '--', *command]
        result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=180)
        if result.returncode:
            # Do not echo arbitrary subprocess output: it may include connection details.
            try:
                code = json.loads(result.stdout).get('error', 'COMMAND_FAILED')
            except (ValueError, AttributeError):
                code = 'COMMAND_FAILED'
            raise RuntimeError(f'{role}: {code}; check deployment, permissions and logs')
        return json.loads(result.stdout)

    def control(self, role, action):
        return self.exec(role, ['node', 'packages/demo-supervisor.js', action, self.args.cycle])

    def database(self, action):
        return self.exec('runner', ['python', '-m', 'migration.demo_reset',
            '--cycle', self.args.cycle, '--confirm-reset', self.args.cycle, action])

    def idle_commands(self):
        fleet = self.api('fleet')
        for job in fleet['migrations']:
            # Commands, rather than old checkpoints, show whether a runner is executing now.
            commands = self.api('migrations/' + job['job_id'] + '/commands')
            if any(c['status'] in ('pending', 'running') for c in commands):
                raise RuntimeError('Finish or cancel the outstanding platform command first: ' + job['job_id'])

    def reset(self):
        if self.args.confirm_reset != self.args.cycle:
            raise ValueError('reset requires --confirm-reset with the exact cycle ID')
        if self.state.get('phase') not in (None, 'resetting'):
            raise RuntimeError('Cycle already prepared/used; choose a NEW cycle ID')
        self.idle_commands()
        for role in ('bot', 'source', 'destination'):
            self.control(role, 'status')  # Missing software/credentials fail before stopping anything.
        if self.state.get('phase') != 'resetting':
            self.database('check-cycle')
            for role in ('bot', 'source', 'destination'):
                self.control(role, 'check-cycle')
        snapshot = self.database('inspect')
        if snapshot['connectors'] or snapshot.get('active_cutovers') or any(
                d['active_slots'] for d in snapshot['databases'].values()):
            raise RuntimeError('CDC/runner is active; resolve it before resetting demo data')
        self.save(phase='resetting', cycle=self.args.cycle)
        self.database('begin-reset')
        self.idle_commands()
        for role in ('bot', 'source', 'destination'):
            self.control(role, 'stop')
        # Reset is deliberately not a rollback: both DBs close, then old demo
        # rows and receipt/bot ledgers are retained under the cycle archive.
        self.database('reset-db')
        for role in ('destination', 'source', 'bot'):
            self.control(role, 'reset')
        self.database('open-source')
        for role in ('destination', 'source', 'bot'):
            self.control(role, 'start')
        deadline = time.monotonic() + 60
        for role in ('destination', 'source', 'bot'):
            while True:
                try:
                    self.control(role, 'ready')
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
        self.idle_commands()
        self.database('finish-reset')
        self.save(phase='prepared')
        return {'cycle': self.args.cycle, 'phase': 'prepared', 'source_rows': 800,
                'target_rows': 0, 'generation': 'stopped', 'next': 'start'}

    def register(self):
        if self.state.get('job_id'):
            return self.state['job_id']
        result = self.api('migrations', {'source_env': 'onprem', 'target_env': 'aws',
            'data_source': 'onprem_return', 'data_target': 'aws',
            'source_provider': 'onprem', 'target_provider': 'aws', 'apps': ['sample-app-onprem']})
        job = result['job_id']
        self.save(job_id=job)
        return job

    def map_data(self, job):
        mapping = self.api(f'migrations/{job}/data-mapping')
        target = 'managed:postgres:oneaction-review-db'
        if target not in [x['id'] for x in mapping['targets']]:
            raise RuntimeError('Existing demonstration RDS is missing from inventory')
        bindings = []
        for source in mapping['sources']:
            if source['kind'] == 'postgres':
                if source['id'] != 'cluster:hackathon/postgres':
                    raise RuntimeError('Unexpected PostgreSQL source; review the mapping')
                bindings.append({'source': source['id'], 'action': 'map', 'target': target})
            else:
                bindings.append({'source': source['id'], 'action': 'exclude',
                    'reason': '반복 데모는 샘플앱 DB와 접수 원장만 이전하며 기존 CDC 브로커를 사용합니다.'})
        self.api(f'migrations/{job}/data-mapping', {
            'inventory_hash': mapping['inventory_hash'], 'bindings': bindings})

    def start(self):
        if self.state.get('phase') != 'prepared':
            raise RuntimeError('Run reset with a new cycle ID first')
        # Child processes start asynchronously. Wait for all three app processes,
        # then let the runner perform the full HTTP/DB precheck.
        for role in ('bot', 'source', 'destination'):
            state = self.control(role, 'status')
            if state['cycle'] != self.args.cycle or state['stopped'] or not state['child_running']:
                raise RuntimeError(f'{role} is not running this cycle')
        self.idle_commands()
        job = self.register()
        self.map_data(job)
        # Persist before submitting: a lost response must never submit a second run.
        self.save(phase='submitted')
        self.api(f'migrations/{job}/commands', {'action': 'run', 'note': 'demo cycle ' + self.args.cycle})
        return self.wait(job, {'READY', 'COMPLETED'})

    def approve(self):
        job = self.state.get('job_id')
        if not job:
            raise RuntimeError('No registered cycle')
        if self.api('migrations/' + job)['summary']['stage'] != 'READY':
            raise RuntimeError('Approval requires READY; inspect status first')
        self.api(f'migrations/{job}/commands', {'action': 'approve', 'approval_id': 'demo_' + self.args.cycle})
        return self.wait(job, {'COMPLETED'})

    def wait(self, job, expected):
        deadline = time.monotonic() + self.args.wait
        previous = None
        while time.monotonic() < deadline:
            summary = self.api('migrations/' + job)['summary']
            if summary['stage'] != previous:
                previous = summary['stage']; print(json.dumps({'job_id': job, 'stage': previous}), flush=True)
            if summary['stage'] in expected:
                self.save(phase=summary['stage'].lower()); return summary
            commands = self.api(f'migrations/{job}/commands')
            if summary['stage'] in ('ABORTED', 'NEEDS_RECOVERY') or (commands and commands[0]['status'] == 'failed'):
                raise RuntimeError('Migration stopped; inspect status. Do not reset with the same cycle ID.')
            time.sleep(5)
        return {'job_id': job, 'waiting': True, 'next': 'status'}

    def status(self):
        result = {'cycle': self.args.cycle, 'local': self.state}
        if self.state.get('job_id'):
            result['migration'] = self.api('migrations/' + self.state['job_id'])['summary']
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('reset', 'start', 'approve', 'status'))
    p.add_argument('--cycle', required=True, type=validate_cycle)
    p.add_argument('--confirm-reset')
    p.add_argument('--onprem', default=os.getenv('DEMO_ONPREM_KUBECONFIG', '.local/hackathon.yaml'))
    p.add_argument('--aws', default=os.getenv('DEMO_AWS_KUBECONFIG', '.local/oneaction-eks.yaml'))
    p.add_argument('--platform', default='https://platform.yubin.dev')
    p.add_argument('--state-dir', default='.local/demo-cycles')
    p.add_argument('--wait', type=int, default=900)
    args = p.parse_args()
    try:
        if args.action == 'status':
            print(json.dumps(Demo(args).status(), ensure_ascii=False, indent=2))
            return 0
        Path(args.state_dir).mkdir(parents=True, exist_ok=True)
        with (Path(args.state_dir) / 'operator.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(getattr(Demo(args), args.action)(), ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr); return 2
    except Exception as e:
        print('Demo command failed: ' + type(e).__name__, file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
