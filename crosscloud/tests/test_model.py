import pytest

from crosscloud import model

_CANARY = "canary-5d1e8b-should-not-leak"


@pytest.mark.parametrize(
    "classification", ["secret_ref", "suspected_secret", "unknown", "empty"]
)
def test_env_entry_rejects_value_outside_plain_nonsecret(classification):
    with pytest.raises(ValueError, match=classification) as excinfo:
        model.EnvEntry(
            key="LEGACY_TOKEN",
            classification=classification,
            value=_CANARY,
            confirmed_by=None,
        )

    assert _CANARY not in str(excinfo.value)
    assert _CANARY not in repr(excinfo.value)


@pytest.mark.parametrize(
    "classification", ["secret_ref", "suspected_secret", "unknown", "empty"]
)
def test_env_entry_accepts_missing_value_outside_plain_nonsecret(
    classification,
):
    entry = model.EnvEntry(
        key="LEGACY_TOKEN",
        classification=classification,
        value=None,
        confirmed_by=None,
    )

    assert entry.value is None


def test_env_entry_keeps_plain_nonsecret_value():
    entry = model.EnvEntry(
        key="LOG_LEVEL",
        classification="plain_nonsecret",
        value="info",
        confirmed_by=None,
    )

    assert entry.value == "info"
