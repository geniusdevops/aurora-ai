"""Tests for the IBM Cloud connect / account routes.

Route functions are unwrapped past the limiter and RBAC decorators and called
inside a Flask request context, with IBM APIs and storage mocked.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from connectors.ibm_connector.client import IBMAuthError, IBMAPIError
from routes.ibm import ibm_routes as mod

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
KEY = "K" * 44
RO_KEY = "R" * 44

app = Flask(__name__)


def _call(fn, *args, method="POST", json=None, query_string=None):
    raw = inspect.unwrap(fn)
    with app.test_request_context(method=method, json=json, query_string=query_string):
        rv = raw("user-1", *args)
    resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
    return resp.get_json(), status


@pytest.fixture
def ibm_ok():
    """IAM, account lookup and Global Search all succeed; storage is mocked."""
    def account_for(api_key, token=None):
        return {"account_id": ACCOUNT_B if api_key == "OTHER" * 9 else ACCOUNT_A,
                "iam_id": "iam-ServiceId-1", "name": "aurora"}

    with patch.object(mod, "get_iam_token", return_value="tok") as tok, \
            patch.object(mod, "get_account_info", side_effect=account_for), \
            patch.object(mod, "global_search", return_value=[{"crn": "x"}]) as search, \
            patch.object(mod, "upsert_ibm_account") as upsert, \
            patch.object(mod, "save_connection_metadata", return_value=True) as save:
        yield {"token": tok, "search": search, "upsert": upsert, "save": save}


def test_connect_stores_account_and_active_connection(ibm_ok):
    body, status = _call(mod.ibm_connect, json={
        "apiKey": KEY, "readOnlyApiKey": RO_KEY, "defaultRegion": "eu-de",
    })
    assert status == 200
    assert body == {"success": True, "accountId": ACCOUNT_A, "hasReadOnlyKey": True}

    user, account, entry = ibm_ok["upsert"].call_args.args
    assert account == ACCOUNT_A
    assert entry["api_key"] == KEY and entry["read_only_api_key"] == RO_KEY
    assert entry["default_region"] == "eu-de"

    ibm_ok["save"].assert_called_once_with(
        "user-1", "ibm", ACCOUNT_A, connection_method="api_key", region="eu-de",
    )
    # Sanity read is a single-item probe.
    assert ibm_ok["search"].call_args.kwargs["max_items"] == 1


def test_connect_does_not_set_non_active_status(ibm_ok):
    # set_connection_status("connected") would hide the row from every
    # status='active' query; the route must not use it.
    assert not hasattr(mod, "set_connection_status")


def test_connect_never_returns_keys(ibm_ok):
    body, _ = _call(mod.ibm_connect, json={"apiKey": KEY, "readOnlyApiKey": RO_KEY})
    assert KEY not in str(body) and RO_KEY not in str(body)


@pytest.mark.parametrize("payload, message", [
    ({}, "valid IBM Cloud API key"),
    ({"apiKey": "short"}, "valid IBM Cloud API key"),
    ({"apiKey": KEY, "readOnlyApiKey": "bad key!"}, "read-only API key format"),
    ({"apiKey": KEY, "readOnlyApiKey": KEY}, "must be different"),
    ({"apiKey": KEY, "defaultRegion": "US South"}, "Invalid region"),
    ({"apiKey": KEY, "resourceGroup": "not-a-guid"}, "resource group"),
])
def test_connect_rejects_bad_input(ibm_ok, payload, message):
    body, status = _call(mod.ibm_connect, json=payload)
    assert status == 400
    assert message in body["error"]
    ibm_ok["upsert"].assert_not_called()


def test_connect_rejects_read_only_key_from_other_account(ibm_ok):
    body, status = _call(mod.ibm_connect, json={"apiKey": KEY, "readOnlyApiKey": "OTHER" * 9})
    assert status == 400
    assert "different IBM Cloud account" in body["error"]
    ibm_ok["upsert"].assert_not_called()


def test_connect_rejected_key_returns_401(ibm_ok):
    ibm_ok["token"].side_effect = IBMAuthError("nope", code="BXNIM0415E", status=400)
    body, status = _call(mod.ibm_connect, json={"apiKey": KEY})
    assert status == 401
    ibm_ok["upsert"].assert_not_called()


def test_connect_iam_outage_returns_502(ibm_ok):
    ibm_ok["token"].side_effect = IBMAPIError("down", status=503)
    _, status = _call(mod.ibm_connect, json={"apiKey": KEY})
    assert status == 502


def test_connect_key_without_read_access_returns_403(ibm_ok):
    ibm_ok["search"].side_effect = IBMAuthError("denied", status=403)
    body, status = _call(mod.ibm_connect, json={"apiKey": KEY})
    assert status == 403
    assert "Viewer" in body["error"]
    ibm_ok["upsert"].assert_not_called()


def test_connect_fails_when_connection_row_not_saved(ibm_ok):
    ibm_ok["save"].return_value = False
    _, status = _call(mod.ibm_connect, json={"apiKey": KEY})
    assert status == 500


# --- accounts / status -------------------------------------------------------

def _stored(entries, rows):
    return patch.multiple(
        mod,
        load_ibm_accounts=MagicMock(return_value=entries),
        get_all_user_connections=MagicMock(return_value=[{"account_id": a, "region": "us-south"} for a in rows]),
    )


def test_accounts_lists_only_stored_and_active_accounts():
    entries = {
        ACCOUNT_A: {"api_key": KEY, "read_only_api_key": RO_KEY, "default_region": "eu-de"},
        ACCOUNT_B: {"api_key": KEY},
    }
    with _stored(entries, rows=[ACCOUNT_A]):
        body, status = _call(mod.ibm_accounts, method="GET")
    assert status == 200
    assert body["accounts"] == [{
        "accountId": ACCOUNT_A, "serviceId": None, "defaultRegion": "eu-de",
        "resourceGroup": None, "hasReadOnlyKey": True,
    }]
    assert KEY not in str(body)


def test_status_disconnected_when_no_accounts():
    with _stored({}, rows=[]):
        body, _ = _call(mod.ibm_status, method="GET")
    assert body["connected"] is False


def test_status_reports_rejected_key_as_disconnected():
    with _stored({ACCOUNT_A: {"api_key": KEY}}, rows=[ACCOUNT_A]), \
            patch.object(mod, "get_iam_token", side_effect=IBMAuthError("bad")):
        body, _ = _call(mod.ibm_status, method="GET")
    assert body["connected"] is False


def test_status_stays_connected_during_iam_outage():
    with _stored({ACCOUNT_A: {"api_key": KEY}}, rows=[ACCOUNT_A]), \
            patch.object(mod, "get_iam_token", side_effect=IBMAPIError("down")):
        body, _ = _call(mod.ibm_status, method="GET")
    assert body["connected"] is True


def test_remove_account_validates_id():
    _, status = _call(mod.ibm_remove_account, "not-an-id", method="DELETE")
    assert status == 400


def test_remove_unknown_account_returns_404():
    with patch.object(mod, "remove_ibm_account_connection", return_value=False):
        _, status = _call(mod.ibm_remove_account, ACCOUNT_A, method="DELETE")
    assert status == 404


def test_disconnect_reports_partial_failure():
    with patch.object(mod, "disconnect_all_ibm", return_value=False):
        body, status = _call(mod.ibm_disconnect)
    assert status == 500
    assert body["success"] is False


def test_resource_groups_requires_account_id():
    _, status = _call(mod.ibm_resource_groups, method="GET")
    assert status == 400
