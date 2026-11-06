import os
import subprocess
import sys
from pathlib import Path

from migration.openbao_infra.config import ASSETS


def test_all_playbooks_pass_actual_ansible_syntax_check(tmp_path):
    executable = str(Path(sys.executable).parent / "ansible-playbook")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(
        ANSIBLE_CONFIG=str(ASSETS / "ansible.cfg"),
        ANSIBLE_LIBRARY=str(ASSETS / "library"),
        ANSIBLE_MODULE_UTILS=str(ASSETS / "module_utils"),
        ANSIBLE_HOME=str(tmp_path),
        ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"),
    )
    for playbook in sorted((ASSETS / "playbooks").glob("*.yml")):
        p = subprocess.run(
            [executable, "-i", "localhost,", "--syntax-check", str(playbook)],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert p.returncode == 0, (playbook.name, p.stderr)
