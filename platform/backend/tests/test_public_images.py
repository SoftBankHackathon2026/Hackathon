import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from controlplane.execution.image_preflight import prepare_public_image
from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable
from controlplane.providers.public_images import parse_public_image, verify_public_image


def document(value):
    raw = json.dumps(value).encode()
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def registry(*, architecture="amd64", index=False, redirect=None, status=200, corrupt=False):
    config, config_digest = document({"os": "linux", "architecture": architecture})
    manifest, manifest_digest = document({"config": {"digest": config_digest}})
    if index:
        root, root_digest = document(
            {
                "manifests": [
                    {
                        "digest": manifest_digest,
                        "platform": {"os": "linux", "architecture": architecture},
                    }
                ]
            }
        )
    else:
        root, root_digest = manifest, manifest_digest
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == "auth.docker.io":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"token": "anonymous-pull-token"})
        if request.url.host == "production.cloudflare.docker.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=config)
        assert request.url.host == "registry-1.docker.io"
        if status != 200:
            return httpx.Response(status)
        if "/blobs/" in request.url.path:
            if redirect:
                return httpx.Response(307, headers={"location": redirect})
            return httpx.Response(200, content=config)
        data = root if request.url.path.endswith(root_digest) else manifest
        return httpx.Response(200, content=data + b" " if corrupt else data)

    return SimpleNamespace(
        image="docker.io/team/demo@" + root_digest,
        transport=httpx.MockTransport(respond),
        calls=calls,
    )


@pytest.mark.parametrize("index", [False, True])
def test_anonymous_manifest_and_platform_verification(index):
    r = registry(index=index, redirect="https://production.cloudflare.docker.com/signed/config")
    result = verify_public_image(r.image, transport=r.transport)
    assert result == {"image": r.image, "platform": "linux/amd64", "access": "public"}


def test_arm_image_selects_correct_variant():
    r = registry(architecture="arm64", index=True)
    assert verify_public_image(r.image, "ARM64", transport=r.transport)["platform"] == "linux/arm64"
    with pytest.raises(ProviderRejected, match="architecture"):
        verify_public_image(r.image, transport=r.transport)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_inaccessible_images_fail_before_deployment(status):
    r = registry(status=status)
    with pytest.raises(ProviderRejected, match="not_public_or_missing"):
        verify_public_image(r.image, transport=r.transport)


@pytest.mark.parametrize("status", [429, 503])
def test_rate_limit_and_outage_are_retryable(status):
    r = registry(status=status)
    with pytest.raises(ProviderUnavailable):
        verify_public_image(r.image, transport=r.transport)


def test_corrupt_digest_is_rejected():
    r = registry(corrupt=True)
    with pytest.raises(ProviderRejected, match="digest_mismatch"):
        verify_public_image(r.image, transport=r.transport)


def test_signed_cdn_query_is_not_logged(caplog):
    r = registry(redirect="https://production.cloudflare.docker.com/blob?Signature=secret-proof")
    with caplog.at_level(logging.INFO, logger="httpx"):
        verify_public_image(r.image, transport=r.transport)
    assert "secret-proof" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example/blob",
        "https://user:pass@production.cloudflare.docker.com/blob",
    ],
)
def test_redirects_cannot_reach_arbitrary_hosts(url):
    r = registry(redirect=url)
    with pytest.raises(ProviderRejected, match="redirect_rejected"):
        verify_public_image(r.image, transport=r.transport)
    assert len(r.calls) == 3


@pytest.mark.parametrize(
    "image",
    [
        "docker.io/team/demo:latest",
        "docker.io/team/../demo@sha256:" + "a" * 64,
        "https://docker.io/team/demo@sha256:" + "a" * 64,
        "localhost/team/demo@sha256:" + "a" * 64,
    ],
)
def test_canonical_digest_reference_required(image):
    with pytest.raises(ValueError):
        parse_public_image(image)


def test_failed_preflight_is_terminal_without_provider_writes(monkeypatch):
    store = Mock()
    row = {
        "state": "queued",
        "expired": False,
        "checkpoint": {},
        "request": {"source": {"image": registry().image}},
    }
    monkeypatch.setattr(
        "controlplane.execution.image_preflight.verify_public_image",
        Mock(side_effect=ProviderRejected("image_architecture_mismatch")),
    )
    assert prepare_public_image(store, None, row) is False
    assert store.transition.call_args.args[2] == "failed"
    assert store.transition.call_args.kwargs["error_code"] == "image_architecture_mismatch"


def test_preflight_does_not_block_existing_release_recovery(monkeypatch):
    verify = Mock(side_effect=AssertionError("network must not be used during recovery"))
    monkeypatch.setattr("controlplane.execution.image_preflight.verify_public_image", verify)
    row = {"state": "restoring", "request": {"source": {"image": registry().image}}}
    assert prepare_public_image(Mock(), None, row)


@pytest.mark.parametrize("previous_retries", [0, 5])
def test_registry_outage_retries_then_fails_without_uncertain_writes(monkeypatch, previous_retries):
    store = Mock()
    row = {
        "state": "queued",
        "expired": False,
        "checkpoint": {"image_transport_retries": previous_retries},
        "request": {"source": {"image": registry().image}},
    }
    monkeypatch.setattr(
        "controlplane.execution.image_preflight.verify_public_image",
        Mock(side_effect=ProviderUnavailable("outage")),
    )
    assert prepare_public_image(store, None, row) is False
    if previous_retries == 0:
        store.transition.assert_not_called()
        assert store.defer.call_args.kwargs["checkpoint"]["image_transport_retries"] == 1
    else:
        store.defer.assert_not_called()
        assert store.transition.call_args.args[2] == "failed"
