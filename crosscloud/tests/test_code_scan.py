import pathlib

import pytest

from crosscloud import model
from crosscloud.discover import code_scan


def _write(root: pathlib.Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_rds_signer_is_mandatory_rds_iam_coupling(tmp_path):
    _write(
        tmp_path,
        "packages/store/config.js",
        "const x = 1;\n"
        "const {Signer} = require('@aws-sdk/rds-signer');\n"
        "return new Signer(config);\n",
    )

    couplings = code_scan.scan_couplings("request-api", tmp_path)

    assert couplings == (
        model.InfraCoupling(
            workload="request-api",
            location="packages/store/config.js:2",
            rule_id="aws-rds-signer-import",
            category="db_auth_rds_iam",
            certainty="mandatory",
        ),
        model.InfraCoupling(
            workload="request-api",
            location="packages/store/config.js:3",
            rule_id="aws-rds-signer-new",
            category="db_auth_rds_iam",
            certainty="mandatory",
        ),
    )


def test_auth_mode_switch_is_potential_coupling(tmp_path):
    _write(tmp_path, "config.js", "const mode = env.DB_AUTH_MODE;\n")

    (coupling,) = code_scan.scan_couplings("app", tmp_path)

    assert coupling.rule_id == "db-auth-mode-switch"
    assert coupling.certainty == "potential"


@pytest.mark.parametrize(
    ("text", "rule_id", "certainty"),
    [
        (
            "import {Connector} from '@google-cloud/cloud-sql-connector';",
            "gcp-cloudsql-connector",
            "potential",
        ),
        ("authType: AuthTypes.IAM,", "gcp-cloudsql-iam-auth", "mandatory"),
        ("authType: 'IAM',", "gcp-cloudsql-iam-auth", "mandatory"),
        ("enable_iam_auth=True,", "gcp-cloudsql-iam-auth", "mandatory"),
    ],
)
def test_cloudsql_markers(tmp_path, text, rule_id, certainty):
    _write(tmp_path, "src/db.ts", text + "\n")

    (coupling,) = code_scan.scan_couplings("app", tmp_path)

    assert coupling.category == "db_auth_cloudsql_iam"
    assert coupling.rule_id == rule_id
    assert coupling.certainty == certainty


def test_plain_env_reading_is_not_coupling(tmp_path):
    _write(
        tmp_path,
        "index.js",
        "const url = process.env.DATABASE_URL;\n"
        "const pool = new Pool({connectionString: url});\n",
    )

    assert code_scan.scan_couplings("app", tmp_path) == ()


def test_skips_dependency_directories_and_non_source_files(tmp_path):
    marker = "require('@aws-sdk/rds-signer');\n"
    _write(tmp_path, "node_modules/x/index.js", marker)
    _write(tmp_path, ".git/hooks/a.js", marker)
    _write(tmp_path, "README.md", marker)

    assert code_scan.scan_couplings("app", tmp_path) == ()


def test_scans_dependency_manifest(tmp_path):
    _write(
        tmp_path,
        "package.json",
        '{"dependencies": {"@aws-sdk/rds-signer": "^3"}}\n',
    )

    (coupling,) = code_scan.scan_couplings("app", tmp_path)

    assert coupling.location == "package.json:1"


def test_missing_repo_dir_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        code_scan.scan_couplings("app", tmp_path / "missing")
