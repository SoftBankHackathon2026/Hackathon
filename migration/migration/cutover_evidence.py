"""Read-only AWS RDS/app evidence for the demo gate; never executes cutover."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import policy_client as pc
from .config import Config, load_config
from .cutover_policy import execution_digest

MAX_BYTES = 1024 * 1024


class EvidenceError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceError("APP_REDIRECT_REFUSED")


def app_info(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise EvidenceError("APP_URL_INVALID")
    request = Request(url.rstrip('/') + '/api/info', headers={'Accept': 'application/json'})
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise EvidenceError("APP_RESPONSE_TOO_LARGE")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise EvidenceError("APP_RESPONSE_INVALID")
    return result


def describe_rds(identifier: str, region: str, profile: str | None) -> dict[str, Any]:
    command = ['aws', '--no-cli-pager', '--region', region]
    if profile:
        command += ['--profile', profile]
    command += ['rds', 'describe-db-instances', '--db-instance-identifier', identifier,
                '--output', 'json']
    result = subprocess.run(command, capture_output=True, timeout=30, check=False)
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise EvidenceError('RDS_QUERY_FAILED')
    return json.loads(result.stdout)


def collect(config: Config, *, identifier: str, region: str, approved_locations: list[str],
            app_url: str, app_environment: str, evidence_ttl_seconds: int,
            write_wait_max_seconds: int, rds_reader: Callable[[], dict[str, Any]],
            app_reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    target = config.targets[config.job.target]
    if target.kind != 'rds':
        raise EvidenceError('RDS_TARGET_REQUIRED')
    if (not identifier or not region or not app_environment or not approved_locations or
            any(not isinstance(v, str) or not v for v in approved_locations) or
            type(evidence_ttl_seconds) is not int or evidence_ttl_seconds <= 0 or
            type(write_wait_max_seconds) is not int or write_wait_max_seconds <= 0):
        raise EvidenceError('EXPLICIT_PLAN_REQUIRED')
    plan = {'source': config.job.source, 'target': config.job.target,
            'execution_digest': execution_digest(config),
            'approved_locations': approved_locations,
            'limits': {'evidence_ttl_seconds': evidence_ttl_seconds,
                       'write_wait_max_seconds': write_wait_max_seconds},
            # Bind collector selectors too; changing the observed app is a new plan.
            'collector': {'rds_identifier': identifier, 'region': region,
                          'app_url_digest': pc.digest_of(app_url),
                          'app_environment': app_environment}}
    plan['digest'] = pc.digest_of(plan)
    checks = {}
    try:
        instances = rds_reader()['DBInstances']
        if not isinstance(instances, list) or len(instances) != 1:
            raise EvidenceError('RDS_IDENTITY_MISMATCH')
        instance = instances[0]
        endpoint = instance['Endpoint']
        arn = instance['DBInstanceArn'].split(':')
        if (instance['DBInstanceIdentifier'] != identifier or
                endpoint['Address'].lower().rstrip('.') != target.host.lower().rstrip('.') or
                type(endpoint['Port']) is not int or endpoint['Port'] != target.port or
                len(arn) != 7 or arn[2] != 'rds' or arn[3] != region or arn[5:] != ['db', identifier]):
            raise EvidenceError('RDS_IDENTITY_MISMATCH')
        if instance['DBInstanceStatus'] != 'available' or type(instance['PubliclyAccessible']) is not bool:
            raise EvidenceError('RDS_FACTS_UNAVAILABLE')
        checks['target_db_exposure'] = pc.build_check('pass', {'public': instance['PubliclyAccessible']},
                                                    source='aws-rds.describe-db-instances')
        checks['target_location'] = pc.build_check('pass', {'location': arn[3]},
                                                 source='aws-rds.describe-db-instances')
    except Exception:
        # Never persist raw SDK/HTTP/CLI errors, endpoints or credentials.
        for name in ('target_db_exposure', 'target_location'):
            checks[name] = pc.build_check('error', {'code': 'RDS_EVIDENCE_UNAVAILABLE'}, source='aws-rds')
    try:
        info = app_reader()
        db = info['database']
        count = db['row_count']
        valid_count = ((type(count) is int and count >= 0) or
                       (isinstance(count, str) and count.isascii() and count.isdigit()))
        valid = (db['name'] == target.dbname and info['environment'] == app_environment and valid_count)
        checks['target_app_db'] = pc.build_check('pass' if valid else 'fail',
            {'db_connected': valid, 'order_query_ok': valid}, source='target-app.api-info')
    except Exception:
        checks['target_app_db'] = pc.build_check('error', {'code': 'APP_EVIDENCE_UNAVAILABLE'},
                                               source='target-app.api-info')
    doc = {'job_id': config.job.job_id, 'plan': plan, 'checks': checks}
    if pc.validate_input(pc.build_input(job_id=config.job.job_id, stage='pre_cutover',
                                       plan=plan, checks=checks)):
        raise EvidenceError('EVIDENCE_CONTRACT_INVALID')
    return doc


def write_atomic(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.policy-evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(doc, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'output', 'rds-identifier', 'region', 'app-url', 'app-environment'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--approved-location', action='append', required=True)
    p.add_argument('--evidence-ttl-seconds', type=int, required=True)
    p.add_argument('--write-wait-max-seconds', type=int, required=True)
    p.add_argument('--aws-profile')
    a = p.parse_args()
    try:
        doc = collect(load_config(a.config), identifier=a.rds_identifier, region=a.region,
            approved_locations=a.approved_location, app_url=a.app_url, app_environment=a.app_environment,
            evidence_ttl_seconds=a.evidence_ttl_seconds, write_wait_max_seconds=a.write_wait_max_seconds,
            rds_reader=lambda: describe_rds(a.rds_identifier, a.region, a.aws_profile),
            app_reader=lambda: app_info(a.app_url))
        write_atomic(Path(a.output), doc)
        print(json.dumps({'job_id': doc['job_id'], 'checks': {k:v['status'] for k,v in doc['checks'].items()}}))
        return 0 if all(v['status'] == 'pass' for v in doc['checks'].values()) else 2
    except Exception:
        # Invalidate an earlier fresh allow snapshot even if setup fails.
        try:
            write_atomic(Path(a.output), {'error': 'EVIDENCE_COLLECTION_FAILED'})
        except OSError:
            pass
        print('EVIDENCE_COLLECTION_FAILED')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
