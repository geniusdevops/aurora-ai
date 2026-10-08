"""
IBM Cloud REST client.

Uses plain ``requests`` rather than the IBM SDKs, to avoid adding SDK
dependencies to the server image. Provides:

- IAM API-key -> access-token exchange, cached in-process per key hash
- account/Service ID lookup for an API key
- Global Search (paged, fails closed on a repeated cursor or page cap)
- VPC region and resource-group listings for the onboarding dropdowns

API keys are never logged; cache keys are SHA-256 hashes of the key.
"""

import hashlib
import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
IAM_APIKEY_DETAILS_URL = "https://iam.cloud.ibm.com/v1/apikeys/details"
GLOBAL_SEARCH_URL = "https://api.global-search-tagging.cloud.ibm.com/v3/resources/search"
RESOURCE_GROUPS_URL = "https://resource-controller.cloud.ibm.com/v2/resource_groups"
VPC_API_VERSION = "2024-11-12"

HTTP_TIMEOUT = (5, 20)
# Refresh tokens this long before IAM's stated expiry.
_TOKEN_REFRESH_MARGIN_S = 300
# Upper bound on Global Search pages; hitting it fails closed rather than
# returning a silently truncated inventory.
GLOBAL_SEARCH_MAX_PAGES = 200


class IBMError(Exception):
    """Base error for IBM Cloud API calls."""


class IBMAuthError(IBMError):
    """IAM rejected the API key or token.

    ``code`` carries the IAM error code (e.g. ``BXNIM0415E``) when present.
    """

    def __init__(self, message: str, code: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class IBMAPIError(IBMError):
    """Non-auth failure from an IBM Cloud API."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class IBMSearchTruncatedError(IBMError):
    """Global Search paging could not complete (repeated cursor or page cap)."""


# ---------------------------------------------------------------------------
# IAM token cache
# ---------------------------------------------------------------------------

_token_cache: Dict[str, Dict[str, Any]] = {}
# structure: {sha256(api_key): {"access_token": str, "expires_at": float}}
_token_lock = threading.Lock()


def api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def invalidate_cached_ibm_tokens(api_keys: Iterable[str]) -> None:
    """Drop cached IAM tokens for the given API keys (e.g. on disconnect)."""
    with _token_lock:
        for key in api_keys:
            if key:
                _token_cache.pop(api_key_hash(key), None)


def _auth_error_from_response(resp: requests.Response, default: str) -> IBMAuthError:
    code = None
    message = default
    try:
        body = resp.json()
        code = body.get("errorCode") or body.get("code")
        message = body.get("errorMessage") or body.get("message") or default
    except ValueError:
        pass
    return IBMAuthError(message, code=code, status=resp.status_code)


def get_iam_token(api_key: str) -> str:
    """Exchange an IBM Cloud API key for an IAM access token (cached)."""
    if not api_key:
        raise IBMAuthError("API key is required")

    cache_key = api_key_hash(api_key)
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached["expires_at"] - _TOKEN_REFRESH_MARGIN_S > now:
            return cached["access_token"]

    try:
        resp = requests.post(
            IAM_TOKEN_URL,
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": api_key,
            },
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise IBMAPIError(f"IAM token request failed: {type(e).__name__}") from e

    if resp.status_code in (400, 401, 403):
        raise _auth_error_from_response(resp, "IBM Cloud rejected the API key")
    if not resp.ok:
        raise IBMAPIError(f"IAM token request failed with status {resp.status_code}", status=resp.status_code)

    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise IBMAuthError("IAM response did not include an access token")

    expires_at = body.get("expiration")
    if not isinstance(expires_at, (int, float)):
        expires_at = now + float(body.get("expires_in") or 3600)

    with _token_lock:
        _token_cache[cache_key] = {"access_token": access_token, "expires_at": float(expires_at)}
    return access_token


def _get(url: str, token: str, *, params: Optional[Dict[str, Any]] = None,
         headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    all_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if headers:
        all_headers.update(headers)
    try:
        resp = requests.get(url, params=params, headers=all_headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise IBMAPIError(f"GET {url} failed: {type(e).__name__}") from e
    if resp.status_code in (401, 403):
        raise _auth_error_from_response(resp, "IBM Cloud denied the request")
    if not resp.ok:
        raise IBMAPIError(f"GET {url} failed with status {resp.status_code}", status=resp.status_code)
    return resp.json()


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def get_account_info(api_key: str, token: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Return ``{account_id, iam_id, name}`` for the identity behind an API key."""
    token = token or get_iam_token(api_key)
    body = _get(IAM_APIKEY_DETAILS_URL, token, headers={"IAM-Apikey": api_key})
    account_id = body.get("account_id")
    if not account_id:
        raise IBMAPIError("API key details did not include an account id")
    return {
        "account_id": account_id,
        "iam_id": body.get("iam_id"),
        "name": body.get("name"),
    }


# ---------------------------------------------------------------------------
# Global Search
# ---------------------------------------------------------------------------

def global_search(
    token: str,
    query: str = "*",
    fields: Optional[List[str]] = None,
    *,
    limit: int = 1000,
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Run a Global Search query, following ``search_cursor`` until exhausted.

    Raises ``IBMSearchTruncatedError`` if the cursor repeats or the page cap is
    hit, so callers never treat a partial inventory as complete. ``max_items``
    stops early on purpose (e.g. a connectivity probe) and is not an error.
    """
    items: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_cursors = set()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}

    for _ in range(GLOBAL_SEARCH_MAX_PAGES):
        body: Dict[str, Any] = {"query": query}
        if fields:
            body["fields"] = fields
        if cursor:
            body["search_cursor"] = cursor
        try:
            resp = requests.post(GLOBAL_SEARCH_URL, params={"limit": limit}, json=body,
                                 headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise IBMAPIError(f"Global Search request failed: {type(e).__name__}") from e
        if resp.status_code in (401, 403):
            raise _auth_error_from_response(resp, "IBM Cloud denied the Global Search request")
        if not resp.ok:
            raise IBMAPIError(f"Global Search failed with status {resp.status_code}", status=resp.status_code)

        page = resp.json()
        items.extend(page.get("items") or [])
        if max_items is not None and len(items) >= max_items:
            return items[:max_items]

        cursor = page.get("search_cursor")
        if not cursor or not page.get("items"):
            return items
        if cursor in seen_cursors:
            raise IBMSearchTruncatedError("Global Search returned a repeated cursor")
        seen_cursors.add(cursor)

    raise IBMSearchTruncatedError(
        f"Global Search exceeded {GLOBAL_SEARCH_MAX_PAGES} pages"
    )


# ---------------------------------------------------------------------------
# Onboarding helpers
# ---------------------------------------------------------------------------

def list_vpc_regions(token: str, base_region: str = "us-south") -> List[Dict[str, str]]:
    """List VPC regions available to the account."""
    body = _get(
        f"https://{base_region}.iaas.cloud.ibm.com/v1/regions",
        token,
        params={"version": VPC_API_VERSION, "generation": 2},
    )
    return [
        {"name": r.get("name"), "status": r.get("status")}
        for r in body.get("regions") or []
        if r.get("name")
    ]


def list_resource_groups(token: str, account_id: str) -> List[Dict[str, Any]]:
    """List resource groups in an account."""
    body = _get(RESOURCE_GROUPS_URL, token, params={"account_id": account_id})
    return [
        {"id": g.get("id"), "name": g.get("name"), "default": bool(g.get("default"))}
        for g in body.get("resources") or []
        if g.get("id")
    ]
