"""
IBM Cloud API routes - connect, multi-account management, status, disconnect.

Credentials (Service ID API keys) are stored in Vault via utils.cloud.ibm_credentials;
user_connections holds one row per IBM account. API keys are never logged or returned.
"""

import logging
import re

from flask import jsonify, request

from connectors.ibm_connector.client import (
    IBMAuthError,
    IBMError,
    get_account_info,
    get_iam_token,
    global_search,
    list_resource_groups,
    list_vpc_regions,
)
from routes.ibm import ibm_bp
from routes.ibm.helpers import disconnect_all_ibm, remove_ibm_account_connection
from utils.auth.rbac_decorators import require_permission
from utils.cloud.ibm_credentials import PROVIDER, load_ibm_accounts, upsert_ibm_account
from utils.db.connection_utils import get_all_user_connections, save_connection_metadata
from utils.log_sanitizer import hash_for_log
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)

API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]{2,8}$")
ACCOUNT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
RESOURCE_GROUP_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def _auth_error_response(e: IBMAuthError, which: str = "API key"):
    logger.warning("IBM IAM rejected %s (code=%s status=%s)", which, e.code, e.status)
    return jsonify({"error": f"IBM Cloud rejected the {which}. Check that it is valid and not deleted."}), 401


def _account_summaries(user_id: str):
    """Merge stored Vault entries with active user_connections rows."""
    entries = load_ibm_accounts(user_id)
    rows = {c["account_id"]: c for c in get_all_user_connections(user_id, PROVIDER)}
    summaries = []
    for account_id in sorted(set(entries) & set(rows)):
        entry = entries[account_id]
        summaries.append({
            "accountId": account_id,
            "serviceId": entry.get("service_id"),
            "defaultRegion": entry.get("default_region") or rows[account_id].get("region"),
            "resourceGroup": entry.get("resource_group"),
            "hasReadOnlyKey": bool(entry.get("read_only_api_key")),
        })
    return summaries


@ibm_bp.route("/ibm/connect", methods=["POST"])
@limiter.limit("5 per minute")
@require_permission("connectors", "write")
def ibm_connect(user_id):
    """Validate an IBM Cloud API key and add (or update) its account.

    Body: {apiKey, readOnlyApiKey?, defaultRegion, resourceGroup?}
    Calling again with another account's key adds that account.
    """
    data = request.get_json(silent=True) or {}
    api_key = (data.get("apiKey") or data.get("api_key") or "").strip()
    ro_key = (data.get("readOnlyApiKey") or data.get("read_only_api_key") or "").strip()
    region = (data.get("defaultRegion") or data.get("default_region") or "us-south").strip()
    resource_group = (data.get("resourceGroup") or data.get("resource_group") or "").strip()

    if not API_KEY_PATTERN.match(api_key):
        return jsonify({"error": "A valid IBM Cloud API key is required"}), 400
    if ro_key and not API_KEY_PATTERN.match(ro_key):
        return jsonify({"error": "Invalid read-only API key format"}), 400
    if ro_key and ro_key == api_key:
        return jsonify({"error": "The read-only API key must be different from the main API key"}), 400
    if not REGION_PATTERN.match(region):
        return jsonify({"error": "Invalid region"}), 400
    if resource_group and not RESOURCE_GROUP_PATTERN.match(resource_group):
        return jsonify({"error": "Invalid resource group id"}), 400

    try:
        token = get_iam_token(api_key)
        info = get_account_info(api_key, token)
    except IBMAuthError as e:
        return _auth_error_response(e)
    except IBMError as e:
        logger.warning("IBM connect failed validating key: %s", e)
        return jsonify({"error": "Could not reach IBM Cloud to validate the API key"}), 502

    account_id = info["account_id"]

    if ro_key:
        try:
            ro_info = get_account_info(ro_key, get_iam_token(ro_key))
        except IBMAuthError as e:
            return _auth_error_response(e, "read-only API key")
        except IBMError as e:
            logger.warning("IBM connect failed validating read-only key: %s", e)
            return jsonify({"error": "Could not reach IBM Cloud to validate the read-only API key"}), 502
        if ro_info["account_id"] != account_id:
            return jsonify({"error": "The read-only API key belongs to a different IBM Cloud account"}), 400

    try:
        global_search(token, fields=["crn"], limit=1, max_items=1)
    except IBMAuthError as e:
        logger.warning("IBM connect sanity read denied (code=%s)", e.code)
        return jsonify({
            "error": "The API key is valid but cannot read resources. Grant the Service ID "
                     "Viewer and Reader access on All Account Management and IAM-enabled services."
        }), 403
    except IBMError as e:
        logger.warning("IBM connect sanity read failed: %s", e)
        return jsonify({"error": "Could not list resources with the API key"}), 502

    try:
        upsert_ibm_account(user_id, account_id, {
            "api_key": api_key,
            "read_only_api_key": ro_key or None,
            "service_id": info.get("iam_id"),
            "default_region": region,
            "resource_group": resource_group or None,
        })
    except Exception as e:
        logger.error("Failed to store IBM credentials: %s", type(e).__name__)
        return jsonify({"error": "Failed to store IBM Cloud credentials"}), 500

    if not save_connection_metadata(user_id, PROVIDER, account_id,
                                    connection_method="api_key", region=region):
        return jsonify({"error": "Failed to save IBM Cloud connection"}), 500

    logger.info("IBM account connected account=%s read_only_key=%s", hash_for_log(account_id), bool(ro_key))
    return jsonify({
        "success": True,
        "accountId": account_id,
        "hasReadOnlyKey": bool(ro_key),
    })


@ibm_bp.route("/ibm/accounts", methods=["GET"])
@limiter.limit("60 per minute")
@require_permission("connectors", "read")
def ibm_accounts(user_id):
    """List connected IBM accounts (no secrets)."""
    try:
        return jsonify({"accounts": _account_summaries(user_id)})
    except Exception as e:
        logger.error("Failed to list IBM accounts: %s", e)
        return jsonify({"error": "Failed to list IBM Cloud accounts"}), 500


@ibm_bp.route("/ibm/accounts/<account_id>", methods=["DELETE"])
@limiter.limit("10 per minute")
@require_permission("connectors", "write")
def ibm_remove_account(user_id, account_id):
    """Remove a single IBM account."""
    if not ACCOUNT_ID_PATTERN.match(account_id):
        return jsonify({"error": "Invalid account id"}), 400
    try:
        if not remove_ibm_account_connection(user_id, account_id):
            return jsonify({"error": "Account not found"}), 404
    except Exception as e:
        logger.error("Failed to remove IBM account: %s", e)
        return jsonify({"error": "Failed to remove IBM Cloud account"}), 500
    return jsonify({"success": True})


@ibm_bp.route("/ibm/status", methods=["GET"])
@limiter.limit("60 per minute")
@require_permission("connectors", "read")
def ibm_status(user_id):
    """Connection status. Validates one account's key against IAM (cached token)."""
    try:
        accounts = _account_summaries(user_id)
    except Exception as e:
        logger.error("Failed to read IBM status: %s", e)
        return jsonify({"connected": False, "provider": PROVIDER}), 200
    if not accounts:
        return jsonify({"connected": False, "provider": PROVIDER, "accounts": []})

    entries = load_ibm_accounts(user_id)
    first = entries.get(accounts[0]["accountId"]) or {}
    try:
        get_iam_token(first.get("api_key", ""))
    except IBMAuthError:
        return jsonify({"connected": False, "provider": PROVIDER, "accounts": accounts,
                        "error": "Stored API key was rejected by IBM Cloud"})
    except IBMError as e:
        # Network/IAM outage: keep reporting the stored connection.
        logger.warning("IBM status check could not reach IAM: %s", e)

    return jsonify({"connected": True, "provider": PROVIDER, "accounts": accounts})


@ibm_bp.route("/ibm/disconnect", methods=["POST"])
@limiter.limit("10 per minute")
@require_permission("connectors", "write")
def ibm_disconnect(user_id):
    """Disconnect all IBM accounts."""
    try:
        ok = disconnect_all_ibm(user_id)
    except Exception as e:
        logger.error("Error disconnecting IBM Cloud: %s", e)
        return jsonify({"error": "Failed to disconnect IBM Cloud"}), 500
    if not ok:
        return jsonify({"success": False, "error": "IBM Cloud was only partially disconnected"}), 500
    return jsonify({"success": True, "message": "IBM Cloud disconnected"})


def _token_for_account(user_id: str, account_id: str):
    entry = load_ibm_accounts(user_id).get(account_id)
    if not entry:
        return None
    return get_iam_token(entry["api_key"])


@ibm_bp.route("/ibm/regions", methods=["GET"])
@limiter.limit("30 per minute")
@require_permission("connectors", "read")
def ibm_regions(user_id):
    """List VPC regions. Uses ?account_id= or the first connected account."""
    account_id = request.args.get("account_id", "")
    try:
        if not account_id:
            accounts = load_ibm_accounts(user_id)
            if not accounts:
                return jsonify({"error": "IBM Cloud not connected", "action": "CONNECT_REQUIRED"}), 401
            account_id = sorted(accounts)[0]
        elif not ACCOUNT_ID_PATTERN.match(account_id):
            return jsonify({"error": "Invalid account id"}), 400
        token = _token_for_account(user_id, account_id)
        if not token:
            return jsonify({"error": "Account not found"}), 404
        return jsonify({"regions": list_vpc_regions(token)})
    except IBMAuthError as e:
        return _auth_error_response(e)
    except IBMError as e:
        logger.warning("IBM region listing failed: %s", e)
        return jsonify({"error": "Failed to list IBM Cloud regions"}), 502


@ibm_bp.route("/ibm/resource-groups", methods=["GET"])
@limiter.limit("30 per minute")
@require_permission("connectors", "read")
def ibm_resource_groups(user_id):
    """List resource groups for ?account_id=."""
    account_id = request.args.get("account_id", "")
    if not ACCOUNT_ID_PATTERN.match(account_id):
        return jsonify({"error": "account_id is required"}), 400
    try:
        token = _token_for_account(user_id, account_id)
        if not token:
            return jsonify({"error": "Account not found"}), 404
        return jsonify({"resourceGroups": list_resource_groups(token, account_id)})
    except IBMAuthError as e:
        return _auth_error_response(e)
    except IBMError as e:
        logger.warning("IBM resource group listing failed: %s", e)
        return jsonify({"error": "Failed to list resource groups"}), 502
