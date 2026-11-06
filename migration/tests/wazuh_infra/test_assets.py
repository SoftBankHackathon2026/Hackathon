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
