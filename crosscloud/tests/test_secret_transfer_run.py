import logging
import traceback

import pytest
from memory_secret_store import MemorySecretStore

from crosscloud.secret_transfer import run, store

_CANARY = b"canary-7f3a91-should-not-leak"
_RUN_ID = "ccx-s1-forward"


def _item(source="src/db-url", target="tgt-db-url"):
    return run.SecretTransferItem(
        source_secret_id=source, target_secret_id=target
    )


def _all_text(*objects) -> str:
    return "\n".join(repr(o) + str(o) for o in objects)


def test_transfer_copies_value_and_reports_verified():
    source = MemorySecretStore({"src/db-url": _CANARY})
    target = MemorySecretStore()

    results = run.transfer_secrets(
        (_item(),), frozenset({"src/db-url"}), source, target, _RUN_ID
    )

    assert target.read_current("tgt-db-url") == _CANARY
    assert [r.verified for r in results] == [True]
    assert results[0].written.created is True


def test_transfer_refuses_unlisted_secret_before_reading_any_value():
    source = MemorySecretStore({"src/db-url": _CANARY, "src/other": b"x"})
    target = MemorySecretStore()

    with pytest.raises(run.SecretTransferError, match="src/other"):
        run.transfer_secrets(
            (_item(), _item("src/other", "tgt-other")),
            frozenset({"src/db-url"}),
            source,
            target,
            _RUN_ID,
        )

    assert source.reads == []
    assert target.versions == {}


def test_transfer_refuses_missing_source_value():
    source = MemorySecretStore()
    target = MemorySecretStore()

    with pytest.raises(run.SecretTransferError) as excinfo:
        run.transfer_secrets(
            (_item(),), frozenset({"src/db-url"}), source, target, _RUN_ID
        )

    assert excinfo.value.error_class == "source_missing"


def test_transfer_mismatch_rolls_back_this_run_and_hides_value(caplog):
    caplog.set_level(logging.DEBUG, logger="crosscloud")
    source = MemorySecretStore({"src/a": b"first", "src/db-url": _CANARY})
    target = MemorySecretStore()
    target.corrupt_read_back_for.add("tgt-db-url")

    with pytest.raises(run.SecretTransferError) as excinfo:
        run.transfer_secrets(
            (_item("src/a", "tgt-a"), _item()),
            frozenset({"src/a", "src/db-url"}),
            source,
            target,
            _RUN_ID,
        )

    assert excinfo.value.error_class == "mismatch"
    assert target.read_current("tgt-a") is None
    assert target.read_current("tgt-db-url") is None
    text = _all_text(excinfo.value) + caplog.text
    text += "".join(traceback.format_exception(excinfo.value))
    assert _CANARY.decode() not in text


def test_transfer_write_failure_rolls_back_earlier_items():
    source = MemorySecretStore({"src/a": b"first", "src/db-url": _CANARY})
    target = MemorySecretStore()
    target.fail_add_for.add("tgt-db-url")

    with pytest.raises(run.SecretTransferError) as excinfo:
        run.transfer_secrets(
            (_item("src/a", "tgt-a"), _item()),
            frozenset({"src/a", "src/db-url"}),
            source,
            target,
            _RUN_ID,
        )

    assert excinfo.value.error_class == "target_write_failed"
    assert target.read_current("tgt-a") is None
    assert _CANARY.decode() not in "".join(
        traceback.format_exception(excinfo.value)
    )


def test_results_and_logs_never_contain_value(caplog):
    caplog.set_level(logging.DEBUG, logger="crosscloud")
    source = MemorySecretStore({"src/db-url": _CANARY})
    target = MemorySecretStore()

    results = run.transfer_secrets(
        (_item(),), frozenset({"src/db-url"}), source, target, _RUN_ID
    )

    assert _CANARY.decode() not in _all_text(*results) + caplog.text


def test_roll_back_retires_only_versions_this_run_created():
    target = MemorySecretStore({"tgt-db-url": b"older"})
    source = MemorySecretStore({"src/db-url": _CANARY})
    results = run.transfer_secrets(
        (_item(),), frozenset({"src/db-url"}), source, target, _RUN_ID
    )

    outcomes = run.roll_back_transfer(results, target)

    assert outcomes == ("retired",)
    assert target.read_current("tgt-db-url") == b"older"


@pytest.mark.parametrize(
    "logger_name",
    [
        "botocore",
        "botocore.parsers",
        "botocore.endpoint",
        "google",
        "google.api_core.grpc_helpers",
        "urllib3",
    ],
)
def test_transfer_refuses_when_client_debug_logging_is_on(logger_name):
    logger = logging.getLogger(logger_name)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    source = MemorySecretStore({"src/db-url": _CANARY})
    try:
        with pytest.raises(run.SecretTransferError) as excinfo:
            run.transfer_secrets(
                (_item(),),
                frozenset({"src/db-url"}),
                source,
                MemorySecretStore(),
                _RUN_ID,
            )
    finally:
        logger.setLevel(previous)

    assert excinfo.value.error_class == "unsafe_logging"
    assert source.reads == []


def test_transfer_reports_source_read_failure():
    source = MemorySecretStore({"src/db-url": _CANARY})
    source.fail_read_for.add("src/db-url")

    with pytest.raises(run.SecretTransferError) as excinfo:
        run.transfer_secrets(
            (_item(),),
            frozenset({"src/db-url"}),
            source,
            MemorySecretStore(),
            _RUN_ID,
        )

    assert excinfo.value.error_class == "source_read_failed"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            store.SecretStoreError("read_version", "tgt-db-url", "Unavailable"),
            run.SecretTransferError,
        ),
        (RuntimeError("connection reset"), RuntimeError),
    ],
)
def test_read_back_failure_rolls_back_the_version_just_written(error, expected):
    source = MemorySecretStore({"src/a": b"first", "src/db-url": _CANARY})
    target = MemorySecretStore()
    target.fail_read_back_for["tgt-db-url"] = error

    with pytest.raises(expected):
        run.transfer_secrets(
            (_item("src/a", "tgt-a"), _item()),
            frozenset({"src/a", "src/db-url"}),
            source,
            target,
            _RUN_ID,
        )

    assert target.read_current("tgt-a") is None
    assert target.read_current("tgt-db-url") is None


def test_rollback_continues_past_a_failed_retire():
    source = MemorySecretStore({"src/a": b"first", "src/db-url": _CANARY})
    target = MemorySecretStore()
    target.corrupt_read_back_for.add("tgt-db-url")
    target.fail_retire_for.add("tgt-db-url")

    with pytest.raises(run.SecretTransferError) as excinfo:
        run.transfer_secrets(
            (_item("src/a", "tgt-a"), _item()),
            frozenset({"src/a", "src/db-url"}),
            source,
            target,
            _RUN_ID,
        )

    assert excinfo.value.error_class == "mismatch"
    assert target.read_current("tgt-a") is None


def test_roll_back_reports_failed_retire():
    target = MemorySecretStore()
    source = MemorySecretStore({"src/db-url": _CANARY})
    results = run.transfer_secrets(
        (_item(),), frozenset({"src/db-url"}), source, target, _RUN_ID
    )
    target.fail_retire_for.add("tgt-db-url")

    assert run.roll_back_transfer(results, target) == ("failed",)


def test_allowed_ids_must_be_a_frozenset():
    # 문자열은 Collection 이라 'in' 이 부분 문자열 검사로 바뀐다.
    with pytest.raises(TypeError):
        run.transfer_secrets(
            (_item(),),
            "src/db-url",
            MemorySecretStore({"src/db-url": _CANARY}),
            MemorySecretStore(),
            _RUN_ID,
        )
