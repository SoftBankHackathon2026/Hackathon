import json
import os
import subprocess
import sys
from pathlib import Path

from migration.wazuh_infra.config import ASSETS


def test_all_packaged_playbooks_pass_actual_ansible_syntax_check(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(
        ANSIBLE_CONFIG=str(ASSETS / "ansible.cfg"),
        ANSIBLE_LIBRARY=str(ASSETS / "library"),
        ANSIBLE_MODULE_UTILS=str(ASSETS / "module_utils"),
        ANSIBLE_HOME=str(tmp_path),
        ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"),
    )
    for playbook in (ASSETS / "playbooks").glob("*.yml"):
        p = subprocess.run(
            [
                str(Path(sys.executable).parent / "ansible-playbook"),
                "-i",
                "localhost,",
                "--syntax-check",
                str(playbook),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert p.returncode == 0, (playbook.name, p.stderr)


def test_missing_module_report_always_stops_actual_ansible(tmp_path):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(mode=0o700)
    playbook = tmp_path / "missing-report.yml"
    playbook.write_text(
        "- hosts: localhost\n  gather_facts: false\n  tasks:\n    - wazuh_report:\n        report: {}\n        enforce: false\n      no_log: true\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(
        ANSIBLE_CONFIG=str(ASSETS / "ansible.cfg"),
        ANSIBLE_HOME=str(tmp_path),
        ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"),
        WI_REPORT_DIR=str(report_dir),
    )
    proc = subprocess.run(
        [
            str(Path(sys.executable).parent / "ansible-playbook"),
            "-i",
            "localhost,",
            str(playbook),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0
    report = json.loads(next(report_dir.glob("*.json")).read_text())
    assert report["reason_codes"] == ["MODULE_REPORT_MISSING"]
