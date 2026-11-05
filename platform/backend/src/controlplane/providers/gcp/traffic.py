import hashlib
import json

from google.cloud import run_v2

REVISION_TARGET = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
LATEST_TARGET = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
STABLE_TAG = "oneport-stable"
CANDIDATE_TAG = "oneport-candidate"


def digest(template):
    return hashlib.sha256(
        json.dumps(run_v2.RevisionTemplate.to_dict(template), sort_keys=True).encode()
    ).hexdigest()


def traffic(service):
    return [run_v2.TrafficTarget.to_dict(t) for t in service.traffic]


def normalized(targets):
    return sorted(json.dumps(t, sort_keys=True) for t in targets)


def weights(service):
    result = {}
    for t in service.traffic_statuses:
        if t.percent:
            revision = t.revision.rsplit("/", 1)[-1]
            result[revision] = result.get(revision, 0) + t.percent
    return result


def revision_target(revision, percent, tag=""):
    return run_v2.TrafficTarget.to_dict(
        run_v2.TrafficTarget(type_=REVISION_TARGET, revision=revision, percent=percent, tag=tag)
    )


def targets(stable, candidate, percent):
    return [
        revision_target(stable, 100 - percent, STABLE_TAG),
        revision_target(candidate, percent, CANDIDATE_TAG),
    ]


def pinned_rollback_targets(service, stable):
    rollback = []
    for target in service.traffic:
        pinned = run_v2.TrafficTarget(target)
        if pinned.type_ == LATEST_TARGET:
            pinned.type_ = REVISION_TARGET
            pinned.revision = stable
        rollback.append(run_v2.TrafficTarget.to_dict(pinned))
    return rollback or [revision_target(stable, 100)]
