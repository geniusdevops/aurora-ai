"""Tests for IBM Cloud credential storage (org-scoped Vault JSON)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from utils.cloud import ibm_credentials as creds

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


@pytest.fixture
def store():
    """In-memory stand-in for the org's Vault secret."""
    state = {"data": {}}
    lock_calls = []

    @contextmanager
    def fake_lock(user_id):
        lock_calls.append(user_id)
        yield

    def fake_get(user_id, provider):
        assert provider == "ibm"
        return state["data"]

    def fake_store(user_id, data, provider):
        assert provider == "ibm"
        state["data"] = data

    deleted = MagicMock(return_value=(True, 1))

    def fake_delete(user_id, provider):
        state["data"] = {}
        return deleted(user_id, provider)

    with patch.object(creds, "_org_credentials_lock", fake_lock), \
            patch.object(creds, "get_token_data", side_effect=fake_get), \
            patch.object(creds, "store_tokens_in_db", side_effect=fake_store), \
            patch.object(creds, "delete_user_secret", side_effect=fake_delete):
        yield state, lock_calls, deleted


def test_upsert_adds_accounts_without_dropping_others(store):
    state, lock_calls, _ = store
    creds.upsert_ibm_account("u1", ACCOUNT_A, {"api_key": "ka", "read_only_api_key": None})
    creds.upsert_ibm_account("u1", ACCOUNT_B, {"api_key": "kb", "read_only_api_key": "rb"})
    accounts = state["data"]["accounts"]
    assert set(accounts) == {ACCOUNT_A, ACCOUNT_B}
    # Empty values are not stored.
    assert "read_only_api_key" not in accounts[ACCOUNT_A]
    assert lock_calls == ["u1", "u1"]


def test_read_only_returns_read_only_key(store):
    creds.upsert_ibm_account("u1", ACCOUNT_A, {"api_key": "write", "read_only_api_key": "ro"})
    got = creds.get_ibm_account_credentials("u1", ACCOUNT_A, read_only=True)
    assert got["api_key"] == "ro"
    assert got["account_id"] == ACCOUNT_A


def test_read_only_without_read_only_key_fails_closed(store):
    creds.upsert_ibm_account("u1", ACCOUNT_A, {"api_key": "write"})
    with pytest.raises(creds.IBMReadOnlyCredentialMissing) as exc:
        creds.get_ibm_account_credentials("u1", ACCOUNT_A, read_only=True)
    assert ACCOUNT_A in str(exc.value)


def test_write_mode_returns_write_key(store):
    creds.upsert_ibm_account("u1", ACCOUNT_A, {"api_key": "write", "read_only_api_key": "ro"})
    assert creds.get_ibm_account_credentials("u1", ACCOUNT_A)["api_key"] == "write"


def test_unknown_account_returns_none(store):
    assert creds.get_ibm_account_credentials("u1", ACCOUNT_A) is None


def test_remove_keeps_other_accounts(store):
    state, _, deleted = store
    creds.upsert_ibm_account("u1", ACCOUNT_A, {"api_key": "ka"})
    creds.upsert_ibm_account("u1", ACCOUNT_B, {"api_key": "kb"})
    removed = creds.remove_ibm_account("u1", ACCOUNT_A)
    assert removed == {"api_key": "ka"}
    assert set(state["data"]["accounts"]) == {ACCOUNT_B}
    deleted.assert_not_called()


def test_removing_last_account_deletes_secret(store):
    _, _, deleted = store
    creds.upsert_ibm_account("u1", ACCOUNT_A, {"api_key": "ka"})
    creds.remove_ibm_account("u1", ACCOUNT_A)
    deleted.assert_called_once_with("u1", "ibm")


def test_remove_unknown_account_is_noop(store):
    _, _, deleted = store
    assert creds.remove_ibm_account("u1", ACCOUNT_A) is None
    deleted.assert_not_called()


def test_lock_key_is_org_scoped():
    with patch("utils.auth.stateless_auth.resolve_org_id", return_value="org-1"):
        k1 = creds._lock_key("user-1")
        k2 = creds._lock_key("user-2")
    with patch("utils.auth.stateless_auth.resolve_org_id", return_value="org-2"):
        k3 = creds._lock_key("user-1")
    assert k1 == k2 != k3
    assert 0 <= k1 <= 0x7FFFFFFFFFFFFFFF
