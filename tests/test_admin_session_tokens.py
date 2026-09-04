"""
Админ-токены без состояния.

Раньше токен жил в dict в памяти процесса: рестарт API выбрасывал всех, а
две реплики не видели токены друг друга. Теперь токен - подписанная строка,
валидная везде, где известен ADMIN_SECRET_KEY.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.deps import (  # noqa: E402
    _SESSION_TTL_HOURS,
    create_admin_session,
    validate_admin_session,
)


def _settings(key: str = "k" * 48):
    return SimpleNamespace(admin_secret_key=SimpleNamespace(get_secret_value=lambda: key))


def test_token_survives_process_restart():
    """Валидация не зависит от того, кто выпустил токен."""
    settings = _settings()
    token = create_admin_session(settings)
    # «другой процесс» - свежий Settings с тем же ключом
    assert validate_admin_session(token, _settings())


def test_tampered_expiry_is_rejected():
    settings = _settings()
    expires, nonce, sig = create_admin_session(settings).split(".")
    later = str(int(expires) + 3600)
    assert not validate_admin_session(f"{later}.{nonce}.{sig}", settings)


def test_wrong_key_is_rejected():
    token = create_admin_session(_settings("a" * 48))
    assert not validate_admin_session(token, _settings("b" * 48))


def test_expired_token_is_rejected(monkeypatch):
    import src.api.deps as deps
    from datetime import datetime, timedelta, timezone

    settings = _settings()
    token = create_admin_session(settings)

    class _Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(hours=_SESSION_TTL_HOURS + 1)

    monkeypatch.setattr(deps, "datetime", _Future)
    assert not validate_admin_session(token, settings)


def test_garbage_is_rejected():
    settings = _settings()
    for junk in ("", "abc", "1.2", "x.y.z", "1.n.s.extra"):
        assert not validate_admin_session(junk, settings)


def test_signing_key_is_not_the_admin_key():
    """Подпись токена не должна быть HMAC прямо на ADMIN_SECRET_KEY."""
    import hashlib
    import hmac

    settings = _settings()
    expires, nonce, sig = create_admin_session(settings).split(".")
    naive = hmac.new(
        b"k" * 48, f"{expires}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()
    assert sig != naive
