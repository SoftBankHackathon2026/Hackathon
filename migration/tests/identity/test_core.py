from __future__ import annotations
import copy
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
import pytest
from migration.identity.config import load_spec, read, write
from migration.identity.models import (BlockedError, ConfigError, ConflictError, Inventory, Plan, UnknownOutcome, digest, primitive)
from migration.identity.planner import make_plan
from migration.identity.preflight import evidence_digest, preflight, validate_plan
from migration.identity.executor import execute, rollback_plan
from migration.identity.state import State
from migration.identity.verifier import verify, GcpReadProbe

EXAMPLES = Path(__file__).resolve().parents[2]/'identity-examples'


def plan_fixture():
    spec=load_spec(EXAMPLES/'aws-gcp-readonly.yaml')
    inventory=Inventory.from_dict(read(EXAMPLES/'aws-gcp-inventory.json'))
    return make_plan(spec,inventory)


class Source:
    def __init__(self, side): self.side=copy.deepcopy(side)
    def collect(self): return copy.deepcopy(self.side)


class Target(Source):
    def __init__(self, plan):
        super().__init__(plan.inventory.target)
        self.current={op.id:copy.deepcopy(op.expected) for op in plan.operations}
        self.writes=[]; self.lose=None; self.unavailable=False; self.before_write=None
    def observe(self,op):
        if self.unavailable: raise TimeoutError('token=never-store')
        return copy.deepcopy(self.current[op.id])
    def satisfied(self,op,obs): return obs.get('body',{}).get('desired')==op.desired
    def apply(self,op,before):
        if self.before_write: self.before_write(op)
        self.writes.append(op.id)
        self.current[op.id]={'exists':True,'body':{'desired':copy.deepcopy(op.desired),'id':op.id},'version':'v2'}
        if self.lose==op.id: raise TimeoutError('password=never-store')
        return self.current[op.id]


def execution_fixture():
    p=plan_fixture(); source=Source(p.inventory.source); target=Target(p)
    review={'schema_version':1,'plan_digest':p.plan_digest,'reviewer':'reviewer','reference':'review-001'}
    return p,source,target,review


def test_forward_separates_identity_role_binding_and_external_attachment():
    p=plan_fixture(); validate_plan(p)
    assert [o.kind for o in p.operations]==['gcp.service_account','gcp.custom_role','gcp.bucket_binding']
    assert p.operations[1].desired['included_permissions']==['storage.objects.get','storage.objects.list']
    assert p.operations[2].resource==p.spec.target.resource
    assert p.operations[2].depends_on==[o.id for o in p.operations[:2]]
    assert any(e['kind']=='execution_identity_attachment' for e in p.graph['edges'])
    assert preflight(p)['live_checked'] is False


def test_reverse_uses_separate_rule_and_ec2_trust():
    p=make_plan(load_spec(EXAMPLES/'gcp-aws-readonly.yaml'),Inventory.from_dict(read(EXAMPLES/'gcp-aws-inventory.json')))
    validate_plan(p)
    assert [o.kind for o in p.operations]==['aws.role','aws.inline_policy']
    assert p.operations[0].desired['trust_policy']['Statement'][0]['Principal']=={'Service':'ec2.amazonaws.com'}


@pytest.mark.parametrize('replacement',[
 {'Effect':'Deny','Action':'s3:GetObject','Resource':'*'},
 {'Effect':'Allow','NotAction':'s3:PutObject','Resource':'*'},
 {'Effect':'Allow','Action':'s3:GetObject','NotResource':'*'},
 {'Effect':'Allow','Action':'s3:GetObject','Resource':'arn:aws:s3:::source-reports-example/prefix/*'},
 {'Effect':'Allow','Action':'s3:*','Resource':'*'},
 {'Effect':'Allow','Action':'s3:GetObject','Resource':'arn:aws:s3:::source-reports-example/*','Condition':{}},
])
def test_complex_policy_never_generates_applicable_operations(replacement):
    p=plan_fixture(); p.inventory.source['policies'][0]['Statement'].append(replacement)
    blocked=make_plan(p.spec,p.inventory)
    assert blocked.blockers and not blocked.operations
    with pytest.raises(BlockedError):validate_plan(blocked)


def test_unknown_coverage_and_missing_observation_block():
    p=plan_fixture(); p.inventory.source['coverage']['organization']='denied'; p.inventory.target['observations']={}
    p=make_plan(p.spec,p.inventory)
    assert any('denied' in b for b in p.blockers)
    assert any('observation missing' in b for b in p.blockers)


def test_order_independent_evidence_and_plan():
    p=plan_fixture(); raw=primitive(p.inventory)
    statements=raw['source']['policies'][0]['Statement']
    raw['source']['policies']=[{'Version':'2012-10-17','Statement':[s]} for s in statements]
    first=make_plan(p.spec,Inventory.from_dict(raw))
    raw['source']['policies'].reverse()
    second=make_plan(p.spec,Inventory.from_dict(raw))
    assert first.plan_digest==second.plan_digest
    assert evidence_digest(first.inventory.source)==evidence_digest(second.inventory.source)


def test_mutable_internal_plan_cannot_bypass_review():
    p=plan_fixture(); p.operations[1].desired['included_permissions']=['storage.objects.delete']
    with pytest.raises(ConfigError):validate_plan(p)


def test_recomputed_arbitrary_plan_is_not_a_valid_rule_output():
    p=plan_fixture(); p.operations[1].desired['included_permissions']=['storage.objects.delete']; p=p.sealed()
    with pytest.raises(ConfigError):validate_plan(p)


def test_execute_resume_reuses_and_persists(tmp_path):
    p,source,target,review=execution_fixture()
    with State(tmp_path) as state:
        result=execute(p,review,state,source,target)
        assert result['access_verified'] is False and len(target.writes)==3
    with State(tmp_path) as state:
        execute(p,review,state,source,target)
        assert len(target.writes)==3
        recovery=rollback_plan(p,state)
        assert recovery['mode']=='read_only_recovery_proposal'
        assert all(not x['automatic_apply_supported'] for x in recovery['items'])


def test_lost_response_reconciles_without_claiming_ownership(tmp_path):
    p,source,target,review=execution_fixture(); target.lose=p.operations[0].id
    with State(tmp_path) as state:
        execute(p,review,state,source,target)
        row=state.get(p.plan_digest,p.operations[0].id)
        assert row['status']=='reconciled' and row['ownership']=='uncertain' and row['created'] is False
        assert rollback_plan(p,state)['items'][-1]['action']=='manual_reconciliation_required'
    assert 'never-store' not in (tmp_path/'identity.sqlite3').read_bytes().decode('latin1')


def test_crash_after_write_restarts_by_readback(tmp_path):
    p,source,target,review=execution_fixture(); op=p.operations[0]
    with State(tmp_path) as state:
        state.bind(p.plan_digest,review)
        state.record(p.plan_digest,op.id,{'status':'started','before':op.expected,'created':False,'ownership':'pending'})
    target.current[op.id]={'exists':True,'body':{'desired':op.desired},'version':'v2'}
    with State(tmp_path) as state:
        execute(p,review,state,source,target)
        assert state.get(p.plan_digest,op.id)['status']=='reconciled'
    assert op.id not in target.writes


def test_unknown_outcome_durable_and_no_blind_retry(tmp_path):
    p,source,target,review=execution_fixture()
    def ambiguous(op,before):
        target.unavailable=True
        raise TimeoutError('token=never-store')
    target.apply=ambiguous
    with State(tmp_path) as state:
        with pytest.raises(UnknownOutcome):execute(p,review,state,source,target)
        assert state.get(p.plan_digest,p.operations[0].id)['status']=='unknown'


def test_review_binding_target_context_and_source_change_block(tmp_path):
    p,source,target,review=execution_fixture()
    with State(tmp_path) as state:
        bad={**review,'plan_digest':'wrong'}
        with pytest.raises(BlockedError):execute(p,bad,state,source,target)
        target.side['policies'].append({'kind':'unrelated','allow':'admin'})
        with pytest.raises(BlockedError):execute(p,review,state,source,target)
        target.side=copy.deepcopy(p.inventory.target)
        source.side['coverage']['boundary']='unknown'
        with pytest.raises(BlockedError):execute(p,review,state,source,target)
    assert not target.writes


def test_completed_identity_replacement_blocks_resume(tmp_path):
    p,source,target,review=execution_fixture()
    with State(tmp_path) as state:
        execute(p,review,state,source,target)
        target.current[p.operations[0].id]['body']['id']='recreated'
        with pytest.raises(ConflictError):execute(p,review,state,source,target)
    assert len(target.writes)==3


def test_state_lock_prevents_concurrent_execution(tmp_path):
    with State(tmp_path) as first, State(tmp_path) as second:
        with first.lock():
            with pytest.raises(ConflictError):
                with second.lock():pass


@pytest.mark.parametrize('outcome',['network_error','not_found','authentication_error','unknown'])
def test_negative_access_requires_real_denial_and_known_resource(outcome):
    p=plan_fixture()
    class Probe:
        def run(self,case):return {'outcome':outcome,'subject':p.spec.target.identity,'identity_confirmed':True,'resource_exists':True}
    result=verify(p,Probe())
    assert not result['live_access_verified']
    assert all(r['status']=='inconclusive' for r in result['results'])


def test_offline_and_default_verification_never_claim_live():
    p=plan_fixture(); result=verify(p)
    assert result['status']=='incomplete' and not result['live_access_verified']
    assert all(r['status']=='not_run' for r in result['results'])


def test_help_and_plan_do_not_import_cloud_sdks(tmp_path):
    code="import sys; from migration.identity.__main__ import main; main(['plan','--spec',sys.argv[1],'--inventory',sys.argv[2],'--out',sys.argv[3]]); assert not any(x in sys.modules for x in ['boto3','google.auth','kubernetes','openstack','httpx'])"
    result=subprocess.run([sys.executable,'-c',code,str(EXAMPLES/'aws-gcp-readonly.yaml'),str(EXAMPLES/'aws-gcp-inventory.json'),str(tmp_path/'plan.json')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    subprocess.run([sys.executable,'-m','migration.identity','--help'],check=True,capture_output=True)
    assert Plan.from_dict(read(tmp_path/'plan.json')).operations


def test_blocked_cli_writes_explanation_with_nonzero_exit(tmp_path):
    p=plan_fixture(); p.inventory.source['coverage']['organization']='unknown'
    write(tmp_path/'inventory.json',p.inventory)
    result=subprocess.run([sys.executable,'-m','migration.identity','plan','--spec',str(EXAMPLES/'aws-gcp-readonly.yaml'),'--inventory',str(tmp_path/'inventory.json'),'--out',str(tmp_path/'plan.json')],capture_output=True,text=True)
    assert result.returncode==4 and read(tmp_path/'plan.json')['blockers']


@pytest.mark.parametrize('member',['allUsers','allAuthenticatedUsers','group:readers@example.invalid'])
def test_reverse_public_or_indirect_permissions_are_not_silently_dropped(member):
    spec=load_spec(EXAMPLES/'gcp-aws-readonly.yaml')
    inv=Inventory.from_dict(read(EXAMPLES/'gcp-aws-inventory.json'))
    inv.source['policies'].extend([
      {'kind':'gcp.role','name':'roles/storage.objectAdmin','included_permissions':['storage.objects.get','storage.objects.list','storage.objects.delete']},
      {'kind':'gcp.allow_policy','resource':spec.source.resource,'policy':{'bindings':[{'role':'roles/storage.objectAdmin','members':[member]}]}}])
    p=make_plan(spec,inv)
    assert p.blockers and not p.operations


def test_source_evidence_of_another_identity_blocks_plan():
    p=plan_fixture();p.inventory.source['identities'][0]['name']='arn:aws:iam::111111111111:role/Other'
    result=make_plan(p.spec,p.inventory)
    assert 'source identity evidence does not identify the declared principal' in result.blockers


def test_cli_bad_review_is_rejected_before_live_factory(tmp_path,monkeypatch,capsys):
    from migration.identity.__main__ import main
    import migration.identity.registry
    p=plan_fixture();write(tmp_path/'plan.json',p)
    write(tmp_path/'review.json',{'schema_version':1,'plan_digest':'wrong','reviewer':'test','reference':'record'})
    def unexpected(endpoint):raise AssertionError('credential discovery must not run')
    monkeypatch.setattr(migration.identity.registry,'from_environment',unexpected)
    code=main(['apply','--live','--plan',str(tmp_path/'plan.json'),'--review-ref',str(tmp_path/'review.json'),'--state-dir',str(tmp_path/'state')])
    assert code==4 and not (tmp_path/'state').exists()
