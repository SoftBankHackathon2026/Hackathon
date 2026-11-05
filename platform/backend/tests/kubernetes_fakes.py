from copy import deepcopy

from kubernetes.client import ApiException

from controlplane.providers.onprem import pods


class MemoryApi:
    """Small server fake enforcing object identity and CAS; not a K8s emulator."""

    def __init__(self):
        self.objects = {}
        self.creates = 0
        self.patch_calls = 0
        self.next_error = None

    def __getattr__(self, method):
        verb, kind = method.split("_namespaced_")

        def call(name=None, namespace=None, body=None, **kwargs):
            if verb == "read" and kind == "service_account":
                return {"metadata": {"name": name}}
            if verb == "read" and kind == "secret":
                return {
                    "metadata": {"uid": "db-version-1"},
                    "immutable": True,
                    "data": {"url": "not-logged"},
                }
            key = (kind, name or body["metadata"]["name"])
            if verb == "read":
                if key not in self.objects:
                    raise ApiException(status=404)
                return deepcopy(self.objects[key])
            if self.next_error:
                error, self.next_error = self.next_error, None
                raise error
            if verb == "create":
                self.creates += 1
                if key in self.objects:
                    raise ApiException(status=409)
                obj = deepcopy(body)
                obj["metadata"].update(uid=f"uid-{self.creates}", resourceVersion="1", generation=1)
                self.objects[key] = obj
                return deepcopy(obj)
            if verb == "patch":
                self.patch_calls += 1
                obj = self.objects[key]
                if body["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]:
                    raise ApiException(status=409)
                obj["spec"]["selector"] = deepcopy(body["spec"]["selector"])
                obj["metadata"]["resourceVersion"] = str(
                    int(obj["metadata"]["resourceVersion"]) + 1
                )
                return deepcopy(obj)
            raise AssertionError(method)

        return call


def ready(api, env, release, replicas=1):
    api.objects[("deployment", pods.name(env, release))]["status"] = {
        "observedGeneration": 1,
        "replicas": replicas,
        "updatedReplicas": replicas,
        "readyReplicas": replicas,
        "availableReplicas": replicas,
    }
