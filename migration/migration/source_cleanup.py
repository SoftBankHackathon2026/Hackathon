"""Idempotent job-scoped source cleanup, using the publication owner's role."""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from psycopg import sql

from . import db
from .cdc import ConnectClient, drop_slot
from .config import Config, load_config


def drop_owned_publication(conn, publication: str) -> bool:
    with conn.cursor() as cur:
        cur.execute('SELECT pg_get_userbyid(pubowner), current_user FROM pg_publication WHERE pubname=%s',
                    (publication,))
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return False
        if row[0] != row[1]:
            conn.rollback()
            raise ValueError('PUBLICATION_OWNER_MISMATCH')
        cur.execute(sql.SQL('DROP PUBLICATION {}').format(sql.Identifier(publication)))
    conn.commit()
    return True


def cleanup_source(config: Config, target: str | None = None) -> dict[str, Any]:
    target = target or config.job.source
    if target != config.job.source:
        raise ValueError('SOURCE_TARGET_MISMATCH')
    job = config.job
    result: dict[str, Any] = {'slot': job.slot_name, 'publication': job.publication_name,
                              'slot_dropped': False, 'publication_dropped': False}
    try:
        client = ConnectClient(config.kafka.connect_url)
        if client.exists(job.source_connector) or client.exists(job.sink_connector):
            result.update(error='CONNECTORS_STILL_PRESENT', slot_status='blocked', publication_status='blocked')
            return result
    except Exception:
        result.update(error='CONNECTORS_CHECK_FAILED', slot_status='blocked', publication_status='blocked')
        return result
    try:
        with db.connect(config.connection(target, 'admin', application_name='migration-cleanup-slot')) as conn:
            result['slot_dropped'] = drop_slot(conn, job.slot_name)
        result['slot_status'] = 'dropped' if result['slot_dropped'] else 'absent'
    except Exception:
        result.update(error='SLOT_CLEANUP_FAILED', slot_status='error', publication_status='blocked')
        return result
    try:
        with db.connect(config.connection(target, 'cdc', application_name='migration-cleanup-publication')) as conn:
            result['publication_dropped'] = drop_owned_publication(conn, job.publication_name)
        result['publication_status'] = 'dropped' if result['publication_dropped'] else 'absent'
    except Exception:
        result.update(error='PUBLICATION_CLEANUP_FAILED', publication_status='error')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('MIGRATION_CONFIG', 'config.yaml'))
    args = parser.parse_args()
    try:
        result = cleanup_source(load_config(args.config))
    except Exception:
        print(json.dumps({'error': 'SOURCE_CLEANUP_FAILED'}))
        return 2
    print(json.dumps(result))
    return 2 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
