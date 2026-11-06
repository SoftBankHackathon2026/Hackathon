import json
import unittest

from controlplane.domain.workload_inventory import analyze


def obj(kind, name, spec=None, namespace="demo", **extra):
    return dict(
        kind=kind, metadata={"name": name, "namespace": namespace}, spec=spec or {}, **extra
    )


class InventoryTests(unittest.TestCase):
    def test_secret_values_and_inline_config_are_not_emitted(self):
        pod = {
            "containers": [
                {
                    "image": "harbor/demo/app:v1",
                    "env": [
                        {"name": "PASSWORD", "value": "INLINE_CANARY"},
                        {
                            "name": "DB",
                            "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}},
                        },
                    ],
                }
            ]
        }
        result = analyze(
            {
                "kind": "List",
                "items": [
                    obj("Deployment", "app", {"template": {"spec": pod}}),
                    obj(
                        "Secret",
                        "db",
                        data={"password": "SECRET_CANARY"},
                        stringData={"token": "TOKEN_CANARY"},
                    ),
                    obj("ConfigMap", "settings", data={"url": "CONFIG_CANARY"}),
                ],
            }
        )
        serialized = json.dumps(result)
        for value in ["INLINE_CANARY", "SECRET_CANARY", "TOKEN_CANARY", "CONFIG_CANARY"]:
            self.assertNotIn(value, serialized)
        secret = next(x for x in result["dependencies"] if x["target"]["kind"] == "Secret")
        self.assertTrue(secret["present_in_export"])
        self.assertEqual(result["images"], ["harbor/demo/app:v1"])

    def test_init_cronjob_projected_and_namespaced_dependencies(self):
        pod = {
            "initContainers": [
                {"image": "init:v1", "envFrom": [{"configMapRef": {"name": "config"}}]}
            ],
            "imagePullSecrets": [{"name": "registry"}],
            "volumes": [{"projected": {"sources": [{"secret": {"name": "db"}}]}}],
        }
        result = analyze(
            {
                "kind": "List",
                "items": [
                    obj("CronJob", "job", {"jobTemplate": {"spec": {"template": {"spec": pod}}}}),
                    obj("Secret", "db", namespace="other"),
                ],
            }
        )
        self.assertEqual(result["images"], ["init:v1"])
        self.assertEqual(
            {x["target"]["name"] for x in result["dependencies"]},
            {"config", "registry", "db", "default"},
        )
        self.assertFalse(any(x["present_in_export"] for x in result["dependencies"]))

    def test_ingress_storage_and_unsupported_are_not_ready(self):
        result = analyze(
            {
                "kind": "List",
                "items": [
                    obj(
                        "Ingress",
                        "web",
                        {
                            "tls": [{"secretName": "tls"}],
                            "defaultBackend": {"service": {"name": "app"}},
                        },
                    ),
                    obj(
                        "StatefulSet",
                        "db",
                        {"volumeClaimTemplates": [{"metadata": {"name": "data"}}]},
                    ),
                    obj("Service", "app", {"type": "LoadBalancer"}),
                    obj("CustomThing", "unknown"),
                ],
            }
        )
        self.assertEqual(result["readiness"], "not_evaluated")
        self.assertTrue(any("claim templates" in x for x in result["target_review_required"]))
        self.assertTrue(any("unsupported" in x for x in result["target_review_required"]))
        service = next(x for x in result["dependencies"] if x["target"]["kind"] == "Service")
        self.assertTrue(service["present_in_export"])

    def test_argo_rollout_pod_template_is_analyzed(self):
        result = analyze(
            obj(
                "Rollout",
                "app",
                {
                    "template": {
                        "spec": {
                            "containers": [{"image": "app:v1"}],
                            "volumes": [{"persistentVolumeClaim": {"claimName": "data"}}],
                        }
                    }
                },
            )
        )
        self.assertEqual(result["images"], ["app:v1"])
        self.assertTrue(any(x["target"]["name"] == "data" for x in result["dependencies"]))
        self.assertTrue(any("Argo Rollouts" in x for x in result["target_review_required"]))

    def test_invalid_document_rejected(self):
        for document in [[], {"kind": "List", "items": "not-a-list"}, {"kind": "Pod"}]:
            with self.assertRaises(ValueError):
                analyze(document)

    def test_input_is_not_mutated_and_output_is_deterministic(self):
        items = [obj("Secret", "a"), obj("Service", "b")]
        original = json.dumps(items)
        self.assertEqual(
            analyze({"kind": "List", "items": items}),
            analyze({"kind": "List", "items": list(reversed(items))}),
        )
        self.assertEqual(json.dumps(items), original)
