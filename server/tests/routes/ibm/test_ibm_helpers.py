"""Tests for IBM Cloud connection teardown."""

from unittest.mock import MagicMock, patch

from routes.ibm import helpers

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


def _graph():
    graph = MagicMock()
    return graph, patch("services.graph.memgraph_client.get_memgraph_client", return_value=graph)


def test_disconnect_all_deactivates_every_account_and_drops_tokens():
    graph, graph_patch = _graph()
    entries = {ACCOUNT_A: {"api_key": "ka", "read_only_api_key": "ra"}, ACCOUNT_B: {"api_key": "kb"}}
    with graph_patch, \
            patch.object(helpers, "load_ibm_accounts", return_value=entries), \
            patch.object(helpers, "delete_user_secret", return_value=(True, 1)) as del_secret, \
            patch.object(helpers, "get_all_user_connections", return_value=[
                {"account_id": ACCOUNT_A}, {"account_id": ACCOUNT_B}]) as list_conns, \
            patch.object(helpers, "delete_connection_secret", return_value=True) as deactivate, \
            patch.object(helpers, "invalidate_cached_ibm_tokens") as invalidate:
        assert helpers.disconnect_all_ibm("u1") is True

    del_secret.assert_called_once_with("u1", "ibm")
    assert list_conns.call_args.kwargs == {"raise_on_error": True}
    assert {c.args[2] for c in deactivate.call_args_list} == {ACCOUNT_A, ACCOUNT_B}
    assert sorted(invalidate.call_args.args[0]) == ["ka", "kb", "ra"]
    graph.delete_services_for_provider.assert_called_once_with("u1", "ibm")


def test_disconnect_all_uses_preloaded_entries():
    _, graph_patch = _graph()
    with graph_patch, \
            patch.object(helpers, "load_ibm_accounts") as load, \
            patch.object(helpers, "delete_user_secret", return_value=(True, 0)), \
            patch.object(helpers, "get_all_user_connections", return_value=[]), \
            patch.object(helpers, "invalidate_cached_ibm_tokens") as invalidate:
        helpers.disconnect_all_ibm("u1", [{"api_key": "ka"}])
    load.assert_not_called()
    assert list(invalidate.call_args.args[0]) == ["ka"]


def test_disconnect_all_fails_when_listing_fails():
    _, graph_patch = _graph()
    with graph_patch, \
            patch.object(helpers, "load_ibm_accounts", return_value={}), \
            patch.object(helpers, "delete_user_secret", return_value=(True, 1)), \
            patch.object(helpers, "get_all_user_connections", side_effect=RuntimeError("db")), \
            patch.object(helpers, "invalidate_cached_ibm_tokens"):
        assert helpers.disconnect_all_ibm("u1") is False


def test_disconnect_all_reports_partial_row_failure():
    _, graph_patch = _graph()
    with graph_patch, \
            patch.object(helpers, "load_ibm_accounts", return_value={}), \
            patch.object(helpers, "delete_user_secret", return_value=(True, 1)), \
            patch.object(helpers, "get_all_user_connections", return_value=[
                {"account_id": ACCOUNT_A}, {"account_id": ACCOUNT_B}]), \
            patch.object(helpers, "delete_connection_secret", side_effect=[False, True]) as deactivate, \
            patch.object(helpers, "invalidate_cached_ibm_tokens"):
        assert helpers.disconnect_all_ibm("u1") is False
    # One failure does not abandon the rest.
    assert deactivate.call_count == 2


def test_remove_one_account_scopes_graph_delete_to_account():
    graph, graph_patch = _graph()
    with graph_patch, \
            patch.object(helpers, "remove_ibm_account", return_value={"api_key": "ka"}), \
            patch.object(helpers, "delete_connection_secret", return_value=True) as deactivate, \
            patch.object(helpers, "invalidate_cached_ibm_tokens") as invalidate:
        assert helpers.remove_ibm_account_connection("u1", ACCOUNT_A) is True
    deactivate.assert_called_once_with("u1", "ibm", ACCOUNT_A)
    graph.delete_services_for_account.assert_called_once_with("u1", "ibm", ACCOUNT_A)
    assert list(invalidate.call_args.args[0]) == ["ka"]


def test_remove_unknown_account_returns_false():
    _, graph_patch = _graph()
    with graph_patch, \
            patch.object(helpers, "remove_ibm_account", return_value=None), \
            patch.object(helpers, "delete_connection_secret", return_value=False):
        assert helpers.remove_ibm_account_connection("u1", ACCOUNT_A) is False
