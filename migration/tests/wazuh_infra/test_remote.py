import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_pki_has_real_loopback_subject_alternative_names(remote_modules):
    from cryptography import x509

    _, _, material, _ = remote_modules
    ca_key, ca, _, _ = material.certificate("test-ca")
    _, leaf, key_bytes, certificate_bytes = material.certificate(
        "indexer", ca_key, ca, ("127.0.0.1", "localhost")
    )
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert certificate_bytes.startswith(b"-----BEGIN CERTIFICATE-----")
    assert key_bytes.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_no_symlink_storage_traversal(remote_modules, tmp_path):
    common, _, _, host = remote_modules
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(common.WazuhError, match="SYMLINK_PATH_REFUSED"):
        host.filesystem(str(alias / "raw"))
    assert host.filesystem(str(real / "not-created"))["observed_path"] == str(real)


def test_atomic_write_preserves_permissions_and_same_contents(remote_modules, tmp_path):
    common, *_ = remote_modules
    path = tmp_path / "secret.json"
    assert common.save(path, {"test": True})
    assert path.stat().st_mode & 0o777 == 0o600
    assert common.save(path, {"test": True}) is False


def test_source_pagination_is_bounded_and_partial(remote_modules, monkeypatch):

    _, helpers, *_ = remote_modules
    calls = []

    class S3:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return {
                "Contents": [],
                "IsTruncated": True,
                "NextContinuationToken": "next",
            }

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(
            Session=lambda **kwargs: SimpleNamespace(client=lambda *args, **kw: S3())
        ),
    )
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kwargs: kwargs)
    )
    c = {
        "sources": [
            {
                "id": "s1",
                "type": "cloudtrail",
                "profile": "test",
                "region": "ap-northeast-2",
                "bucket": "logs-bucket",
                "prefix": "logs/",
                "account_id": "123456789012",
                "max_pages": 2,
            }
        ]
    }
    result = helpers.assess_sources(
        c, {"s1": "[test]\naws_access_key_id=example\naws_secret_access_key=example\n"}
    )[0]
    assert len(calls) == 2
    assert calls[1]["ContinuationToken"] == "next"
    assert result["listing_complete"] is False
    assert result["readable"] is False
    assert "example" not in str(result)


def test_manager_configuration_archives_all_events_and_disables_auto_input(
    remote_modules, monkeypatch, tmp_path
):
    import xml.etree.ElementTree as ET

    _, _, material, _ = remote_modules
    captured = {}
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self: (
            "<ossec_config><global/><remote/><auth><disabled>no</disabled></auth><active-response/></ossec_config><ossec_config><localfile><location>test.log</location></localfile></ossec_config>"
        ),
    )
    monkeypatch.setattr(
        material,
        "owned",
        lambda path, content, *args, **kwargs: captured.update(content=content) or True,
    )
    material.configure_manager({"sources": []})
    root = ET.fromstring("<root>" + captured["content"] + "</root>")
    assert root.find(".//logall_json").text == "yes"
    assert root.find(".//wodle/disabled").text == "yes"
    assert root.find(".//remote") is None
    assert root.find(".//active-response") is None
    assert root.find(".//auth/disabled").text == "yes"
    assert root.find(".//localfile/location").text == "test.log"
