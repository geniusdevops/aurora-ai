"""Tests for the IBM Cloud REST client: IAM token cache, errors, Global Search paging."""

from unittest.mock import MagicMock, patch

import pytest

from connectors.ibm_connector import client as ibm


@pytest.fixture(autouse=True)
def _clear_token_cache():
    ibm._token_cache.clear()
    yield
    ibm._token_cache.clear()


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.json.return_value = body if body is not None else {}
    return r


# --- IAM token -------------------------------------------------------------

def test_token_is_cached_until_near_expiry():
    with patch.object(ibm.requests, "post", return_value=_resp(body={
        "access_token": "tok-1", "expiration": 10_000,
    })) as post, patch.object(ibm.time, "time", return_value=1_000):
        assert ibm.get_iam_token("key-a") == "tok-1"
        assert ibm.get_iam_token("key-a") == "tok-1"
    assert post.call_count == 1


def test_token_refreshes_inside_safety_margin():
    responses = [
        _resp(body={"access_token": "tok-1", "expiration": 1_200}),
        _resp(body={"access_token": "tok-2", "expiration": 9_999}),
    ]
    with patch.object(ibm.requests, "post", side_effect=responses), \
            patch.object(ibm.time, "time", return_value=1_000):
        assert ibm.get_iam_token("key-a") == "tok-1"
        # 200s left < 300s margin -> refreshed
        assert ibm.get_iam_token("key-a") == "tok-2"


def test_cache_is_keyed_per_api_key_and_never_stores_the_key():
    with patch.object(ibm.requests, "post", side_effect=[
        _resp(body={"access_token": "tok-a", "expires_in": 3600}),
        _resp(body={"access_token": "tok-b", "expires_in": 3600}),
    ]):
        assert ibm.get_iam_token("key-a") == "tok-a"
        assert ibm.get_iam_token("key-b") == "tok-b"
    assert "key-a" not in ibm._token_cache and "key-b" not in ibm._token_cache
    assert ibm.api_key_hash("key-a") in ibm._token_cache


def test_invalidate_drops_cached_token():
    with patch.object(ibm.requests, "post", side_effect=[
        _resp(body={"access_token": "tok-1", "expires_in": 3600}),
        _resp(body={"access_token": "tok-2", "expires_in": 3600}),
    ]) as post:
        ibm.get_iam_token("key-a")
        ibm.invalidate_cached_ibm_tokens(["key-a", None])
        assert ibm.get_iam_token("key-a") == "tok-2"
    assert post.call_count == 2


def test_rejected_key_raises_auth_error_with_iam_code():
    body = {"errorCode": "BXNIM0415E", "errorMessage": "Provided API key could not be found."}
    with patch.object(ibm.requests, "post", return_value=_resp(400, body)):
        with pytest.raises(ibm.IBMAuthError) as exc:
            ibm.get_iam_token("bad-key")
    assert exc.value.code == "BXNIM0415E"
    assert exc.value.status == 400
    assert ibm._token_cache == {}


def test_iam_outage_is_not_an_auth_error():
    with patch.object(ibm.requests, "post", return_value=_resp(503)):
        with pytest.raises(ibm.IBMAPIError) as exc:
            ibm.get_iam_token("key-a")
    assert not isinstance(exc.value, ibm.IBMAuthError)


def test_network_error_is_wrapped_without_leaking_key():
    with patch.object(ibm.requests, "post", side_effect=ibm.requests.ConnectionError("boom key-a")):
        with pytest.raises(ibm.IBMAPIError) as exc:
            ibm.get_iam_token("key-a")
    assert "key-a" not in str(exc.value)


# --- Account info ------------------------------------------------------------

def test_account_info_sends_api_key_header():
    with patch.object(ibm.requests, "get", return_value=_resp(body={
        "account_id": "a" * 32, "iam_id": "iam-ServiceId-1", "name": "aurora",
    })) as get:
        info = ibm.get_account_info("key-a", token="tok")
    assert info == {"account_id": "a" * 32, "iam_id": "iam-ServiceId-1", "name": "aurora"}
    headers = get.call_args.kwargs["headers"]
    assert headers["IAM-Apikey"] == "key-a"
    assert headers["Authorization"] == "Bearer tok"


# --- Global Search -----------------------------------------------------------

def test_global_search_follows_cursor_until_empty():
    pages = [
        _resp(body={"items": [{"crn": "1"}], "search_cursor": "c1"}),
        _resp(body={"items": [{"crn": "2"}], "search_cursor": "c2"}),
        _resp(body={"items": [], "search_cursor": "c3"}),
    ]
    with patch.object(ibm.requests, "post", side_effect=pages) as post:
        items = ibm.global_search("tok", fields=["crn"])
    assert [i["crn"] for i in items] == ["1", "2"]
    assert post.call_args_list[1].kwargs["json"]["search_cursor"] == "c1"


def test_global_search_fails_closed_on_repeated_cursor():
    pages = [
        _resp(body={"items": [{"crn": "1"}], "search_cursor": "c1"}),
        _resp(body={"items": [{"crn": "2"}], "search_cursor": "c1"}),
    ]
    with patch.object(ibm.requests, "post", side_effect=pages):
        with pytest.raises(ibm.IBMSearchTruncatedError):
            ibm.global_search("tok")


def test_global_search_fails_closed_on_page_cap(monkeypatch):
    monkeypatch.setattr(ibm, "GLOBAL_SEARCH_MAX_PAGES", 2)
    pages = [
        _resp(body={"items": [{"crn": "1"}], "search_cursor": "c1"}),
        _resp(body={"items": [{"crn": "2"}], "search_cursor": "c2"}),
    ]
    with patch.object(ibm.requests, "post", side_effect=pages):
        with pytest.raises(ibm.IBMSearchTruncatedError):
            ibm.global_search("tok")


def test_global_search_max_items_stops_early_without_error():
    with patch.object(ibm.requests, "post", return_value=_resp(body={
        "items": [{"crn": "1"}, {"crn": "2"}], "search_cursor": "c1",
    })) as post:
        items = ibm.global_search("tok", limit=1, max_items=1)
    assert items == [{"crn": "1"}]
    assert post.call_count == 1


def test_global_search_denied_raises_auth_error():
    with patch.object(ibm.requests, "post", return_value=_resp(403, {"code": "forbidden"})):
        with pytest.raises(ibm.IBMAuthError):
            ibm.global_search("tok")
