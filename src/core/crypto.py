"""Fernet-based encryption for email account credentials.

Provides symmetric encryption (AES-128-CBC + HMAC) for at-rest protection
of email account passwords stored in the SQLite database.

Key management strategy:
  1. If ``DOCMIND_EMAIL_ENCRYPTION_KEY`` env var is set, use that key.
  2. Otherwise, generate (or retrieve) a key stored in the ``settings``
     table under ``email_encryption_key``.
  3. The key is generated once on first use and persisted in the DB so
     that restarts don't invalidate existing encrypted passwords.

The key can also be rotated: provide a new key via env var or
``rotate_key()``, and all existing passwords will be re-encrypted.

This module is designed to be self-contained — it does not import other
DocMind modules to avoid circular dependencies. The ``Database`` instance
is passed in for DB-backed key storage.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

if TYPE_CHECKING:
    from .db_sqlite import Database

logger = logging.getLogger(__name__)

# Setting key used to persist the Fernet key in the DB settings table.
_ENCRYPTION_KEY_SETTING = "email_encryption_key"

# Env var for externally-provided key (e.g., from a secret manager).
_ENCRYPTION_KEY_ENV = "DOCMIND_EMAIL_ENCRYPTION_KEY"

# Prefix that all Fernet tokens have (base64-encoded, starts with 'gAAAAA').
# Used to distinguish encrypted values from plaintext during migration.
_FERNET_PREFIX = "gAAAAA"


class CryptoError(Exception):
    """Raised when encryption/decryption fails."""


def _is_encrypted(value: str) -> bool:
    """Heuristic: does this value look like a Fernet token?

    Fernet tokens are base64-urlsafe encoded and always start with the
    version byte (0x80) which encodes as 'g' in the URL-safe alphabet,
    followed by a timestamp — producing the recognizable 'gAAAAA' prefix.

    This is used during migration to skip already-encrypted passwords.
    """
    if not value:
        return False
    return value.startswith(_FERNET_PREFIX)


class CredentialEncryptor:
    """Encrypts and decrypts email account passwords using Fernet.

    Usage:
        encryptor = CredentialEncryptor(db)
        await encryptor.init()  # load or generate key
        encrypted = encryptor.encrypt("secret123")
        decrypted = encryptor.decrypt(encrypted)
    """

    def __init__(self, db: "Database") -> None:
        self._db = db
        self._fernet: Optional[Fernet] = None
        self._multi: Optional[MultiFernet] = None
        self._key_source: str = "unknown"

    @property
    def is_initialized(self) -> bool:
        return self._fernet is not None

    @property
    def key_source(self) -> str:
        """Where the active key came from: 'env', 'database', or 'unknown'."""
        return self._key_source

    async def init(self) -> None:
        """Load or generate the Fernet key.

        Priority:
        1. ``DOCMIND_EMAIL_ENCRYPTION_KEY`` env var (external secret)
        2. Key stored in the DB ``settings`` table
        3. Generate a new key and persist it to the DB
        """
        env_key = os.environ.get(_ENCRYPTION_KEY_ENV, "").strip()
        if env_key:
            try:
                self._fernet = Fernet(env_key.encode())
                self._key_source = "env"
                logger.info("Email encryption key loaded from %s", _ENCRYPTION_KEY_ENV)
            except Exception:
                logger.warning(
                    "%s is set but invalid — falling back to DB key", _ENCRYPTION_KEY_ENV
                )
                env_key = ""

        if self._fernet is None:
            db_key = await self._db.get_setting(_ENCRYPTION_KEY_SETTING)
            if db_key:
                try:
                    self._fernet = Fernet(db_key.encode())
                    self._key_source = "database"
                    logger.debug("Email encryption key loaded from DB settings")
                except Exception:
                    logger.warning(
                        "DB-stored email encryption key is invalid — regenerating"
                    )
                    db_key = None

            if db_key is None:
                new_key = Fernet.generate_key()
                await self._db.set_setting(
                    _ENCRYPTION_KEY_SETTING, new_key.decode()
                )
                self._fernet = Fernet(new_key)
                self._key_source = "database"
                logger.info("New email encryption key generated and stored in DB")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string, returning a Fernet token string.

        If the value already looks encrypted (has the Fernet token prefix),
        it is returned as-is to avoid double-encryption during updates.
        """
        if self._fernet is None:
            raise CryptoError("Encryptor not initialized — call init() first")
        if not plaintext:
            return plaintext
        if _is_encrypted(plaintext):
            return plaintext
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet token string back to plaintext.

        If the value doesn't look like a Fernet token (e.g., it's a
        plaintext password from before encryption was enabled), it is
        returned as-is for backward compatibility.

        Raises CryptoError if the token is a valid-looking Fernet token
        but cannot be decrypted (wrong key, corrupted data).
        """
        if self._fernet is None:
            raise CryptoError("Encryptor not initialized — call init() first")
        if not token:
            return token
        if not _is_encrypted(token):
            return token
        try:
            if self._multi is not None:
                return self._multi.decrypt(token.encode()).decode()
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise CryptoError(
                "Failed to decrypt email password — key may have changed"
            )

    def add_decryption_key(self, key: str) -> None:
        """Add an additional key for decryption fallback (key rotation).

        The primary key (used for new encryption) remains unchanged.
        MultiFernet tries the primary key first, then falls back.
        """
        if self._fernet is None:
            raise CryptoError("Encryptor not initialized — call init() first")
        try:
            old_fernet = Fernet(key.encode())
        except Exception:
            logger.warning("Invalid old encryption key — skipping")
            return
        if self._multi is None:
            self._multi = MultiFernet([self._fernet, old_fernet])
        else:
            keys = list(self._multi._fernets)  # type: ignore[attr-defined]
            if old_fernet not in keys:
                keys.append(old_fernet)
            self._multi = MultiFernet(keys)

    async def rotate_key(self, new_key: Optional[str] = None) -> None:
        """Rotate the encryption key and re-encrypt all stored passwords.

        Args:
            new_key: Optional new Fernet key string. If None, a new key
                     is generated. If provided, must be a valid Fernet key.

        This method:
        1. Saves the old key for MultiFernet fallback decryption
        2. Sets the new key as primary (for encryption)
        3. Persists the new key to the DB settings table
        4. Re-encrypts all email account passwords in the DB
        """
        if self._fernet is None:
            raise CryptoError("Encryptor not initialized — call init() first")

        # Save the old key string for fallback decryption.
        old_key_from_db = await self._db.get_setting(_ENCRYPTION_KEY_SETTING)
        old_key_from_env = os.environ.get(_ENCRYPTION_KEY_ENV, "").strip()
        old_key_str = old_key_from_env if old_key_from_env else old_key_from_db

        # Generate or set new key
        if new_key is None:
            new_key = Fernet.generate_key().decode()
        new_fernet = Fernet(new_key.encode())

        # Build MultiFernet: new key first (primary), old key as fallback.
        if old_key_str:
            try:
                old_fernet = Fernet(old_key_str.encode())
                self._multi = MultiFernet([new_fernet, old_fernet])
            except Exception:
                logger.warning("Old encryption key is invalid — no fallback")
                self._multi = None
        else:
            self._multi = None

        # Set new key as primary for encryption
        self._fernet = new_fernet
        self._key_source = "database"

        # Persist new key to DB
        await self._db.set_setting(_ENCRYPTION_KEY_SETTING, new_key)

        # Re-encrypt all passwords. We read raw (encrypted) passwords
        # directly from the DB to avoid the automatic decryption in
        # _row_to_email_account_dict, then decrypt and re-encrypt manually.
        async with self._db.connection() as conn:
            cursor = await conn.execute(
                "SELECT id, password FROM email_accounts WHERE password != ''"
            )
            rows = await cursor.fetchall()

        for row in rows:
            raw = row["password"]
            if not raw:
                continue
            try:
                plaintext = self.decrypt(raw)
                new_encrypted = self.encrypt(plaintext)
                if new_encrypted != raw:
                    await self._db.update_email_account_password(
                        row["id"], new_encrypted
                    )
            except CryptoError:
                logger.error(
                    "Failed to re-encrypt password for account id=%d during rotation",
                    row["id"],
                )

        logger.info("Email encryption key rotated successfully")


# Module-level singleton — initialized by the server on startup.
_encryptor: Optional[CredentialEncryptor] = None


async def init_encryptor(db: "Database") -> CredentialEncryptor:
    """Initialize and return the module-level encryptor singleton."""
    global _encryptor
    _encryptor = CredentialEncryptor(db)
    await _encryptor.init()
    return _encryptor


def get_encryptor() -> Optional[CredentialEncryptor]:
    """Get the initialized encryptor singleton, or None if not yet initialized."""
    return _encryptor


def encrypt_password(plaintext: str) -> str:
    """Convenience: encrypt a password using the module-level singleton."""
    enc = _encryptor
    if enc is None or not enc.is_initialized:
        return plaintext
    return enc.encrypt(plaintext)


def decrypt_password(token: str) -> str:
    """Convenience: decrypt a password using the module-level singleton.

    If the encryptor is not initialized, returns the value as-is
    (backward compatibility with plaintext storage).
    """
    enc = _encryptor
    if enc is None or not enc.is_initialized:
        return token
    return enc.decrypt(token)
