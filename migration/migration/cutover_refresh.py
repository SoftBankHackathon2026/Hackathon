"""Refresh RDS evidence synchronously at a cutover policy boundary."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from .config import Config
from .cutover_evidence import EvidenceError, app_info, collect, write_atomic

REQUIRED = {'identifier', 'region', 'approved_locations', 'app_url', 'app_environment',
            'evidence_ttl_seconds', 'write_wait_max_seconds'}


def read_settings(path: Path) -> dict:
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise EvidenceError('COLLECTOR_SETTINGS_INVALID')
    value = json.loads(raw)
    if not isinstance(value, dict) or not REQUIRED <= value.keys() or value.keys() - REQUIRED - {'aws_profile'}:
        raise EvidenceError('COLLECTOR_SETTINGS_INVALID')
    for name in ('identifier', 'region', 'app_url', 'app_environment'):
        if not isinstance(value[name], str) or not value[name].strip():
            raise EvidenceError('COLLECTOR_SETTINGS_INVALID')
    if 'aws_profile' in value and (not isinstance(value['aws_profile'], str) or not value['aws_profile'].strip()):
        raise EvidenceError('COLLECTOR_SETTINGS_INVALID')
    if not isinstance(value['approved_locations'], list):
        raise EvidenceError('COLLECTOR_SETTINGS_INVALID')
    return value


def describe_rds_sdk(identifier: str, region: str, profile: str | None) -> dict:
    import boto3
    from botocore.config import Config as AwsConfig

    # Default provider chain supports workload identity; no credentials in settings.
    session = boto3.Session(profile_name=profile)
    with closing(session.client('rds', region_name=region, config=AwsConfig(
            connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 2, 'mode': 'standard'},
            ignore_configured_endpoint_urls=True))) as client:
        return client.describe_db_instances(DBInstanceIdentifier=identifier)


def refresh(config: Config, settings_path: Path, output: Path) -> None:
    try:
        settings = read_settings(settings_path)
        profile = settings.pop('aws_profile', None)
        document = collect(config, **settings,
            rds_reader=lambda: describe_rds_sdk(settings['identifier'], settings['region'], profile),
            app_reader=lambda: app_info(settings['app_url']))
        write_atomic(output, document)
    except Exception:
        try:
            write_atomic(output, {'error': 'EVIDENCE_REFRESH_FAILED'})
        except OSError:
            pass
        # Even if invalidation cannot be written, the current caller must stop.
        raise EvidenceError('EVIDENCE_REFRESH_FAILED') from None
