"""IBM Cloud connection teardown, shared by the IBM routes and the generic
``DELETE /api/connected-accounts/<user>/ibm`` path."""

import logging

from connectors.ibm_connector.client import invalidate_cached_ibm_tokens
from utils.cloud.ibm_credentials import PROVIDER, load_ibm_accounts, remove_ibm_account
from utils.db.connection_utils import delete_connection_secret, get_all_user_connections
from utils.log_sanitizer import hash_for_log
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)


def _invalidate_tokens(entries) -> None:
    keys = []
    for entry in entries:
        keys.extend([entry.get("api_key"), entry.get("read_only_api_key")])
    invalidate_cached_ibm_tokens(k for k in keys if k)


def remove_ibm_account_connection(user_id: str, account_id: str) -> bool:
    """Remove one IBM account: Vault entry, connection row, token cache and graph nodes.

    Returns False when the account was neither stored nor connected.
    """
    removed = remove_ibm_account(user_id, account_id)
    if removed:
        _invalidate_tokens([removed])
    row_ok = delete_connection_secret(user_id, PROVIDER, account_id)

    try:
        from services.graph.memgraph_client import get_memgraph_client
        get_memgraph_client().delete_services_for_account(user_id, PROVIDER, account_id)
    except Exception as e:
        logger.warning("Failed to delete Memgraph nodes for ibm account=%s: %s", hash_for_log(account_id), e)

    return bool(removed) or row_ok


def disconnect_all_ibm(user_id: str, entries=None) -> bool:
    """Disconnect every IBM account for the org. Returns False on a partial failure.

    ``entries`` are the stored account entries, for callers that already
    deleted the Vault secret and loaded them beforehand.
    """
    ok = True
    try:
        if entries is None:
            entries = load_ibm_accounts(user_id).values()
        _invalidate_tokens(entries)
    except Exception as e:
        logger.warning("Failed to load IBM accounts for token invalidation: %s", e)

    secret_ok, _ = delete_user_secret(user_id, PROVIDER)
    ok = ok and secret_ok

    try:
        # raise_on_error: a swallowed DB error would look like "no accounts" and
        # report success with rows still active for fan-out and discovery.
        connections = get_all_user_connections(user_id, PROVIDER, raise_on_error=True)
    except Exception as e:
        logger.warning("Failed to list IBM accounts for disconnect: %s", e)
        connections = []
        ok = False
    for conn in connections:
        try:
            row_ok = delete_connection_secret(user_id, PROVIDER, conn["account_id"])
        except Exception as e:
            logger.warning("Failed to deactivate IBM account=%s: %s", hash_for_log(conn["account_id"]), e)
            row_ok = False
        ok = ok and row_ok

    try:
        from services.graph.memgraph_client import get_memgraph_client
        get_memgraph_client().delete_services_for_provider(user_id, PROVIDER)
    except Exception as e:
        logger.warning("Failed to delete Memgraph nodes for provider=ibm: %s", e)

    return ok
