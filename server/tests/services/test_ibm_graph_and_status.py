"""IBM additions to the graph client and the unified connector status check."""

from unittest.mock import patch

import pytest

from services.graph.memgraph_client import MemgraphClient
from routes import connector_status


def test_service_row_carries_ibm_account_id():
    row = MemgraphClient._build_service_row("u1", "vpc-1", "ibm", {"ibm_account_id": "a" * 32})
    assert row["ibm_account_id"] == "a" * 32
    assert MemgraphClient._build_service_row("u1", "x", "aws", {})["ibm_account_id"] == ""


def test_delete_services_for_account_matches_provider_property():
    client = MemgraphClient.__new__(MemgraphClient)
    with patch.object(MemgraphClient, "_execute", return_value=[{"deleted": 3}]) as execute:
        assert client.delete_services_for_account("u1", "ibm", "acct") == 3
    query, params = execute.call_args.args
    assert "ibm_account_id: $account_id" in query
    assert params == {"user_id": "u1", "provider": "ibm", "account_id": "acct"}


def test_delete_services_for_account_rejects_unknown_provider():
    client = MemgraphClient.__new__(MemgraphClient)
    with pytest.raises(ValueError):
        client.delete_services_for_account("u1", "gcp", "p")


def test_ibm_status_checker_registered():
    assert connector_status.PROVIDER_CHECKERS["ibm"] is connector_status._check_ibm


def test_check_ibm_exchanges_only_first_account_key():
    from connectors.ibm_connector import client as ibm
    creds = {"accounts": {"b" * 32: {"api_key": "kb"}, "a" * 32: {"api_key": "ka"}}}
    with patch.object(ibm, "get_iam_token", return_value="tok") as tok:
        assert connector_status._check_ibm(creds) == {"connected": True}
    tok.assert_called_once_with("ka")


def test_check_ibm_rejected_key_is_disconnected():
    from connectors.ibm_connector import client as ibm
    with patch.object(ibm, "get_iam_token", side_effect=ibm.IBMAuthError("bad")):
        assert connector_status._check_ibm({"accounts": {"a" * 32: {"api_key": "k"}}}) == {"connected": False}


def test_check_ibm_without_accounts_is_disconnected():
    assert connector_status._check_ibm({}) == {"connected": False}
    assert connector_status._check_ibm({"accounts": {}}) == {"connected": False}
