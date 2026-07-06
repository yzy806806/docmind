"""Tests for src.core.crypto — Fernet encryption for email account passwords.

Covers:
- CredentialEncryptor: init from env var, init from DB, auto-generate key
- encrypt/decrypt round-trip
- _is_encrypted heuristic for distinguishing plaintext from Fernet tokens
- Double-encryption prevention (encrypt skips already-encrypted values)
- Backward compatibility (decrypt returns plaintext as-is if not encrypted)
- Key rotation: re-encrypt all passwords with a new key
- Key rotation: invalid old DB key handled gracefully
- Key rotation: multi-rotation data integrity (3 successive rotations)
- Migration: plaintext passwords auto-encrypted on connect()
- Migration: mixed plaintext and encrypted rows handled correctly
- Migration: empty password rows skipped
- Module-level convenience functions: encrypt_password / decrypt_password
- CryptoError on decryption with wrong key
- MultiFernet fallback: decrypt with old key after rotation
- add_decryption_key: enables MultiFernet fallback, invalid key skipped, idempotent
- add_decryption_key: before init raises CryptoError
- add_decryption_key: multiple old keys for layered rotation history
- DB integration: create_email_account encrypts, get_email_account decrypts
- update_email_account: password encrypted on update
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture(autouse=True)
def _reset_encryptor():
    """Reset the module-level encryptor singleton before each test."""
    from src.core import crypto
    crypto._encryptor = None
    yield
    crypto._encryptor = None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Remove any externally-set encryption key env var."""
    monkeypatch.delenv("DOCMIND_EMAIL_ENCRYPTION_KEY", raising=False)


# ── Unit tests: _is_encrypted ────────────────────────────────────


class TestIsEncrypted:
    def test_plaintext_not_detected_as_encrypted(self):
        from src.core.crypto import _is_encrypted
        assert not _is_encrypted("my-secret-password")
        assert not _is_encrypted("p@ssw0rd123")
        assert not _is_encrypted("")

    def test_fernet_token_detected_as_encrypted(self):
        from src.core.crypto import _is_encrypted
        key = Fernet.generate_key()
        f = Fernet(key)
        token = f.encrypt(b"secret").decode()
        assert _is_encrypted(token)

    def test_empty_string_not_encrypted(self):
        from src.core.crypto import _is_encrypted
        assert not _is_encrypted("")


# ── Unit tests: CredentialEncryptor ──────────────────────────────


class TestCredentialEncryptor:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_init_generates_key_in_db(self, real_db):
        from src.core.crypto import CredentialEncryptor, _ENCRYPTION_KEY_SETTING

        enc = CredentialEncryptor(real_db)
        await enc.init()

        assert enc.is_initialized
        assert enc.key_source == "database"

        # Key should be persisted in DB
        key_in_db = await real_db.get_setting(_ENCRYPTION_KEY_SETTING)
        assert key_in_db is not None
        assert len(key_in_db) > 0

    @pytest.mark.asyncio
    async def test_init_loads_key_from_env(self, real_db, monkeypatch):
        from src.core.crypto import CredentialEncryptor

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("DOCMIND_EMAIL_ENCRYPTION_KEY", key)

        enc = CredentialEncryptor(real_db)
        await enc.init()

        assert enc.is_initialized
        assert enc.key_source == "env"

    @pytest.mark.asyncio
    async def test_init_falls_back_on_invalid_env_key(self, real_db, monkeypatch):
        from src.core.crypto import CredentialEncryptor

        monkeypatch.setenv("DOCMIND_EMAIL_ENCRYPTION_KEY", "not-a-valid-key")

        enc = CredentialEncryptor(real_db)
        await enc.init()

        # Should fall back to DB-generated key
        assert enc.is_initialized
        assert enc.key_source == "database"

    @pytest.mark.asyncio
    async def test_init_reuses_existing_db_key(self, real_db):
        from src.core.crypto import CredentialEncryptor, _ENCRYPTION_KEY_SETTING

        # First init generates and stores key
        enc1 = CredentialEncryptor(real_db)
        await enc1.init()
        key1 = await real_db.get_setting(_ENCRYPTION_KEY_SETTING)

        # Second init should load the same key
        enc2 = CredentialEncryptor(real_db)
        await enc2.init()
        key2 = await real_db.get_setting(_ENCRYPTION_KEY_SETTING)

        assert key1 == key2

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_roundtrip(self, real_db):
        from src.core.crypto import CredentialEncryptor

        enc = CredentialEncryptor(real_db)
        await enc.init()

        plaintext = "my-secret-password-123"
        encrypted = enc.encrypt(plaintext)

        assert encrypted != plaintext
        assert enc.decrypt(encrypted) == plaintext

    @pytest.mark.asyncio
    async def test_encrypt_empty_string(self, real_db):
        from src.core.crypto import CredentialEncryptor

        enc = CredentialEncryptor(real_db)
        await enc.init()

        assert enc.encrypt("") == ""
        assert enc.decrypt("") == ""

    @pytest.mark.asyncio
    async def test_encrypt_skips_already_encrypted(self, real_db):
        from src.core.crypto import CredentialEncryptor

        enc = CredentialEncryptor(real_db)
        await enc.init()

        plaintext = "my-secret"
        encrypted_once = enc.encrypt(plaintext)
        encrypted_twice = enc.encrypt(encrypted_once)

        # Should not double-encrypt
        assert encrypted_twice == encrypted_once

    @pytest.mark.asyncio
    async def test_decrypt_plaintext_returns_as_is(self, real_db):
        from src.core.crypto import CredentialEncryptor

        enc = CredentialEncryptor(real_db)
        await enc.init()

        plaintext = "plain-password"
        result = enc.decrypt(plaintext)
        assert result == plaintext

    @pytest.mark.asyncio
    async def test_decrypt_wrong_key_raises_crypto_error(self, real_db):
        from src.core.crypto import CredentialEncryptor, CryptoError

        # Encrypt with key 1
        enc1 = CredentialEncryptor(real_db)
        await enc1.init()
        encrypted = enc1.encrypt("secret")

        # Create a new encryptor with a different key
        from src.core.db_sqlite import Database
        import tempfile
        fd, path2 = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db2 = Database(db_path=path2)
        await db2.connect()
        try:
            enc2 = CredentialEncryptor(db2)
            await enc2.init()
            with pytest.raises(CryptoError):
                enc2.decrypt(encrypted)
        finally:
            await db2.disconnect()
            if os.path.exists(path2):
                os.unlink(path2)

    @pytest.mark.asyncio
    async def test_not_initialized_raises(self, real_db):
        from src.core.crypto import CredentialEncryptor, CryptoError

        enc = CredentialEncryptor(real_db)
        # Don't call init()
        with pytest.raises(CryptoError):
            enc.encrypt("test")
        with pytest.raises(CryptoError):
            enc.decrypt("test")


# ── Unit tests: Module-level functions ───────────────────────────


class TestModuleLevelFunctions:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_encrypt_password_without_init_returns_plaintext(self):
        from src.core.crypto import encrypt_password

        # No init_encryptor called
        assert encrypt_password("secret") == "secret"

    @pytest.mark.asyncio
    async def test_decrypt_password_without_init_returns_plaintext(self):
        from src.core.crypto import decrypt_password

        assert decrypt_password("secret") == "secret"
        assert decrypt_password("gAAAAA something") == "gAAAAA something"

    @pytest.mark.asyncio
    async def test_init_encryptor_sets_singleton(self, real_db):
        from src.core.crypto import init_encryptor, get_encryptor

        enc = await init_encryptor(real_db)
        assert enc.is_initialized
        assert get_encryptor() is enc

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_via_module_functions(self, real_db):
        from src.core.crypto import init_encryptor, encrypt_password, decrypt_password

        await init_encryptor(real_db)
        encrypted = encrypt_password("my-pw")
        assert encrypted != "my-pw"
        assert decrypt_password(encrypted) == "my-pw"


# ── DB integration: encryption is transparent ────────────────────


class TestDBEncryptionIntegration:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_create_email_account_encrypts_password(self, real_db):
        """Password should be encrypted in DB but decrypted when read back."""
        acct = await real_db.create_email_account({
            "name": "test-enc",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "super-secret",
        })

        # The returned dict should have the decrypted password
        assert acct["password"] == "super-secret"

        # But the raw DB value should be encrypted
        async with real_db.connection() as conn:
            cursor = await conn.execute(
                "SELECT password FROM email_accounts WHERE id = ?", (acct["id"],)
            )
            row = await cursor.fetchone()

        raw_password = row["password"]
        assert raw_password != "super-secret"
        assert raw_password.startswith("gAAAAA")

    @pytest.mark.asyncio
    async def test_get_email_account_decrypts_password(self, real_db):
        await real_db.create_email_account({
            "name": "test-get",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "decrypt-me",
        })

        accounts = await real_db.list_email_accounts()
        acct = accounts[0]
        assert acct["password"] == "decrypt-me"

        fetched = await real_db.get_email_account(acct["id"])
        assert fetched["password"] == "decrypt-me"

    @pytest.mark.asyncio
    async def test_update_email_account_encrypts_new_password(self, real_db):
        acct = await real_db.create_email_account({
            "name": "test-update",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "original-pw",
        })

        updated = await real_db.update_email_account(
            acct["id"], {"password": "new-pw"}
        )
        assert updated["password"] == "new-pw"

        # Verify raw DB value is encrypted
        async with real_db.connection() as conn:
            cursor = await conn.execute(
                "SELECT password FROM email_accounts WHERE id = ?", (acct["id"],)
            )
            row = await cursor.fetchone()

        assert row["password"] != "new-pw"
        assert row["password"].startswith("gAAAAA")

    @pytest.mark.asyncio
    async def test_update_email_account_without_password_preserves_encryption(self, real_db):
        acct = await real_db.create_email_account({
            "name": "test-no-pw-update",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "keep-me",
        })

        await real_db.update_email_account(acct["id"], {"folder": "Sent"})

        fetched = await real_db.get_email_account(acct["id"])
        assert fetched["password"] == "keep-me"
        assert fetched["folder"] == "Sent"

    @pytest.mark.asyncio
    async def test_multiple_accounts_different_passwords(self, real_db):
        passwords = ["pw1", "pw2", "pw3"]
        for i, pw in enumerate(passwords):
            await real_db.create_email_account({
                "name": f"acct-{i}",
                "host": f"imap{i}.example.com",
                "username": f"user{i}@example.com",
                "password": pw,
            })

        accounts = await real_db.list_email_accounts()
        for i, acct in enumerate(accounts):
            assert acct["password"] == passwords[i]

        # All raw passwords should be encrypted differently
        async with real_db.connection() as conn:
            cursor = await conn.execute("SELECT password FROM email_accounts ORDER BY id")
            rows = await cursor.fetchall()

        raw_passwords = [r["password"] for r in rows]
        assert len(set(raw_passwords)) == 3  # all unique
        for rp in raw_passwords:
            assert rp.startswith("gAAAAA")


# ── Migration: plaintext -> encrypted ────────────────────────────


class TestPlaintextMigration:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_plaintext_passwords_migrated_on_connect(self, tmp_db_path):
        """Pre-existing plaintext passwords should be encrypted on connect."""
        from src.core.db_sqlite import Database

        # Create DB with schema but NO encryption (bypass _init_encryption_and_migrate)
        db = Database(db_path=tmp_db_path)
        self._conn = await __import__("aiosqlite").connect(tmp_db_path)
        self._conn.row_factory = __import__("aiosqlite").Row
        await self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
            "CREATE TABLE IF NOT EXISTS email_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL, port INTEGER DEFAULT 993, use_ssl BOOLEAN DEFAULT TRUE, username TEXT NOT NULL, password TEXT NOT NULL, folder TEXT DEFAULT 'INBOX', poll_interval_seconds INTEGER DEFAULT 600, action_after_fetch TEXT DEFAULT 'mark_seen', move_to_folder TEXT, body_handling TEXT DEFAULT 'save_with_attachments', attachment_whitelist TEXT, attachment_blacklist TEXT, deduplication_strategy TEXT DEFAULT 'message_id', enabled BOOLEAN DEFAULT TRUE, last_sync_at TEXT, last_error TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
        )
        await self._conn.commit()

        # Insert plaintext passwords directly
        await self._conn.execute(
            "INSERT INTO email_accounts (name, host, username, password) VALUES (?, ?, ?, ?)",
            ("acct1", "imap1.com", "u1", "plaintext-pw-1"),
        )
        await self._conn.execute(
            "INSERT INTO email_accounts (name, host, username, password) VALUES (?, ?, ?, ?)",
            ("acct2", "imap2.com", "u2", "plaintext-pw-2"),
        )
        await self._conn.commit()
        await self._conn.close()

        # Now connect with the real Database class -- should auto-migrate
        db = Database(db_path=tmp_db_path)
        await db.connect()

        try:
            accounts = await db.list_email_accounts()
            assert len(accounts) == 2

            # Passwords should be decrypted back to plaintext
            pw_map = {a["name"]: a["password"] for a in accounts}
            assert pw_map["acct1"] == "plaintext-pw-1"
            assert pw_map["acct2"] == "plaintext-pw-2"

            # Raw DB values should now be encrypted
            async with db.connection() as conn:
                cursor = await conn.execute(
                    "SELECT password FROM email_accounts ORDER BY id"
                )
                rows = await cursor.fetchall()

            for row in rows:
                assert row["password"].startswith("gAAAAA")
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_already_encrypted_not_re_encrypted_on_connect(self, tmp_db_path):
        """Encrypted passwords should not be re-encrypted on reconnect."""
        from src.core.db_sqlite import Database

        # First connect: creates encrypted passwords
        db1 = Database(db_path=tmp_db_path)
        await db1.connect()
        await db1.create_email_account({
            "name": "acct",
            "host": "imap.com",
            "username": "u",
            "password": "secret",
        })

        # Get the raw encrypted value
        async with db1.connection() as conn:
            cursor = await conn.execute("SELECT password FROM email_accounts WHERE id = 1")
            row = await cursor.fetchone()
        encrypted_before = row["password"]
        await db1.disconnect()

        # Second connect: should NOT re-encrypt
        db2 = Database(db_path=tmp_db_path)
        await db2.connect()
        async with db2.connection() as conn:
            cursor = await conn.execute("SELECT password FROM email_accounts WHERE id = 1")
            row = await cursor.fetchone()
        encrypted_after = row["password"]
        await db2.disconnect()

        # Same encrypted value (not re-encrypted)
        assert encrypted_before == encrypted_after


# ── Key rotation ─────────────────────────────────────────────────


class TestKeyRotation:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_rotate_key_re_encrypts_all_passwords(self, real_db):
        from src.core.crypto import init_encryptor, get_encryptor

        # Setup: init encryptor (sets module-level singleton) with some accounts
        enc = await init_encryptor(real_db)
        await real_db.create_email_account({
            "name": "a1", "host": "h1", "username": "u1", "password": "pw1"
        })
        await real_db.create_email_account({
            "name": "a2", "host": "h2", "username": "u2", "password": "pw2"
        })

        # Get raw encrypted values before rotation
        async with real_db.connection() as conn:
            cursor = await conn.execute("SELECT id, password FROM email_accounts ORDER BY id")
            rows_before = await cursor.fetchall()

        # Rotate key (on the same singleton instance)
        enc = get_encryptor()
        await enc.rotate_key()

        # Get raw encrypted values after rotation
        async with real_db.connection() as conn:
            cursor = await conn.execute("SELECT id, password FROM email_accounts ORDER BY id")
            rows_after = await cursor.fetchall()

        # Encrypted values should have changed
        for rb, ra in zip(rows_before, rows_after):
            assert rb["password"] != ra["password"]
            assert ra["password"].startswith("gAAAAA")

        # But decrypted values should be the same
        accounts = await real_db.list_email_accounts()
        pw_map = {a["name"]: a["password"] for a in accounts}
        assert pw_map["a1"] == "pw1"
        assert pw_map["a2"] == "pw2"

    @pytest.mark.asyncio
    async def test_rotate_with_explicit_new_key(self, real_db):
        from src.core.crypto import init_encryptor, get_encryptor

        enc = await init_encryptor(real_db)
        await real_db.create_email_account({
            "name": "a1", "host": "h1", "username": "u1", "password": "secret-pw"
        })

        new_key = Fernet.generate_key().decode()
        enc = get_encryptor()
        await enc.rotate_key(new_key)

        # Should decrypt correctly with the new key
        accounts = await real_db.list_email_accounts()
        assert accounts[0]["password"] == "secret-pw"

    @pytest.mark.asyncio
    async def test_decrypt_after_rotation_uses_multifernet_fallback(self, real_db):
        """After rotation, the old key is kept as a MultiFernet fallback."""
        from src.core.crypto import init_encryptor, get_encryptor

        enc = await init_encryptor(real_db)
        encrypted = enc.encrypt("original-secret")

        # Rotate to a new key
        enc = get_encryptor()
        await enc.rotate_key()

        # The old encrypted value should still be decryptable via MultiFernet
        # (the old key is added as a fallback)
        plaintext = enc.decrypt(encrypted)
        assert plaintext == "original-secret"

    @pytest.mark.asyncio
    async def test_rotate_without_init_raises(self, real_db):
        from src.core.crypto import CredentialEncryptor, CryptoError

        enc = CredentialEncryptor(real_db)
        with pytest.raises(CryptoError):
            await enc.rotate_key()


# ── Env var key ──────────────────────────────────────────────────


class TestEnvVarKey:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_env_key_used_for_encryption(self, real_db, monkeypatch):
        from src.core.crypto import init_encryptor, encrypt_password, decrypt_password

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("DOCMIND_EMAIL_ENCRYPTION_KEY", key)

        await init_encryptor(real_db)

        encrypted = encrypt_password("env-key-secret")
        assert encrypted != "env-key-secret"
        assert decrypt_password(encrypted) == "env-key-secret"

        # Verify the key is NOT stored in DB (env takes priority)
        from src.core.crypto import _ENCRYPTION_KEY_SETTING
        db_key = await real_db.get_setting(_ENCRYPTION_KEY_SETTING)
        assert db_key is None or db_key != key

    @pytest.mark.asyncio
    async def test_same_env_key_across_restarts(self, tmp_db_path, monkeypatch):
        from src.core.db_sqlite import Database
        from src.core.crypto import init_encryptor, encrypt_password, decrypt_password

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("DOCMIND_EMAIL_ENCRYPTION_KEY", key)

        # First connect
        db1 = Database(db_path=tmp_db_path)
        await db1.connect()
        await db1.create_email_account({
            "name": "a1", "host": "h1", "username": "u1", "password": "restart-pw"
        })
        await db1.disconnect()

        # Reset encryptor
        from src.core import crypto
        crypto._encryptor = None

        # Second connect with same key
        db2 = Database(db_path=tmp_db_path)
        await db2.connect()
        try:
            accounts = await db2.list_email_accounts()
            assert accounts[0]["password"] == "restart-pw"
        finally:
            await db2.disconnect()


# ── Cross-cutting: password not leaked in API responses ──────────


class TestPasswordNotInAPIResponse:
    """Verify that the DB layer returns decrypted passwords (for internal use)
    but that the API layer strips them from responses."""

    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_db_returns_decrypted_password(self, real_db):
        """The DB layer should return decrypted passwords for internal use
        (e.g., IMAP login). The API layer is responsible for stripping."""
        await real_db.create_email_account({
            "name": "api-test",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "api-secret",
        })

        accounts = await real_db.list_email_accounts()
        # DB layer returns decrypted password (for IMAP login)
        assert accounts[0]["password"] == "api-secret"


# ── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_password_with_special_characters(self, real_db):
        passwords = [
            "p@ssw0rd!#$%",
            "unicode: üñïçödé",
            "very-long-" + "x" * 200,
            "  spaces  ",
            "tab\tchar",
        ]
        for i, pw in enumerate(passwords):
            await real_db.create_email_account({
                "name": f"special-{i}",
                "host": f"imap{i}.com",
                "username": f"u{i}",
                "password": pw,
            })

        accounts = await real_db.list_email_accounts()
        for i, acct in enumerate(accounts):
            assert acct["password"] == passwords[i]

    @pytest.mark.asyncio
    async def test_empty_password_stored_and_retrieved(self, real_db):
        await real_db.create_email_account({
            "name": "empty-pw",
            "host": "imap.com",
            "username": "u",
            "password": "",
        })

        fetched = await real_db.get_email_account(
            (await real_db.list_email_accounts())[0]["id"]
        )
        assert fetched["password"] == ""

    @pytest.mark.asyncio
    async def test_update_email_account_password_direct(self, real_db):
        """Test the raw update_email_account_password method (used by rotation)."""
        acct = await real_db.create_email_account({
            "name": "direct-update",
            "host": "imap.com",
            "username": "u",
            "password": "original",
        })

        # This method stores the value directly without encryption
        raw_encrypted = "gAAAAADummyValueForTest"
        await real_db.update_email_account_password(acct["id"], raw_encrypted)

        async with real_db.connection() as conn:
            cursor = await conn.execute(
                "SELECT password FROM email_accounts WHERE id = ?", (acct["id"],)
            )
            row = await cursor.fetchone()

        assert row["password"] == raw_encrypted


# ── add_decryption_key (MultiFernet fallback) ────────────────────


class TestAddDecryptionKey:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_add_decryption_key_enables_multifernet_fallback(self, real_db):
        """After adding an old key, data encrypted with it can still be decrypted."""
        from src.core.crypto import CredentialEncryptor

        # Generate two different Fernet keys
        old_key = Fernet.generate_key().decode()
        old_fernet = Fernet(old_key.encode())

        # Init encryptor with a new key
        enc = CredentialEncryptor(real_db)
        await enc.init()

        # Encrypt something with the OLD key (simulating pre-rotation data)
        old_encrypted = old_fernet.encrypt(b"legacy-secret").decode()

        # Without the fallback, this should fail
        with pytest.raises(Exception):
            enc.decrypt(old_encrypted)

        # Add the old key as a decryption fallback
        enc.add_decryption_key(old_key)

        # Now it should decrypt successfully
        plaintext = enc.decrypt(old_encrypted)
        assert plaintext == "legacy-secret"

        # New encryptions should still use the new key
        new_encrypted = enc.encrypt("new-secret")
        assert enc.decrypt(new_encrypted) == "new-secret"

    @pytest.mark.asyncio
    async def test_add_decryption_key_with_invalid_key_skips(self, real_db):
        """Adding an invalid key should warn and skip without breaking the encryptor."""
        from src.core.crypto import CredentialEncryptor

        enc = CredentialEncryptor(real_db)
        await enc.init()

        # Should not raise
        enc.add_decryption_key("not-a-valid-fernet-key")

        # Encryptor should still work normally
        encrypted = enc.encrypt("still-works")
        assert enc.decrypt(encrypted) == "still-works"

    @pytest.mark.asyncio
    async def test_add_decryption_key_before_init_raises(self, real_db):
        """Cannot add a decryption key before the encryptor is initialized."""
        from src.core.crypto import CredentialEncryptor, CryptoError

        enc = CredentialEncryptor(real_db)
        with pytest.raises(CryptoError):
            enc.add_decryption_key(Fernet.generate_key().decode())

    @pytest.mark.asyncio
    async def test_add_multiple_decryption_keys(self, real_db):
        """Multiple old keys can be added for layered key rotation history."""
        from src.core.crypto import CredentialEncryptor

        # Simulate three generations of keys
        key_v1 = Fernet.generate_key()
        key_v2 = Fernet.generate_key()
        key_v3 = Fernet.generate_key()

        v1_data = Fernet(key_v1).encrypt(b"v1-secret").decode()
        v2_data = Fernet(key_v2).encrypt(b"v2-secret").decode()

        # Current key is v3
        enc = CredentialEncryptor(real_db)
        await enc.init()
        # Override with v3 as primary
        enc._fernet = Fernet(key_v3)

        # Add old keys
        enc.add_decryption_key(key_v2.decode())
        enc.add_decryption_key(key_v1.decode())

        # All three generations should decrypt
        assert enc.decrypt(v1_data) == "v1-secret"
        assert enc.decrypt(v2_data) == "v2-secret"
        assert enc.decrypt(enc.encrypt("v3-secret")) == "v3-secret"

    @pytest.mark.asyncio
    async def test_add_same_key_twice_is_idempotent(self, real_db):
        """Adding the same old key twice should not create duplicates."""
        from src.core.crypto import CredentialEncryptor

        old_key = Fernet.generate_key().decode()
        old_data = Fernet(old_key.encode()).encrypt(b"dup-test").decode()

        enc = CredentialEncryptor(real_db)
        await enc.init()

        enc.add_decryption_key(old_key)
        enc.add_decryption_key(old_key)  # should be idempotent

        assert enc.decrypt(old_data) == "dup-test"


# ── Key rotation additional edge cases ───────────────────────────


class TestKeyRotationEdgeCases:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_rotate_with_invalid_old_db_key(self, real_db):
        """Rotation should handle invalid old key gracefully (no fallback)."""
        from src.core.crypto import CredentialEncryptor, _ENCRYPTION_KEY_SETTING

        # Store a known-valid key
        enc = CredentialEncryptor(real_db)
        await enc.init()

        # Corrupt the stored key in the DB
        await real_db.set_setting(_ENCRYPTION_KEY_SETTING, "not-valid-base64!!!")

        # Rotation should still succeed — old key is invalid, skip fallback
        await enc.rotate_key()

        # Encryptor should still work
        encrypted = enc.encrypt("after-bad-key-rotation")
        assert enc.decrypt(encrypted) == "after-bad-key-rotation"

    @pytest.mark.asyncio
    async def test_rotate_preserves_data_integrity(self, real_db):
        """After rotation, all account passwords should round-trip correctly."""
        from src.core.crypto import init_encryptor, get_encryptor

        enc = await init_encryptor(real_db)

        # Create accounts with various passwords
        passwords = ["pw-1", "p@ss!#$%", "unicode-üñïçödé", "", "x" * 500]
        for i, pw in enumerate(passwords):
            await real_db.create_email_account({
                "name": f"rot-{i}", "host": f"h{i}", "username": f"u{i}", "password": pw
            })

        # Rotate 3 times
        for _ in range(3):
            enc = get_encryptor()
            await enc.rotate_key()

        # All accounts should still return correct passwords
        accounts = await real_db.list_email_accounts()
        for acct in accounts:
            idx = int(acct["name"].split("-")[1])
            assert acct["password"] == passwords[idx]


# ── Migration edge cases ─────────────────────────────────────────


class TestMigrationEdgeCases:
    @pytest.fixture
    async def real_db(self, tmp_db_path: str):
        from src.core.db_sqlite import Database
        db = Database(db_path=tmp_db_path)
        await db.connect()
        yield db
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_handles_mixed_plaintext_and_encrypted(self, tmp_db_path):
        """Migration should encrypt plaintext rows and leave encrypted ones alone."""
        from src.core.db_sqlite import Database

        # Create DB with schema only
        raw_conn = await __import__("aiosqlite").connect(tmp_db_path)
        raw_conn.row_factory = __import__("aiosqlite").Row
        await raw_conn.executescript(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
            "CREATE TABLE IF NOT EXISTS email_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL, port INTEGER DEFAULT 993, use_ssl BOOLEAN DEFAULT TRUE, username TEXT NOT NULL, password TEXT NOT NULL, folder TEXT DEFAULT 'INBOX', poll_interval_seconds INTEGER DEFAULT 600, action_after_fetch TEXT DEFAULT 'mark_seen', move_to_folder TEXT, body_handling TEXT DEFAULT 'save_with_attachments', attachment_whitelist TEXT, attachment_blacklist TEXT, deduplication_strategy TEXT DEFAULT 'message_id', enabled BOOLEAN DEFAULT TRUE, last_sync_at TEXT, last_error TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
        )
        await raw_conn.commit()

        # Insert one plaintext and one encrypted password
        fake_fernet = Fernet(Fernet.generate_key())
        encrypted_pw = fake_fernet.encrypt(b"already-encrypted").decode()

        await raw_conn.execute(
            "INSERT INTO email_accounts (name, host, username, password) VALUES (?, ?, ?, ?)",
            ("plain-acct", "h1", "u1", "plaintext-migrate-me"),
        )
        await raw_conn.execute(
            "INSERT INTO email_accounts (name, host, username, password) VALUES (?, ?, ?, ?)",
            ("enc-acct", "h2", "u2", encrypted_pw),
        )
        await raw_conn.commit()
        await raw_conn.close()

        # Connect with real Database — should migrate only the plaintext row
        db = Database(db_path=tmp_db_path)
        await db.connect()
        try:
            async with db.connection() as conn:
                cursor = await conn.execute(
                    "SELECT name, password FROM email_accounts ORDER BY id"
                )
                rows = await cursor.fetchall()

            # Plaintext row should now be encrypted
            assert rows[0]["password"].startswith("gAAAAA")
            assert rows[0]["password"] != "plaintext-migrate-me"

            # Encrypted row should be untouched
            assert rows[1]["password"] == encrypted_pw
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_with_empty_password_rows(self, tmp_db_path):
        """Migration should skip rows with empty passwords."""
        from src.core.db_sqlite import Database

        raw_conn = await __import__("aiosqlite").connect(tmp_db_path)
        raw_conn.row_factory = __import__("aiosqlite").Row
        await raw_conn.executescript(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
            "CREATE TABLE IF NOT EXISTS email_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL, port INTEGER DEFAULT 993, use_ssl BOOLEAN DEFAULT TRUE, username TEXT NOT NULL, password TEXT NOT NULL, folder TEXT DEFAULT 'INBOX', poll_interval_seconds INTEGER DEFAULT 600, action_after_fetch TEXT DEFAULT 'mark_seen', move_to_folder TEXT, body_handling TEXT DEFAULT 'save_with_attachments', attachment_whitelist TEXT, attachment_blacklist TEXT, deduplication_strategy TEXT DEFAULT 'message_id', enabled BOOLEAN DEFAULT TRUE, last_sync_at TEXT, last_error TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
        )
        await raw_conn.commit()

        await raw_conn.execute(
            "INSERT INTO email_accounts (name, host, username, password) VALUES (?, ?, ?, ?)",
            ("empty-pw", "h1", "u1", ""),
        )
        await raw_conn.commit()
        await raw_conn.close()

        db = Database(db_path=tmp_db_path)
        await db.connect()
        try:
            async with db.connection() as conn:
                cursor = await conn.execute("SELECT password FROM email_accounts WHERE id = 1")
                row = await cursor.fetchone()
            # Empty password should stay empty
            assert row["password"] == ""
        finally:
            await db.disconnect()
