"""Generate real upstream artifacts from the PR #51 identity code in this tree."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from migration.secrets.plan import build_plan


def test_unmodified_identity_pr51_plan_is_consumed(scenario):
    root = os.environ.get("IDENTITY_REFERENCE_ROOT")
    if not root:
        pytest.skip(
            "IDENTITY_REFERENCE_ROOT is required for upstream PR compatibility test"
        )
    s = scenario
    program = r"""
import copy,json,sys
from pathlib import Path
from migration.identity.common import digest,private_write,validate_config
from migration.identity.plan import create_plan
root,out=Path(sys.argv[1]),Path(sys.argv[2])
c=json.loads((root/'migration/identity-examples/config.example.json').read_text())
c['targets'].pop('kubernetes');c['targets'].pop('applications')
c=validate_config(c)
inv=json.loads((root/'migration/identity-examples/inventory.example.json').read_text())
inv['policies'][0]['documents'][0]['document']['Statement'][0]['Resource']='arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:orders-AbCdEf'
inv['inventory_hash']=digest({k:v for k,v in inv.items() if k!='inventory_hash'})
m=json.loads((root/'migration/identity-examples/mapping.example.json').read_text())
m['inventory_hash']=inv['inventory_hash']
m['bindings'][0]['targets']={'openbao':[{'path':'kv/data/migration/orders/dev/database','capabilities':['read']}]}
p=create_plan(c,inv,m)
assert p['status']=='planned',p['blockers']
for name,value in [('config',c),('inventory',inv),('plan',p)]:
 private_write(out/('identity-'+name+'.json'),value)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, root, str(s.directory)],
        cwd=s.directory,
        env={**os.environ, "PYTHONPATH": str(Path(root) / "migration")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    from migration.secrets.common import load

    upstream = load(s.config["identity"]["plan"])
    s.mapping["bindings"][0]["identity_assignments"] = [
        upstream["mapping_rows"][0]["assignment_id"]
    ]
    plan = build_plan(s.config, s.inventory, s.mapping)
    assert plan["status"] == "planned"
    assert plan["identity_plan_hash"] == upstream["plan_hash"]
    assert plan["rows"][0]["subjects"][0]["principal_id"] == "g-1"
