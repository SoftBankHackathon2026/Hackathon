import pytest

from crosscloud import model
from crosscloud.discover import env


def test_plain_value_is_kept():
    entry = env.classify_env("LOG_LEVEL", "info")

    assert entry == model.EnvEntry(
        key="LOG_LEVEL",
        classification="plain_nonsecret",
        value="info",
        confirmed_by=None,
    )


@pytest.mark.parametrize(
    "value",
    [
        "ap-northeast-2",
        "8080",
        "mydb.abc123xyz.ap-northeast-2.rds.amazonaws.com",
        "postgres://db.internal:5432/shop",
        "production-environment-name",
    ],
)
def test_ordinary_values_are_plain(value):
    assert env.classify_env("SETTING", value).classification == (
        "plain_nonsecret"
    )


def test_empty_value_is_empty_without_value():
    entry = env.classify_env("OPTIONAL_FLAG", "")

    assert entry.classification == "empty"
    assert entry.value is None


def test_secret_reference_is_secret_ref_without_value():
    ref = model.SecretRef(
        cloud="aws",
        native_id="arn:aws:secretsmanager:ap-northeast-2:1:secret:db",
        version="AWSCURRENT",
    )

    entry = env.classify_env("DATABASE_URL", ref)

    assert entry.classification == "secret_ref"
    assert entry.value is None


@pytest.mark.parametrize(
    "key",
    [
        "DB_PASSWORD",
        "PGPASSWORD",
        "API_KEY",
        "STRIPE_SECRET",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "SERVICE_CREDENTIALS",
        "private-key",
    ],
)
def test_secret_like_key_drops_value(key):
    entry = env.classify_env(key, "hunter2")

    assert entry.classification == "suspected_secret"
    assert entry.value is None


@pytest.mark.parametrize(
    "value",
    [
        "postgres://app:s3cr3t@db.internal:5432/shop",
        "host=db user=app password=s3cr3t",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "xoxb-1234-5678-abcdef",
        "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "q8Zr2Lm9Xv4Tn7Wk1Pb6Hs3Jd5Fg0Yc",
    ],
)
def test_secret_like_value_drops_value(value):
    entry = env.classify_env("SETTING", value)

    assert entry.classification == "suspected_secret"
    assert entry.value is None


@pytest.mark.parametrize("key", ["AUTH_HEADER", "SESSION_SALT", "SENTRY_DSN"])
def test_ambiguous_key_is_unknown(key):
    entry = env.classify_env(key, "something")

    assert entry.classification == "unknown"
    assert entry.value is None


def test_long_low_entropy_token_is_unknown():
    entry = env.classify_env("SETTING", "aaaaaaaaaa1111111111bbbb")

    assert entry.classification == "unknown"
    assert entry.value is None


def test_multiline_value_is_unknown():
    entry = env.classify_env("SETTING", "line one\nline two")

    assert entry.classification == "unknown"
    assert entry.value is None


@pytest.mark.parametrize("key", ["DB_PW", "PGPASS", "SSH_PASSPHRASE", "DBPASS"])
def test_short_password_key_names_drop_value(key):
    entry = env.classify_env(key, "hunter2")

    assert entry.classification == "suspected_secret"
    assert entry.value is None


@pytest.mark.parametrize(
    "value",
    [
        "redis://:canary-pw@cache.internal:6379/0",
        "postgres://app:pa/ss@db.internal:5432/shop",
        "Bearer abc.def",
        "basic dXNlcjpwYXNz",
        '{"password":"x"}',
        '{"db": {"secret": "x"}}',
        "{'token': 'x'}",
        "api_key: x",
    ],
)
def test_embedded_credentials_drop_value(value):
    entry = env.classify_env("CONFIG", value)

    assert entry.classification == "suspected_secret"
    assert entry.value is None
