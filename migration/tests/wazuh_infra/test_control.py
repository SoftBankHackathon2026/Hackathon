from pathlib import Path

import pytest


@pytest.mark.parametrize("action", ["pause", "status"])
def test_interrupted_pause_is_not_reported_as_success(
    remote_manage, remote_modules, monkeypatch, tmp_path, action
):
    _, _, material, _ = remote_modules
    state = {"installed": True, "enabled": True, "control_intent": "pause"}
    commands = []
    results = {}

    class Finished(BaseException):
        pass

    class Module:
        params = {
            "action": "control",
            "config": {"control": {"action": action}},
            "digest": "test",
            "credentials": {"aws": {}},
        }

        def exit_json(self, **kwargs):
            results.update(kwargs)
            raise Finished

        def fail_json(self, **kwargs):
            results.update(kwargs, failed=True)
            raise Finished

    monkeypatch.setattr(remote_manage, "AnsibleModule", lambda **kwargs: Module())
    monkeypatch.setattr(remote_manage, "STATE", tmp_path)
    monkeypatch.setattr(remote_manage, "marker", lambda *args: state)
    monkeypatch.setattr(remote_manage, "save", lambda *args: None)
    monkeypatch.setattr(
        remote_manage, "command", lambda args, **kwargs: commands.append(args) or ""
    )
    monkeypatch.setattr(material, "configure_manager", lambda *args: None)
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *args, **kwargs: (
            '<ossec_config><wodle name="aws-s3"><disabled>yes</disabled></wodle></ossec_config>'
            if str(p) == "/var/ossec/etc/ossec.conf"
            else original_read(p, *args, **kwargs)
        ),
    )
    with pytest.raises(Finished):
        remote_manage.main()
    assert not results.get("failed"), results
    if action == "status":
        assert results["report"]["ingestion_enabled"] is None
        assert "CONTROL_STATE_UNCONFIRMED" in results["report"]["reason_codes"]
        assert not commands
    else:
        assert ["systemctl", "restart", "wazuh-manager"] in commands
        assert results["changed"] is True
        assert results["report"]["ingestion_enabled"] is False
        assert "control_intent" not in state
