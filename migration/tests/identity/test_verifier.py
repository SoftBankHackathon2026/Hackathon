import copy
import json
from types import SimpleNamespace
import pytest
from migration.identity.models import BlockedError, ConfigError
from migration.identity.verifier import GcpReadProbe, pinned_identity, verify
from migration.identity.state import State
from test_core import plan_fixture


class Request:
    def __init__(self,value=None,error=None):self.value,self.error=value,error
    def execute(self,**kwargs):
        if self.error:raise self.error
        return self.value


@pytest.mark.parametrize('reason',['accessNotConfigured','accountDisabled','rateLimitExceeded','quotaExceeded','billingNotEnabled','forbidden','insufficientPermissions'])
def test_gcp_http403_without_specific_iam_denial_is_inconclusive(reason):
    p=plan_fixture()
    error=Exception('token-must-not-appear')
    error.resp=SimpleNamespace(status=403)
    error.content=json.dumps({'error':{'errors':[{'reason':reason}]}}).encode()
    probe=GcpReadProbe.__new__(GcpReadProbe)
    probe.endpoint=p.spec.target; probe.project_number='123'
    bucket=SimpleNamespace(get=lambda **kw:Request({'projectNumber':'123'}))
    probe.operator=SimpleNamespace(storage=SimpleNamespace(buckets=lambda:bucket))
    probe.storage=SimpleNamespace(objects=lambda:SimpleNamespace(list=lambda **kw:Request(error=error)))
    result=probe.run({'action':'list','resource':p.spec.target.resource})
    assert result['outcome']=='unknown' and result['resource_exists'] is True
    assert 'token-must-not-appear' not in json.dumps(result)


def test_gcp_confirmed_permission_specific_denial():
    p=plan_fixture(); error=Exception('sensitive')
    error.resp=SimpleNamespace(status=403)
    error.content=json.dumps({'error':{'details':[{'reason':'IAM_PERMISSION_DENIED','domain':'iam.googleapis.com','metadata':{'permission':'storage.objects.list'}}]}}).encode()
    probe=GcpReadProbe.__new__(GcpReadProbe); probe.endpoint=p.spec.target;probe.project_number='123'
    probe.operator=SimpleNamespace(storage=SimpleNamespace(buckets=lambda:SimpleNamespace(get=lambda **kw:Request({'projectNumber':'123'}))))
    probe.storage=SimpleNamespace(objects=lambda:SimpleNamespace(list=lambda **kw:Request(error=error)))
    assert probe.run({'action':'list','resource':p.spec.target.resource})['outcome']=='denied'


def test_immutable_identity_pinned_to_journal(tmp_path):
    p=plan_fixture();op=p.operations[0]
    after={'exists':True,'body':{'uniqueId':'123','email':p.spec.target.identity},'version':None}
    with State(tmp_path) as state:
        state.record(p.plan_digest,op.id,{'status':'applied','after':after,'before':op.expected})
        operator=SimpleNamespace(observe=lambda op:copy.deepcopy(after))
        assert pinned_identity(p,state,operator)=='123'
        operator.observe=lambda op:{'exists':True,'body':{'uniqueId':'456'},'version':None}
        with pytest.raises(BlockedError):pinned_identity(p,state,operator)


def test_verification_rejects_same_bucket_as_outside_boundary():
    p=plan_fixture();p.spec.verification[2]['resource']=p.spec.target.resource
    with pytest.raises(ConfigError):verify(p)


def test_report_includes_execution_and_verification(tmp_path):
    from migration.identity.report import report,markdown
    p=plan_fixture()
    with State(tmp_path) as state:
        result=verify(p);state.record_verification(p.plan_digest,result)
        value=report(p,state)
        assert value['verification']==result
        assert not value['live_access_verified']
        assert 'not_run' in markdown(value)
