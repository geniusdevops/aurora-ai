"""IBM Cloud credential storage.

All of an org's IBM accounts live in a single Vault secret for provider
``ibm`` (``user_tokens`` is unique per ``(org_id, provider)``), shaped as::

    {"accounts": {"<account_id>": {"api_key": "...", "read_only_api_key": "...",
                                   "service_id": "...", "default_region": "us-south",
                                   "resource_group": "..."}}}

Adding or removing an account is a read-modify-write of that JSON, so writes
are serialised per org with a Postgres advisory lock. Reads and writes go
through the org-resolved ``get_token_data`` / ``store_tokens_in_db`` helpers.
"""

import hashlib
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.db_utils import connect_to_db_as_admin
from utils.log_sanitizer import hash_for_log
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

PROVIDER = "ibm"


class IBMReadOnlyCredentialMissing(Exception):
    """Ask mode needs a read-only API key and none is stored for the account."""

    def __init__(self, account_id: str):
        super().__init__(
            f"Ask mode requires a read-only IBM Cloud API key for account {account_id}; "
            "add one on the IBM Cloud connector page."
        )
        self.account_id = account_id


def _lock_key(user_id: str) -> int:
    try:
        from utils.auth.stateless_auth import resolve_org_id
        scope = resolve_org_id(user_id) or user_id
    except Exception:
        scope = user_id
    digest = hashlib.sha256(f"ibm:creds:{scope}".encode()).digest()[:7]
    return int.from_bytes(digest, byteorder="big", signed=False) & 0x7FFFFFFFFFFFFFFF


@contextmanager
def _org_credentials_lock(user_id: str) -> Iterator[None]:
    """Serialise read-modify-write of the org's IBM secret.

    Session-level lock on a dedicated connection: the Vault write happens in
    ``store_tokens_in_db`` on its own connection, so a transaction-scoped lock
    would not cover it.
    """
    key = _lock_key(user_id)
    conn = connect_to_db_as_admin()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        conn.close()


def load_ibm_accounts(user_id: str) -> Dict[str, Dict[str, Any]]:
    """Return ``{account_id: entry}`` for the org's stored IBM accounts."""
    data = get_token_data(user_id, PROVIDER) or {}
    accounts = data.get("accounts")
    return dict(accounts) if isinstance(accounts, dict) else {}


def get_ibm_account_credentials(
    user_id: str, account_id: str, read_only: bool = False
) -> Optional[Dict[str, Any]]:
    """Return the credentials to use for one account, or None if not stored.

    With ``read_only=True`` the read-only key is returned as ``api_key``; if
    the account has none, ``IBMReadOnlyCredentialMissing`` is raised. Ask mode
    never falls back to the write key.
    """
    entry = load_ibm_accounts(user_id).get(account_id)
    if not entry:
        return None
    creds = dict(entry)
    if read_only:
        ro_key = entry.get("read_only_api_key")
        if not ro_key:
            raise IBMReadOnlyCredentialMissing(account_id)
        creds["api_key"] = ro_key
    creds["account_id"] = account_id
    return creds


def upsert_ibm_account(user_id: str, account_id: str, entry: Dict[str, Any]) -> None:
    """Add or replace one account in the org's IBM secret."""
    clean = {k: v for k, v in entry.items() if v not in (None, "")}
    with _org_credentials_lock(user_id):
        accounts = load_ibm_accounts(user_id)
        accounts[account_id] = clean
        store_tokens_in_db(user_id, {"accounts": accounts}, PROVIDER)
    logger.info("[IBM-CREDS] Stored account=%s (total=%d)", hash_for_log(account_id), len(accounts))


def remove_ibm_account(user_id: str, account_id: str) -> Optional[Dict[str, Any]]:
    """Remove one account; deletes the secret when it was the last one.

    Returns the removed entry (so callers can invalidate token caches), or
    None if the account was not stored.
    """
    with _org_credentials_lock(user_id):
        accounts = load_ibm_accounts(user_id)
        removed = accounts.pop(account_id, None)
        if removed is None:
            return None
        if accounts:
            store_tokens_in_db(user_id, {"accounts": accounts}, PROVIDER)
        else:
            delete_user_secret(user_id, PROVIDER)
    logger.info("[IBM-CREDS] Removed account=%s (remaining=%d)", hash_for_log(account_id), len(accounts))
    return removed
