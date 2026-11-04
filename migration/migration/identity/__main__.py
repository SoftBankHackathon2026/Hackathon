"""python -m migration.identity: explicit offline and live command boundaries."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from . import SCHEMA_VERSION
from .collect import collect
from .config import load_spec, read, write
from .executor import execute, rollback_plan
from .models import ConfigError, IdentityError, Inventory, Plan, digest, primitive
from .planner import COVERAGE, make_plan
from .preflight import preflight, validate_plan, validate_review
from .report import markdown, report
from .state import State


def parser():
    p = argparse.ArgumentParser(description='범위를 명시한 계정·권한 이관 도구 (실제 접근 검증과 구성 변경은 별도)')
    commands = p.add_subparsers(dest='command', required=True)
    c = commands.add_parser('collect', help='읽기 전용 수집 또는 익명 자료 가져오기')
    c.add_argument('--spec', required=True); c.add_argument('--out', required=True)
    m = c.add_mutually_exclusive_group(required=True)
    m.add_argument('--live', action='store_true'); m.add_argument('--inventory', help='오프라인 자료 가져오기; 실환경 수집으로 표시하지 않음')
    c = commands.add_parser('plan', help='자격증명 없이 계획 및 차단 사유 생성')
    c.add_argument('--spec', required=True); c.add_argument('--inventory', required=True); c.add_argument('--out', required=True)
    for name in ('preflight','apply','resume','verify','rollback-plan','report'):
        c = commands.add_parser(name)
        c.add_argument('--plan', required=True)
        if name in {'preflight','apply','resume','verify'}:
            c.add_argument('--live', action='store_true', help='명시적으로 제품 API 연결 허용')
        if name in {'apply','resume'}:
            c.add_argument('--review-ref', required=True, help='정확한 계획 식별값·검토자·기록 참조를 담은 JSON 파일')
        if name in {'apply','resume','verify','rollback-plan'}:
            c.add_argument('--state-dir', required=True)
        if name == 'report':
            c.add_argument('--state-dir'); c.add_argument('--markdown')
        if name in {'rollback-plan','report'}:
            c.add_argument('--out', required=True)
        if name == 'verify':
            c.add_argument('--report', required=True)
    c = commands.add_parser('status')
    c.add_argument('--state-dir', required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'collect':
            spec = load_spec(args.spec)
            if args.live:
                from .registry import from_environment
                inventory = collect(spec, from_environment(spec.source), from_environment(spec.target))
            else:
                inventory = Inventory.from_dict(read(args.inventory))
                if inventory.spec_digest != digest(spec):
                    raise ConfigError('inventory is bound to another specification')
            write(args.out, inventory)
            complete = all(all(side['coverage'].get(k) in {'complete','not_applicable'} for k in COVERAGE) for side in (inventory.source,inventory.target))
            print(json.dumps({'status':'collected' if args.live else 'offline_imported', 'complete':complete}))
            return 0 if complete else 4
        if args.command == 'plan':
            plan = make_plan(load_spec(args.spec), Inventory.from_dict(read(args.inventory)))
            write(args.out, plan)
            print(json.dumps({'plan_digest':plan.plan_digest,'blockers':plan.blockers}))
            return 4 if plan.blockers else 0
        if args.command == 'status':
            if not (Path(args.state_dir)/'identity.sqlite3').is_file():
                raise ConfigError('identity state database does not exist')
            with State(args.state_dir) as state:
                print(json.dumps({'operations': state.results()}))
            return 0
        plan = Plan.from_dict(read(args.plan))
        if args.command == 'preflight':
            if args.live:
                from .registry import from_environment
                result = preflight(plan, from_environment(plan.spec.source), from_environment(plan.spec.target))
            else:
                result = preflight(plan)
            print(json.dumps(result)); return 0
        if args.command in {'apply','resume'}:
            if not args.live:
                raise ConfigError('apply and resume require --live; offline plans never write')
            validate_plan(plan)
            review = read(args.review_ref)
            validate_review(plan, review)
            from .registry import from_environment
            with State(args.state_dir) as state:
                try:
                    result = execute(plan, review, state, from_environment(plan.spec.source), from_environment(plan.spec.target))
                except IdentityError as exc:
                    if exc.exit_code not in {2,6} and any(row['status'] in {'applied','reconciled'} for row in state.results(plan.plan_digest)):
                        print(json.dumps({'status':'partially_applied','message':str(exc)}),file=sys.stderr)
                        return 5
                    raise
            print(json.dumps(result)); return 0
        if args.command == 'verify':
            from .verifier import from_environment as probe_factory, pinned_identity, verify
            probe = None
            if args.live:
                validate_plan(plan)
                from .registry import from_environment
                operator = from_environment(plan.spec.target)
                if not (Path(args.state_dir)/'identity.sqlite3').is_file():
                    raise ConfigError('live verification requires existing identity execution state')
                with State(args.state_dir) as state:
                    stable_id = pinned_identity(plan, state, operator)
                probe = probe_factory(plan, operator, stable_id)
            result = verify(plan, probe, live=args.live)
            with State(args.state_dir) as state:
                state.record_verification(plan.plan_digest, result)
            write(args.report,result)
            print(json.dumps({'status':result['status'],'live_access_verified':result['live_access_verified']}))
            return 0 if result['status']=='passed' else 4
        if args.command == 'rollback-plan':
            if not (Path(args.state_dir)/'identity.sqlite3').is_file():
                raise ConfigError('identity state database does not exist')
            with State(args.state_dir) as state:
                write(args.out, rollback_plan(plan,state))
            return 0
        if args.command == 'report':
            if args.state_dir:
                with State(args.state_dir) as state:
                    result = report(plan,state)
            else:
                result = report(plan)
            write(args.out,result)
            if args.markdown:
                path=Path(args.markdown); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                path.write_text(markdown(result)); path.chmod(0o600)
            return 0
    except IdentityError as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)}),file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print(json.dumps({'error':'ConfigError','message':'invalid artifact or inaccessible local path'}),file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({'error':'IdentityError','message':'provider or execution error; sensitive details withheld'}),file=sys.stderr)
        return 1
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
