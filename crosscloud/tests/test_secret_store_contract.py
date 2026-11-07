from crosscloud.secret_transfer import store


def test_version_token_is_stable_per_run_and_key():
    first = store.version_token("ccx-s1-forward", "tgt-db-url")

    assert first == store.version_token("ccx-s1-forward", "tgt-db-url")
    assert first != store.version_token("ccx-s1-other", "tgt-db-url")
    assert first != store.version_token("ccx-s1-forward", "tgt-other")
    assert 32 <= len(first) <= 64
