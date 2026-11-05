"""Deliberately small transport/controller double; not evidence of real Argo reconciliation."""

from copy import deepcopy

from kubernetes.client import ApiException
from kubernetes_fakes import MemoryApi

from controlplane.providers.onprem import manifests, pods


class ArgoApi(MemoryApi):
    def __init__(self):
        super().__init__()
        self.rollout = None
        self.replica_sets = []
        self.promotions = 0
        self.replacements = 0
        self.promoting = False

    def get_namespaced_custom_object(self, **kwargs):
        if self.rollout is None:
            raise ApiException(status=404)
        return deepcopy(self.rollout)

    def create_namespaced_custom_object(self, body, **kwargs):
        if self.rollout is not None:
            raise ApiException(status=409)
        self.creates += 1
        self.rollout = deepcopy(body)
        self.rollout["metadata"].update(uid="argo-uid", resourceVersion="1", generation=1)
        return deepcopy(self.rollout)

    def _cas(self, body):
        if any(
            body["metadata"][k] != self.rollout["metadata"][k] for k in ("uid", "resourceVersion")
        ):
            raise ApiException(status=409)

    def replace_namespaced_custom_object(self, body, **kwargs):
        self._cas(body)
        self.replacements += 1
        old = self.rollout
        self.rollout = deepcopy(body)
        self.rollout["metadata"]["generation"] = old["metadata"]["generation"] + 1
        self.rollout["metadata"]["resourceVersion"] = str(
            int(old["metadata"]["resourceVersion"]) + 1
        )
        return deepcopy(self.rollout)

    def patch_namespaced_custom_object_status(self, body, **kwargs):
        self._cas(body)
        assert body["status"] == {"pauseConditions": None}
        self.promotions += 1
        self.promoting = True
        self.rollout["status"]["pauseConditions"] = []
        self.rollout["metadata"]["resourceVersion"] = str(
            int(self.rollout["metadata"]["resourceVersion"]) + 1
        )
        return deepcopy(self.rollout)

    def list_namespaced_replica_set(self, **kwargs):
        return {"items": deepcopy(self.replica_sets)}

    def delete_namespaced_ingress(self, name, body, **kwargs):
        obj = self.objects.get(("ingress", name))
        if obj is None:
            raise ApiException(status=404)
        if any(obj["metadata"][k] != v for k, v in body["preconditions"].items()):
            raise ApiException(status=409)
        del self.objects[("ingress", name)]
        return {}

    def delete_namespaced_service(self, name, body, **kwargs):
        obj = self.objects.get(("service", name))
        if obj is None:
            raise ApiException(status=404)
        if any(obj["metadata"][k] != v for k, v in body["preconditions"].items()):
            raise ApiException(status=409)
        del self.objects[("service", name)]
        return {}

    def delete_namespaced_custom_object(self, name, body, **kwargs):
        if self.rollout is None:
            raise ApiException(status=404)
        if any(self.rollout["metadata"][k] != v for k, v in body["preconditions"].items()):
            raise ApiException(status=409)
        self.rollout = None
        return {}

    def settle(self, env):
        """Model initial auto-activation and the subsequent BlueGreen pause."""
        if self.rollout is None:
            return
        obj = self.rollout
        release = obj["spec"]["template"]["metadata"]["labels"][pods.RELEASE]
        hash_value = release[:10]
        active = self.objects[("service", manifests.name(env) + "-active")]
        preview = self.objects[("service", manifests.name(env) + "-preview")]
        preview["spec"]["selector"][manifests.HASH] = hash_value
        if active["spec"]["selector"][manifests.HASH] == "unassigned" or self.promoting:
            active["spec"]["selector"][manifests.HASH] = hash_value
            self.promoting = False
        is_active = active["spec"]["selector"][manifests.HASH] == hash_value
        obj["status"] = {
            "observedGeneration": str(obj["metadata"]["generation"]),
            "currentPodHash": hash_value,
            "phase": "Healthy" if is_active else "Paused",
            "blueGreen": {"activeSelector": active["spec"]["selector"][manifests.HASH]},
            "pauseConditions": [] if is_active else [{"reason": "BlueGreenPause"}],
        }
        template = deepcopy(obj["spec"]["template"])
        template["metadata"]["labels"][manifests.HASH] = hash_value
        self.replica_sets = [
            {
                "metadata": {
                    "uid": "rs-" + hash_value,
                    "generation": 1,
                    "labels": {manifests.HASH: hash_value},
                    "ownerReferences": [{"uid": obj["metadata"]["uid"], "controller": True}],
                },
                "spec": {"template": template},
                "status": {
                    "observedGeneration": 1,
                    **{
                        k: obj["spec"]["replicas"]
                        for k in ("replicas", "readyReplicas", "availableReplicas")
                    },
                },
            }
        ]
