"""cloud_exec('ibm', ...): command building, credential setup, login/cleanup, fan-out."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from chat.backend.agent.tools import cloud_exec_tool as ce
from utils.cloud import ibm_cli
from utils.cloud.ibm_credentials import IBMReadOnlyCredentialMissing

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


def _proc(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


# --- command building ---------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("is instances", "ibmcloud is instances --output json"),
    ("ibmcloud is vpcs", "ibmcloud is vpcs --output json"),
    ("is instances --output table", "ibmcloud is instances --output table"),
    ("ks cluster ls", "ibmcloud ks cluster ls --output json"),
    ("ks cluster config -c c1", "ibmcloud ks cluster config -c c1"),
    ("resource service-instances", "ibmcloud resource service-instances --output json"),
    ("resource group-create rg", "ibmcloud resource group-create rg"),
    ("regions", "ibmcloud regions"),
    ("kubectl get pods", "kubectl get pods"),
    ("oc get pods", "oc get pods"),
])
def test_build_ibm_command(command, expected):
    assert ce._build_ibm_command(command) == expected


def test_ibmcloud_alias_normalizes_to_ibm():
    assert ce._normalize_cloud_exec_provider("ibmcloud") == "ibm"


# --- environment setup ----------------------------------------------------------

@pytest.fixture
def one_account():
    with patch("utils.db.connection_utils.get_all_user_connections",
               return_value=[{"account_id": ACCOUNT_A, "region": "us-south"}]), \
            patch.object(ce, "_ibm_resource_group", return_value="Default"):
        yield


def _setup(mode, creds=None, error=None, account_id=None):
    getter = MagicMock(side_effect=error) if error else MagicMock(return_value=creds)
    with patch("utils.cloud.ibm_credentials.get_ibm_account_credentials", getter), \
            patch.object(ce, "get_mode_from_context", return_value=mode):
        return ce.setup_ibm_environment_isolated("u1", account_id), getter


def test_setup_keeps_the_api_key_out_of_the_command_env(one_account):
    (ok, account, method, command_env, login_env, error), getter = _setup(
        "agent", creds={"api_key": "write-key", "default_region": "eu-de"})
    assert ok and account == ACCOUNT_A and method == "api_key" and error is None
    assert "IBMCLOUD_API_KEY" not in command_env
    assert login_env["IBMCLOUD_API_KEY"] == "write-key"
    assert login_env["AURORA_IBM_RESOURCE_GROUP"] == "Default"
    assert command_env["IBMCLOUD_REGION"] == "eu-de"
    assert ibm_cli.HOME_PREFIX in command_env["IBMCLOUD_HOME"]
    assert command_env["IBMCLOUD_HOME"] == login_env["IBMCLOUD_HOME"]
    assert getter.call_args.kwargs["read_only"] is False


def test_setup_uses_read_only_key_in_ask_mode(one_account):
    _result, getter = _setup("ask", creds={"api_key": "ro-key"})
    assert getter.call_args.kwargs["read_only"] is True


def test_setup_fails_closed_in_ask_mode_without_read_only_key(one_account):
    (ok, account, reason, command_env, login_env, error), _ = _setup(
        "ask", error=IBMReadOnlyCredentialMissing(ACCOUNT_A))
    assert not ok and reason == ce.IBM_READ_ONLY_KEY_MISSING
    assert command_env is None and login_env is None
    assert "read-only" in error


def test_read_only_failure_is_not_a_reconnect_prompt():
    payload = json.loads(ce._ibm_setup_failure("Ask mode requires ...", "is instances",
                                                ce.IBM_READ_ONLY_KEY_MISSING))
    assert payload["code"] == "READ_ONLY_CREDENTIAL_MISSING"
    assert "requires_connection" not in payload


def test_setup_rejects_an_unknown_account(one_account):
    (ok, _a, _m, _c, _l, error), _ = _setup("agent", creds={"api_key": "k"}, account_id=ACCOUNT_B)
    assert not ok and ACCOUNT_B in error


def test_each_setup_gets_a_fresh_home(one_account):
    (r1, _), (r2, _) = (_setup("agent", creds={"api_key": "k"}),
                        _setup("agent", creds={"api_key": "k"}))
    assert r1[3]["IBMCLOUD_HOME"] != r2[3]["IBMCLOUD_HOME"]


# --- login script / cleanup -----------------------------------------------------

def test_login_script_reads_secrets_from_env_only():
    argv = ibm_cli.prepare_and_login_argv()
    assert argv[:2] == ["bash", "-c"]
    script = argv[2]
    assert 'mkdir -m 700 "$H"' in script          # refuses a pre-existing path
    assert "ln -s" in script and "cp " in script  # binaries linked, small files copied
    assert "--check-version=false" in script
    assert "IBMCLOUD_API_KEY" not in " ".join(argv[:2])


def test_cleanup_only_deletes_aurora_homes():
    script = ibm_cli.cleanup_argv()[2]
    assert "*/aurora-ibm-*)" in script and "rm -rf --" in script


def test_cleanup_is_a_noop_without_a_home():
    with patch.object(ce, "terminal_run") as run:
        ce._ibm_cleanup(None)
    run.assert_not_called()


# --- single account run (fan-out worker) ------------------------------------------

def _worker(login_rc=0, cmd_rc=0):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ibm_cli.prepare_and_login_argv():
            return _proc(login_rc, out="Credentials were rejected. Code: BXNIM0415E" if login_rc else "OK")
        if args == ibm_cli.cleanup_argv():
            return _proc()
        return _proc(cmd_rc, out='[{"name": "vsi"}]' if cmd_rc == 0 else "",
                     err="FAILED\nnot found" if cmd_rc else "")

    command_env = {"IBMCLOUD_HOME": "/tmp/aurora-ibm-x", "IBMCLOUD_REGION": "us-south"}
    login_env = {**command_env, "IBMCLOUD_API_KEY": "k"}
    with patch.object(ce, "setup_ibm_environment_isolated",
                      return_value=(True, ACCOUNT_A, "api_key", command_env, login_env, None)), \
            patch.object(ce, "terminal_run", side_effect=fake_run):
        result = ce._run_ibm_on_account("u1", {"account_id": ACCOUNT_A}, "is instances", None)
    return result, calls, command_env, login_env


def test_worker_logs_in_runs_and_cleans_up():
    result, calls, command_env, login_env = _worker()
    assert result["success"] and result["output"] == '[{"name": "vsi"}]'
    (login_args, login_kw), (cmd_args, cmd_kw), (clean_args, clean_kw) = calls
    assert login_kw["trusted"] is True and login_kw["env"] is login_env
    assert cmd_args == ["ibmcloud", "is", "instances", "--output", "json"]
    assert cmd_kw["env"] is command_env and "trusted" not in cmd_kw
    assert clean_args == ibm_cli.cleanup_argv() and clean_kw["trusted"] is True


def test_worker_reports_login_failure_and_still_cleans_up():
    result, calls, _c, _l = _worker(login_rc=1)
    assert not result["success"] and "BXNIM0415E" in result["error"]
    assert calls[-1][0] == ibm_cli.cleanup_argv()
    assert len(calls) == 2  # login + cleanup, the command never ran


def test_worker_merges_both_streams_on_failure():
    result, _calls, _c, _l = _worker(cmd_rc=1)
    assert not result["success"] and "FAILED" in result["output"]


def test_worker_marks_missing_read_only_key():
    with patch.object(ce, "setup_ibm_environment_isolated",
                      return_value=(False, ACCOUNT_A, ce.IBM_READ_ONLY_KEY_MISSING, None, None, "Ask mode ...")), \
            patch.object(ce, "terminal_run") as run:
        result = ce._run_ibm_on_account("u1", {"account_id": ACCOUNT_A}, "is instances", None)
    assert result["code"] == "READ_ONLY_CREDENTIAL_MISSING"
    assert all(c.args[0] != ibm_cli.prepare_and_login_argv() for c in run.call_args_list)


# --- fan-out ----------------------------------------------------------------------

def test_fan_out_blocks_writes_in_ask_mode_before_any_login():
    with patch.object(ce, "get_mode_from_context", return_value="ask"), \
            patch.object(ce, "_run_ibm_on_account") as worker:
        out = json.loads(ce._cloud_exec_ibm_multi_account(
            "u1", [{"account_id": ACCOUNT_A}, {"account_id": ACCOUNT_B}], "is instance-delete i-1"))
    assert out["code"] == "READ_ONLY_MODE"
    worker.assert_not_called()


def test_fan_out_keys_results_by_account():
    def fake_worker(user_id, conn, command, timeout):
        return {"account_id": conn["account_id"], "success": conn["account_id"] == ACCOUNT_A}

    with patch.object(ce, "get_mode_from_context", return_value="agent"), \
            patch.object(ce, "_run_ibm_on_account", side_effect=fake_worker):
        out = json.loads(ce._cloud_exec_ibm_multi_account(
            "u1", [{"account_id": ACCOUNT_A}, {"account_id": ACCOUNT_B}], "is instances"))
    assert set(out["results_by_account"]) == {ACCOUNT_A, ACCOUNT_B}
    assert out["success"] is False and out["provider"] == "ibm"


# --- cloud_exec end to end (single account) -----------------------------------------

def _run_cloud_exec(command, mode="agent", account_conns=None):
    """Run cloud_exec('ibm', command) with IBM APIs, the gate and the pod mocked."""
    calls, gated = [], []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ibm_cli.prepare_and_login_argv() or args == ibm_cli.cleanup_argv():
            return _proc()
        if args[:1] in (["which"], ["where"]):
            return _proc()
        return _proc(out='[{"name": "vsi"}]')

    def fake_gate(*, user_id, tool_name, command):
        gated.append(command)
        return SimpleNamespace(allowed=True, code=None, block_reason="")

    command_env = {"IBMCLOUD_HOME": "/tmp/aurora-ibm-x", "IBMCLOUD_REGION": "us-south", "PATH": ""}
    login_env = {**command_env, "IBMCLOUD_API_KEY": "k"}
    with patch.object(ce, "terminal_run", side_effect=fake_run), \
            patch("utils.auth.command_gate.gate_command", side_effect=fake_gate), \
            patch.object(ce, "get_mode_from_context", return_value=mode), \
            patch("utils.db.connection_utils.get_all_user_connections",
                  return_value=account_conns or [{"account_id": ACCOUNT_A}]), \
            patch.object(ce, "setup_ibm_environment_isolated",
                         return_value=(True, ACCOUNT_A, "api_key", command_env, login_env, None)), \
            patch.dict(sys.modules, {"chat.backend.agent.tools.cloud_tools": SimpleNamespace(
                get_tool_capture=lambda: None, get_current_tool_call_id=lambda **_: None)}):
        out = json.loads(ce.cloud_exec("ibm", command, user_id="u1", provider_preference="ibm"))
    return out, calls, gated


def test_cloud_exec_logs_in_runs_and_cleans_up():
    out, calls, gated = _run_cloud_exec("is instances")
    assert out["success"] is True and out["provider"] == "ibm"
    assert gated == ["ibmcloud is instances"]
    argvs = [c[0] for c in calls if c[0][:1] not in (["which"], ["where"])]
    assert argvs[0] == ibm_cli.prepare_and_login_argv()
    assert argvs[1] == ["ibmcloud", "is", "instances", "--output", "json"]
    assert argvs[-1] == ibm_cli.cleanup_argv()
    command_kwargs = calls[[c[0] for c in calls].index(argvs[1])][1]
    assert "IBMCLOUD_API_KEY" not in command_kwargs["env"]


def test_cloud_exec_kubectl_is_gated_unprefixed_and_needs_no_login():
    out, calls, gated = _run_cloud_exec("kubectl get pods -n app")
    assert gated == ["kubectl get pods -n app"]
    argvs = [c[0] for c in calls]
    assert ibm_cli.prepare_and_login_argv() not in argvs
    assert ibm_cli.cleanup_argv() not in argvs


def test_cloud_exec_ask_mode_write_is_refused_before_login():
    out, calls, _gated = _run_cloud_exec("is instance-delete i-1", mode="ask")
    assert out["code"] == "READ_ONLY_MODE"
    assert ibm_cli.prepare_and_login_argv() not in [c[0] for c in calls]


def test_cloud_exec_fans_out_without_account_id():
    with patch.object(ce, "_cloud_exec_ibm_multi_account", return_value='{"multi_account": true}') as fan:
        out, _calls, _gated = _run_cloud_exec(
            "is instances", account_conns=[{"account_id": ACCOUNT_A}, {"account_id": ACCOUNT_B}])
    assert out == {"multi_account": True}
    fan.assert_called_once()


# --- resource group for login ----------------------------------------------------

def test_resource_group_name_is_used_when_stored():
    assert ce._ibm_resource_group("u1", ACCOUNT_A, {"resource_group_name": "Default"}) == "Default"


def test_resource_group_lookup_is_not_repeated_after_a_check():
    with patch("connectors.ibm_connector.client.resolve_resource_group") as resolve:
        assert ce._ibm_resource_group("u1", ACCOUNT_A, {"resource_group": "g1", "resource_group_checked": True}) == "g1"
    resolve.assert_not_called()


def test_resource_group_is_resolved_once_and_remembered():
    with patch("utils.cloud.ibm_credentials.load_ibm_accounts",
               return_value={ACCOUNT_A: {"api_key": "w", "read_only_api_key": "r"}}), \
            patch("connectors.ibm_connector.client.resolve_resource_group",
                  return_value=({"id": "g1", "name": "Default"}, True)) as resolve, \
            patch("utils.cloud.ibm_credentials.update_ibm_account_fields") as update:
        assert ce._ibm_resource_group("u1", ACCOUNT_A, {}) == "Default"
    assert resolve.call_args.args[0] == ["w", "r"]
    assert update.call_args.args[2] == {"resource_group": "g1", "resource_group_name": "Default",
                                        "resource_group_checked": True}
