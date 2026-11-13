"""API tokens: how one is made, how it is stored, and how it is checked.

The simplest honest identity mechanism for a proof of concept, using only the
standard library. It is **not** SSO, OIDC, OAuth or a session system, and it is
not production-grade: there is no expiry, no rotation endpoint, no rate limit on
failed attempts and no transport security of its own (run it behind TLS).

A token looks like ``cca_<token_id>_<secret>``:

- ``token_id`` is 16 hex characters, stored in clear as a lookup key. Knowing
  it proves nothing.
- ``secret`` is ``secrets.token_urlsafe(32)`` — 256 bits from the OS's CSPRNG.
  It is never stored. What is stored is PBKDF2-HMAC-SHA256 of it under a
  per-user random salt, and a presented secret is compared with
  ``hmac.compare_digest``.

The token is shown once, when the user is created, and cannot be recovered;
a lost token means creating the user again (and deactivating the old one).

This module imports nothing from the package.
"""

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "cca"
KDF_NAME = "pbkdf2_sha256"
# A random 256-bit secret does not need key stretching the way a password
# does; the iteration count is kept moderate so checking a token costs tens of
# milliseconds per request rather than hundreds. Stored per user, so it can be
# raised later without invalidating anyone.
KDF_ITERATIONS = 100_000
SALT_BYTES = 16
SECRET_BYTES = 32

# The roles a user can hold. Which role each endpoint needs, and what each role
# includes, is `auth.py`'s one table; this is only the vocabulary.
ROLES: tuple[str, ...] = ("estimator", "reviewer", "admin")


def new_token() -> tuple[str, str, str]:
    """A fresh token, as ``(token, token_id, secret)``."""
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return f"{TOKEN_PREFIX}_{token_id}_{secret}", token_id, secret


def new_salt() -> str:
    return secrets.token_hex(SALT_BYTES)


def hash_secret(secret: str, salt: str, iterations: int = KDF_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()


def parse_token(token: str) -> tuple[str, str] | None:
    """Split a presented token into ``(token_id, secret)``, or None if malformed.

    The secret is URL-safe base64 and may itself contain ``_``, so only the first
    two separators are split on.
    """
    parts = token.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    token_id, secret = parts[1], parts[2]
    if len(token_id) != 16 or not secret:
        return None
    try:
        bytes.fromhex(token_id)
    except ValueError:
        return None
    return token_id, secret


def secret_matches(secret: str, salt: str, iterations: int, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret, salt, iterations), expected_hash)


def burn_equivalent_time(secret: str) -> None:
    """Hash once and discard, so an unknown token id costs what a wrong secret does."""
    hash_secret(secret, "00" * SALT_BYTES)
