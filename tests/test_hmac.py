import pytest

from ibkr_exec.api.auth import AuthError, Verifier, sign

SECRETS = {"bot": "bot-secret", "admin": "admin-secret"}
BODY = b'{"a":1}'


def make(now: float = 1_000_000.0, key: str = "bot", ts: float | None = None, secret=None):
    ts_s = str(int(now if ts is None else ts))
    sig = sign(secret or SECRETS[key], "POST", "/signals", ts_s, BODY)
    return dict(
        method="POST",
        path="/signals",
        body=BODY,
        key_id=key,
        timestamp=ts_s,
        signature=sig,
        now=now,
    )


def test_valid_signature_passes() -> None:
    v = Verifier(SECRETS)
    assert v.verify(**make()) == "bot"


def test_bad_signature_rejected() -> None:
    v = Verifier(SECRETS)
    args = make()
    args["signature"] = "0" * 64
    with pytest.raises(AuthError) as e:
        v.verify(**args)
    assert e.value.status == 401 and "signature" in e.value.detail


def test_wrong_secret_rejected() -> None:
    v = Verifier(SECRETS)
    with pytest.raises(AuthError):
        v.verify(**make(secret="not-it"))


def test_tampered_body_rejected() -> None:
    v = Verifier(SECRETS)
    args = make()
    args["body"] = b'{"a":2}'
    with pytest.raises(AuthError):
        v.verify(**args)


def test_old_timestamp_rejected() -> None:
    v = Verifier(SECRETS)
    with pytest.raises(AuthError) as e:
        v.verify(**make(now=1_000_031.0, ts=1_000_000.0))
    assert "timestamp" in e.value.detail
    # 30 s skew is still fine
    v.verify(**make(now=1_000_030.0, ts=1_000_000.0))


def test_future_timestamp_rejected() -> None:
    v = Verifier(SECRETS)
    with pytest.raises(AuthError):
        v.verify(**make(now=1_000_000.0, ts=1_000_040.0))


def test_replay_rejected() -> None:
    v = Verifier(SECRETS)
    args = make()
    v.verify(**args)
    with pytest.raises(AuthError) as e:
        v.verify(**args)
    assert "replayed" in e.value.detail


def test_replay_cache_purges_old_entries() -> None:
    v = Verifier(SECRETS)
    v.verify(**make(now=1_000_000.0))
    v.verify(**make(now=1_000_100.0))
    assert len(v._seen) == 1


def test_key_not_allowed_for_endpoint() -> None:
    v = Verifier(SECRETS)
    with pytest.raises(AuthError) as e:
        v.verify(**make(key="bot"), allowed_keys=("admin",))
    assert e.value.status == 403
    assert v.verify(**make(key="admin"), allowed_keys=("admin",)) == "admin"


def test_missing_headers() -> None:
    v = Verifier(SECRETS)
    args = make()
    args["timestamp"] = None
    with pytest.raises(AuthError):
        v.verify(**args)
    with pytest.raises(AuthError):
        v.verify(**{**make(), "key_id": "unknown"})
