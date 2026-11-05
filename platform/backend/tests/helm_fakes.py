from controlplane.providers.onprem.helm.observation import RolloutState


def rollout_state(**fields) -> RolloutState:
    defaults = {
        "uid": "rollout-uid",
        "resource_version": "1",
        "generation": 1,
        "release": "release",
        "ready": False,
        "failed": False,
    }
    return RolloutState(**{**defaults, **fields})
