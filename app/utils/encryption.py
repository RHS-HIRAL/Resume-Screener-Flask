"""
app/utils/encryption.py
Field-level AES encryption for sensitive candidate data.

Uses Fernet (AES-128-CBC + HMAC-SHA256).  Encrypted values are stored as
    enc:<base64-fernet-token>
so legacy plain-text rows are transparently handled: if a value does NOT
start with the prefix it is returned as-is (useful during migration).

Key: HR_ENCRYPTION_KEY in .env — a URL-safe base-64-encoded 32-byte key.
Generate:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("encryption")

# Sentinel prefix that marks an encrypted value in the database.
ENCRYPTED_PREFIX = "enc:"

# What non-authorized roles see instead of the real value.
MASK = "****"

# Roles allowed to decrypt
DECRYPT_ROLES = {"admin", "hr"}

_fernet_instance: Fernet | None = None
_fernet_initialized: bool = False


def _get_fernet() -> Fernet | None:
    """Return a cached Fernet instance, or None if the key is not configured."""
    global _fernet_instance, _fernet_initialized
    if _fernet_initialized:
        return _fernet_instance

    # Lazy import to avoid circular dependency at module load time.
    from config import Config

    key = (Config.HR_ENCRYPTION_KEY or "").strip()
    _fernet_initialized = True

    if not key:
        logger.warning(
            "[ENCRYPTION] HR_ENCRYPTION_KEY is not set in .env. "
            "Sensitive fields will be stored as plain text. "
            "Set the key and run the migration utility to encrypt existing rows."
        )
        _fernet_instance = None
        return None

    try:
        _fernet_instance = Fernet(key.encode())
        logger.info("[ENCRYPTION] Fernet key loaded successfully.")
        return _fernet_instance
    except Exception as exc:
        logger.error(
            "[ENCRYPTION] HR_ENCRYPTION_KEY is invalid: %s. "
            "Sensitive fields will be stored as plain text.",
            exc,
        )
        _fernet_instance = None
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def encrypt_field(value: str | None) -> str | None:
    """
    Encrypt *value* and return ``enc:<token>``.

    - Returns ``None`` / ``""`` unchanged (no-op for empty values).
    - Returns the value unchanged if no encryption key is configured
      (so the app still works in development without the key).
    - Idempotent: already-encrypted values are returned as-is.
    """
    if not value:
        return value

    # Already encrypted — do not double-encrypt.
    if isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX):
        return value

    fernet = _get_fernet()
    if fernet is None:
        return value  # Key not configured — store plain

    token = fernet.encrypt(str(value).encode()).decode()
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_field(value: str | None, user_role: str) -> str | None:
    """
    Decrypt *value* for *user_role*.

    - Roles in DECRYPT_ROLES receive the plain-text value.
    - All other roles receive ``MASK`` (``"****"``).
    - Legacy plain-text values (no enc: prefix) are returned as-is for all roles
      so that data written before encryption was enabled is still readable.
    - Empty/None values are returned unchanged for all roles.
    """
    if not value:
        return value

    # Not encrypted (legacy plain-text row) — everyone can see it.
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        # Apply the mask retroactively so the UI is consistent while a migration
        # runs, but only if there IS a key configured (else we haven't migrated yet).
        if _get_fernet() is not None and user_role not in DECRYPT_ROLES:
            return MASK
        return value

    # Encrypted — only authorised roles may decrypt.
    if user_role not in DECRYPT_ROLES:
        return MASK

    fernet = _get_fernet()
    if fernet is None:
        # Key disappeared after startup — fail safe.
        return MASK

    try:
        token = value[len(ENCRYPTED_PREFIX) :]
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error("[ENCRYPTION] InvalidToken — key mismatch or corrupted data.")
        return "[decryption error]"
    except Exception as exc:
        logger.error("[ENCRYPTION] Unexpected decryption error: %s", exc)
        return "[decryption error]"


def is_encrypted(value: str | None) -> bool:
    """Return True if *value* is stored in encrypted form."""
    return bool(value and isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX))


def apply_sensitive_mask(candidate_dict: dict, user_role: str) -> dict:
    """
    Decrypt or mask all SENSITIVE_FIELDS in *candidate_dict* in place.
    Returns the same dict for convenience.
    """
    from app.utils.role_access import SENSITIVE_FIELDS

    for field in SENSITIVE_FIELDS:
        if field in candidate_dict:
            candidate_dict[field] = decrypt_field(candidate_dict[field], user_role)

    return candidate_dict
