"""IBM Cloud rules in the built-in command-policy templates, and their backfill."""

import json
import re
from unittest.mock import MagicMock

import pytest

from utils.auth import command_policy


def _template(template_id):
    return next(t for t in command_policy.get_policy_templates() if t["id"] == template_id)


def _allowed(template_id, command):
    return any(re.search(r["pattern"], command) for r in _template(template_id)["allow"])


IBM_READS = [
    "ibmcloud is instances --output json",
    "ibmcloud is security-groups",
    "ibmcloud regions",
    "ibmcloud account show",
    "ibmcloud resource service-instances",
    "ibmcloud resource search 'type:resource-instance'",
    "ibmcloud ks cluster ls --output json",
    "ibmcloud ks cluster config -c c1",
    "ibmcloud oc cluster config -c c1",
    "ibmcloud ce app list",
    "ibmcloud iam service-ids",
]

IBM_WRITES = [
    "ibmcloud is instance-delete i-1",
    "ibmcloud is instance-create vsi vpc zone profile subnet",
    "ibmcloud is vpc-delete v1",
    "ibmcloud ks cluster rm -c c1",
    "ibmcloud iam api-key-create k",
    "ibmcloud resource service-instance-delete db",
]


@pytest.mark.parametrize("command", IBM_READS)
@pytest.mark.parametrize("template_id", ["observability_only", "standard_ops"])
def test_ibm_reads_are_allowed(template_id, command):
    assert _allowed(template_id, command), (template_id, command)


@pytest.mark.parametrize("command", IBM_WRITES)
def test_observability_template_does_not_allow_ibm_writes(command):
    assert not _allowed("observability_only", command), command


@pytest.mark.parametrize("command", [
    "ibmcloud is instance-reboot i-1",
    "ibmcloud is instance-stop i-1",
    "ibmcloud ks worker reboot --cluster c1 -w w1",
])
def test_standard_template_allows_operational_ibm_actions(command):
    assert _allowed("standard_ops", command)
    assert not _allowed("observability_only", command)


@pytest.mark.parametrize("command", IBM_READS + IBM_WRITES)
def test_full_template_allows_all_ibm_commands(command):
    full_id = command_policy.get_policy_templates()[-1]["id"]
    assert _allowed(full_id, command)


class _Cursor:
    """Fake cursor: two orgs, one on observability_only, one with no template."""

    def __init__(self):
        self.inserted = []
        self.rowcount = 0
        self._result = []
        self._org = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("SELECT id FROM organizations"):
            self._result = [("org-a",), ("org-b",)]
        elif sql.startswith("SET myapp.current_org_id"):
            self._org = params[0]
        elif sql.startswith("SET"):
            pass
        elif "FROM user_preferences" in sql:
            self._result = [(json.dumps("observability_only"),)] if params[0] == "org-a" else []
        elif sql.startswith("INSERT INTO org_command_policies"):
            self.inserted.append(params)
            self.rowcount = 1

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def test_backfill_adds_only_ibm_rules_for_orgs_on_a_builtin_template():
    cur = _Cursor()
    conn = MagicMock()
    conn.cursor.return_value = cur

    added = command_policy.backfill_template_rules(conn, r"^ibmcloud")

    assert added == len(cur.inserted) == 2
    assert {p[0] for p in cur.inserted} == {"org-a"}
    assert all(p[2].startswith(r"^ibmcloud") for p in cur.inserted)
    conn.commit.assert_called_once()


def test_backfill_is_idempotent_via_on_conflict():
    import inspect
    src = inspect.getsource(command_policy.backfill_template_rules)
    assert "ON CONFLICT (org_id, mode, pattern, source) DO NOTHING" in src
