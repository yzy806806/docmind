"""Pytest configuration and shared fixtures for docmind tests.

Provides autouse fixtures that prevent cross-test leakage of the
module-level crypto singleton (``crypto._encryptor``) and the
``DOCMIND_EMAIL_ENCRYPTION_KEY`` environment variable.  These used to
live only in ``tests/test_email_encryption.py`` but were promoted here
so that *every* test file benefits -- any test that connects a
``Database`` triggers ``init_encryptor_for_db`` which sets the
singleton, and without a reset between tests the key from one test's
DB can bleed into the next.
"""

import pytest


# -- Autouse: crypto singleton isolation ---------------------------


@pytest.fixture(autouse=True)
def _reset_crypto_singleton():
    """Reset the module-level crypto._encryptor singleton before and after each test.

    Each ``Database.connect()`` calls ``init_encryptor_for_db`` which sets
    ``crypto._encryptor`` to the encryptor for that DB. Without resetting
    between tests, a test that doesn't create its own DB could pick up the
    encryptor (and Fernet key) from a previous test's DB, causing
    cross-database key leakage and intermittent decryption failures.

    The per-instance ``Database._encryptor`` attribute (reset in
    ``disconnect()``) prevents leakage at the instance level; this fixture
    covers the module-level fallback singleton.
    """
    from src.core import crypto

    crypto._encryptor = None
    yield
    crypto._encryptor = None


@pytest.fixture(autouse=True)
def _clean_encryption_key_env(monkeypatch: pytest.MonkeyPatch):
    """Remove any externally-set ``DOCMIND_EMAIL_ENCRYPTION_KEY`` env var.

    Ensures each test starts clean -- the env var is checked by
    ``CredentialEncryptor.init()`` and would override the DB-stored key
    if left over from a previous test or the host environment.
    """
    monkeypatch.delenv("DOCMIND_EMAIL_ENCRYPTION_KEY", raising=False)


# -- Marker configuration ------------------------------------------


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "skipif_not_redis: skip test if fakeredis is not available",
    )


def pytest_collection_modifyitems(config, items):
    """Skip tests marked with skipif_not_redis if fakeredis is not available."""
    try:
        import fakeredis  # noqa: F401
        skip_redis = False
    except ImportError:
        skip_redis = True

    if skip_redis:
        skip_marker = pytest.mark.skip(reason="fakeredis not installed")
        for item in items:
            if item.get_closest_marker("skipif_not_redis"):
                item.add_marker(skip_marker)
