"""Actual-subject access checks, separate from administrative configuration checks.

Default live probes are read-only. Destructive denial probes are deliberately
unsupported until an isolated fixture lifecycle is implemented.
"""
from __future__ import annotations
import os
import json
from .models import BlockedError, ConfigError, Plan, fields, string


def cases(plan: Plan) -> list[dict]:
    result, ids = [], set()
    for case in plan.spec.verification:
        fields(case, {'id', 'action', 'resource', 'expect'}, {'object', 'isolated'})
        for key in ('id', 'action', 'resource'):
            string(case[key], key)
        if case['id'] in ids or case['expect'] not in {'allowed', 'denied'}:
            raise ConfigError('duplicate verification ID or invalid expected result')
        if case['action'] not in {'list', 'get', 'put', 'delete', 'delegate', 'login', 'group-removal'}:
            raise ConfigError('unsupported verification action')
        if 'isolated' in case and type(case['isolated']) is not bool:
            raise ConfigError('isolated requires a boolean')
        if 'object' in case:
            string(case['object'], 'test object')
        if plan.spec.workload == 'object-read' and case['action'] in {'get', 'list'} and case['expect'] == 'allowed' and case['resource'] != plan.spec.target.resource:
            raise ConfigError('positive read probes must use the planned bucket')
        if plan.spec.workload == 'object-read' and case['action'] == 'get' and case['expect'] == 'denied' and case['resource'] == plan.spec.target.resource:
            raise ConfigError('outside-bucket denial requires a different explicit bucket')
        ids.add(case['id'])
        result.append(case)
    return result


def verify(plan: Plan, probe=None, *, live: bool = False) -> dict:
    results = []
    for case in cases(plan):
        if probe is None:
            observation = {'outcome': 'not_run', 'reason': 'actual_subject_probe_not_configured'}
        elif case['action'] in {'put', 'delete'} and case.get('isolated') is not True:
            observation = {'outcome': 'not_run', 'reason': 'isolated_fixture_required'}
        else:
            try:
                observation = probe.run(case)
            except Exception:
                observation = {'outcome': 'unknown', 'reason': 'probe_error_details_withheld'}
        outcome = observation.get('outcome', 'unknown')
        authenticated = observation.get('identity_confirmed') is True and observation.get('subject') == plan.spec.target.identity
        exists = observation.get('resource_exists') is True
        passed = authenticated and exists and outcome == case['expect'] and outcome in {'allowed', 'denied'}
        results.append({'id': case['id'], 'action': case['action'], 'resource': case['resource'], 'expected': case['expect'],
                        'actual': outcome if outcome in {'allowed', 'denied', 'not_run', 'unknown', 'not_found', 'authentication_error', 'network_error', 'unsupported'} else 'unknown',
                        'status': 'passed' if passed else 'not_run' if outcome in {'not_run', 'unsupported'} else 'inconclusive' if not authenticated or not exists or outcome not in {'allowed','denied'} else 'failed',
                        'identity_confirmed': authenticated, 'subject':plan.spec.target.identity if authenticated else None,
                        'resource_exists': exists, 'reason':'confirmed_access_outcome' if passed else 'probe_not_run_or_unsupported' if outcome in {'not_run','unsupported'} else 'subject_or_resource_unconfirmed' if not authenticated or not exists else 'expected_outcome_not_confirmed'})
    required = {('list', 'allowed'), ('get', 'allowed'), ('get', 'denied'), ('put', 'denied'), ('delete', 'denied')} if plan.spec.workload == 'object-read' else {('list','allowed'),('list','denied'),('group-removal','denied')}
    exercised = {(r['action'], r['expected']) for r in results if r['status'] == 'passed'}
    complete = bool(results) and all(r['status'] == 'passed' for r in results) and required <= exercised
    return {'plan_digest': plan.plan_digest, 'mode': 'live_subject' if live else 'mock_or_offline',
            'status': 'passed' if complete else 'failed' if any(r['status']=='failed' for r in results) else 'incomplete',
            'live_access_verified': live and complete, 'results': results,
            'remaining': sorted(f'{a}:{e}' for a,e in required-exercised),
            'limitations': ['write/delete/delegation/login/removal live probes are not implemented; never infer denial from missing resources or network errors']}


class AwsReadProbe:
    def __init__(self, endpoint, operator, session, stable_id):
        self.endpoint, self.operator = endpoint, operator
        self.s3 = session.client('s3', config=self._config())
        caller = session.client('sts', config=self._config()).get_caller_identity()
        role = operator.iam.get_role(RoleName=endpoint.identity.rsplit('/', 1)[1])['Role']
        if role.get('RoleId') != stable_id or caller.get('Account') != endpoint.scope or caller.get('UserId', '').split(':')[0] != stable_id or role.get('Arn') != endpoint.identity:
            raise BlockedError('verification credentials are not the planned AWS role')
        self.subject = endpoint.identity

    @staticmethod
    def _config():
        from botocore.config import Config
        return Config(retries={'total_max_attempts': 1, 'mode': 'standard'})

    def run(self, case):
        base = {'subject': self.subject, 'identity_confirmed': True, 'resource_exists': False}
        if case['action'] not in {'list', 'get'}:
            return {**base, 'outcome': 'unsupported'}
        try:
            self.operator.s3.head_bucket(Bucket=case['resource'], ExpectedBucketOwner=self.endpoint.scope)
            if case['action'] == 'get':
                if not case.get('object'):
                    return {**base, 'outcome': 'not_run'}
                self.operator.s3.head_object(Bucket=case['resource'], Key=case['object'], ExpectedBucketOwner=self.endpoint.scope)
        except Exception:
            return {**base, 'outcome': 'unknown'}
        base['resource_exists'] = True
        try:
            if case['action'] == 'list':
                self.s3.list_objects_v2(Bucket=case['resource'], MaxKeys=1, ExpectedBucketOwner=self.endpoint.scope)
            else:
                response = self.s3.get_object(Bucket=case['resource'], Key=case['object'], Range='bytes=0-0', ExpectedBucketOwner=self.endpoint.scope)
                response['Body'].close()
            return {**base, 'outcome': 'allowed'}
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
            outcome = 'denied' if code in {'AccessDenied', 'AccessDeniedException'} else 'not_found' if code in {'NoSuchKey', 'NoSuchBucket'} else 'authentication_error' if code in {'ExpiredToken','InvalidToken','SignatureDoesNotMatch'} else 'unknown'
            return {**base, 'outcome': outcome}


class GcpReadProbe:
    def __init__(self, endpoint, operator, credentials, stable_id):
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        credentials.refresh(Request())
        account = operator._account()
        if not account or account.get('uniqueId') != stable_id or getattr(credentials, 'service_account_email', None) != endpoint.identity:
            raise BlockedError('verification credentials must identify the planned GCP service account')
        self.endpoint, self.operator = endpoint, operator
        self.storage = build('storage', 'v1', credentials=credentials, cache_discovery=False)
        self.project_number = str(operator._project()['projectNumber'])

    def run(self, case):
        base = {'subject': self.endpoint.identity, 'identity_confirmed': True, 'resource_exists': False}
        if case['action'] not in {'list', 'get'}:
            return {**base, 'outcome': 'unsupported'}
        try:
            bucket = self.operator.storage.buckets().get(bucket=case['resource']).execute(num_retries=0)
            if str(bucket.get('projectNumber')) != self.project_number:
                return {**base, 'outcome': 'not_run'}
            if case['action'] == 'get':
                if not case.get('object'):
                    return {**base, 'outcome': 'not_run'}
                self.operator.storage.objects().get(bucket=case['resource'], object=case['object']).execute(num_retries=0)
        except Exception:
            return {**base, 'outcome': 'unknown'}
        base['resource_exists'] = True
        try:
            if case['action'] == 'list':
                request = self.storage.objects().list(bucket=case['resource'], maxResults=1)
            else:
                request = self.storage.objects().get(bucket=case['resource'], object=case['object'], alt='media')
                request.headers['Range'] = 'bytes=0-0'
            request.execute(num_retries=0)
            return {**base, 'outcome': 'allowed'}
        except Exception as exc:
            status = getattr(getattr(exc, 'resp', None), 'status', None)
            denied = False
            if status == 403:
                try:
                    # HTTP 403 also denotes quota, billing and API-disabled errors.
                    # Accept only a structured, action-specific IAM denial.
                    error = json.loads(exc.content).get('error', {})
                    permission = 'storage.objects.list' if case['action'] == 'list' else 'storage.objects.get'
                    denied = any(detail.get('reason') == 'IAM_PERMISSION_DENIED' and
                                 detail.get('domain') in {'iam.googleapis.com', 'storage.googleapis.com'} and
                                 detail.get('metadata', {}).get('permission') == permission
                                 for detail in error.get('details', []) if isinstance(detail, dict))
                except (AttributeError, ValueError, TypeError):
                    pass
            return {**base, 'outcome': 'denied' if denied else 'not_found' if status == 404 else 'authentication_error' if status == 401 else 'unknown'}


def pinned_identity(plan: Plan, state, operator) -> str:
    kind = {'aws': 'aws.role', 'gcp': 'gcp.service_account'}.get(plan.spec.target.provider)
    operation = next((op for op in plan.operations if op.kind == kind), None)
    if operation is None:
        raise BlockedError('this product has no supported actual-subject identity pin')
    record = state.get(plan.plan_digest, operation.id)
    expected = record.get('after') if record and record.get('status') in {'applied','reused','reconciled'} else operation.expected
    if not expected or not expected.get('exists'):
        raise BlockedError('no reviewed or applied immutable target identity is recorded')
    current = operator.observe(operation)
    key = 'stable_id' if kind == 'aws.role' else 'uniqueId'
    stable_id = expected['body'].get(key)
    if not stable_id or current.get('body', {}).get(key) != stable_id:
        raise BlockedError('verification identity changed since review or application')
    return stable_id


def from_environment(plan: Plan, operator, stable_id):
    if plan.spec.target.provider == 'aws':
        profile = os.environ.get('IDENTITY_VERIFY_AWS_PROFILE')
        if not profile:
            raise ConfigError('set IDENTITY_VERIFY_AWS_PROFILE to a separate workload profile')
        import boto3
        return AwsReadProbe(plan.spec.target, operator, boto3.Session(profile_name=profile), stable_id)
    if plan.spec.target.provider == 'gcp':
        path = os.environ.get('IDENTITY_VERIFY_GCP_CREDENTIALS')
        if not path:
            raise ConfigError('set IDENTITY_VERIFY_GCP_CREDENTIALS to external workload credential configuration')
        import google.auth
        credentials, _ = google.auth.load_credentials_from_file(path, scopes=['https://www.googleapis.com/auth/devstorage.read_only'])
        return GcpReadProbe(plan.spec.target, operator, credentials, stable_id)
    raise BlockedError('actual-subject live probes for this product are unsupported')
