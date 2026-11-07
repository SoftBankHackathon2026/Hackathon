import fake_ingress
import fake_redis
import pytest

from crosscloud import ingress_control, ingress_drain


def _bulk(text):
    return f"${len(text)}\r\n{text}\r\n".encode()


def _group(pending, lag):
    lag_part = b"$-1\r\n" if lag is None else f":{lag}\r\n".encode()
    return (
        b"*12\r\n"
        + _bulk("name")
        + _bulk("delivery")
        + _bulk("consumers")
        + b":1\r\n"
        + _bulk("pending")
        + f":{pending}\r\n".encode()
        + _bulk("last-delivered-id")
        + _bulk("1-0")
        + _bulk("entries-read")
        + b":5\r\n"
        + _bulk("lag")
        + lag_part
    )


def _reply(*groups):
    return f"*{len(groups)}\r\n".encode() + b"".join(groups)


def _check(redis_url, ingress_url):
    return ingress_drain.drain_problems(
        redis_url, ingress_control.IngressControl(ingress_url, token=None)
    )


def test_drained_when_no_pending_lag_or_in_flight():
    fake = fake_ingress.FakeIngress()
    with fake_redis.serve(_reply(_group(0, 0))) as (redis_url, received):
        with fake.serve() as url:
            problems = _check(redis_url, url)

    assert problems == ()
    assert b"XINFO" in received[0]
    assert b"ingress:inbox" in received[0]


@pytest.mark.parametrize(
    ("pending", "lag", "word"),
    [(2, 0, "pending"), (0, 3, "lag"), (0, None, "lag")],
)
def test_undelivered_requests_block_cleanup(pending, lag, word):
    fake = fake_ingress.FakeIngress()
    with fake_redis.serve(_reply(_group(pending, lag))) as (redis_url, _):
        with fake.serve() as url:
            problems = _check(redis_url, url)

    assert any(word in p for p in problems)


def test_in_flight_or_error_blocks_cleanup():
    fake = fake_ingress.FakeIngress()
    fake.state.update(in_flight=1, last_error="CONTENT_CONFLICT")
    with fake_redis.serve(_reply(_group(0, 0))) as (redis_url, _):
        with fake.serve() as url:
            problems = _check(redis_url, url)

    assert any("in_flight" in p for p in problems)
    assert any("CONTENT_CONFLICT" in p for p in problems)


def test_missing_delivery_group_blocks_cleanup():
    fake = fake_ingress.FakeIngress()
    with fake_redis.serve(_reply()) as (redis_url, _):
        with fake.serve() as url:
            problems = _check(redis_url, url)

    assert any("delivery" in p for p in problems)


def test_redis_error_reply_blocks_cleanup():
    fake = fake_ingress.FakeIngress()
    with fake_redis.serve(b"-ERR no such key\r\n") as (redis_url, _):
        with fake.serve() as url:
            problems = _check(redis_url, url)

    assert any("no such key" in p for p in problems)


def test_unreachable_redis_and_ingress_block_cleanup():
    problems = _check("redis://127.0.0.1:9", "http://127.0.0.1:9")

    assert len(problems) == 2


def test_rejects_non_redis_url():
    with pytest.raises(ValueError):
        _check("http://127.0.0.1:6379", "http://127.0.0.1:9")


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://127.0.0.1:6379/1",
        "redis://:secret@127.0.0.1:6379",
        "redis://user:secret@127.0.0.1:6379/0",
        "redis://127.0.0.1:6379?db=2",
    ],
)
def test_rejects_url_naming_other_db_or_auth(redis_url):
    # SELECT·AUTH 없이 묻으면 DB 0 을 보고 미처리를 0 으로 오판한다.
    with pytest.raises(ValueError) as caught:
        _check(redis_url, "http://127.0.0.1:9")

    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("path", ["", "/", "/0"])
def test_accepts_default_db_path(path):
    fake = fake_ingress.FakeIngress()
    with fake_redis.serve(_reply(_group(0, 0))) as (redis_url, _):
        with fake.serve() as url:
            problems = _check(redis_url + path, url)

    assert problems == ()


def test_bulk_string_without_crlf_after_length_blocks_cleanup():
    # 길이 8 뒤에 CRLF 가 아니라 "XX" 가 오면 응답이 깨진 것이다.
    broken = _group(0, 0).replace(_bulk("delivery"), b"$8\r\ndeliveryXX")
    fake = fake_ingress.FakeIngress()
    with fake_redis.serve(_reply(broken)) as (redis_url, _):
        with fake.serve() as url:
            problems = _check(redis_url, url)

    assert any("XINFO" in p for p in problems)
