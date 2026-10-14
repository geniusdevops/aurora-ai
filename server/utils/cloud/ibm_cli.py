"""IBM Cloud CLI session helpers for cloud_exec.

Every ``cloud_exec('ibm', ...)`` that runs ``ibmcloud`` gets a private
``IBMCLOUD_HOME`` that is created, logged into and deleted *where the command
runs*: in the user's terminal pod under pod isolation, or locally otherwise.
That is why setup and cleanup are shell scripts run through ``terminal_run``
rather than ``tempfile.mkdtemp`` / ``shutil.rmtree`` in this process.

Plugin layout (Phase 0 spike, check 3c): plugins are installed once in the image
under ``AURORA_IBMCLOUD_PLUGINS`` (default ``/opt/ibmcloud/plugins``), which is
treated as read-only. Each home gets a real ``.bluemix/plugins`` tree with:

- a copy of the shared ``plugins/config.json`` (the CLI's plugin registry);
- one real directory per plugin, where files of 1 MB or more (the plugin
  binaries) are symlinked and smaller files are copied, because plugins write
  their own ``config.json``, ``.lastcheck`` and caches inside their directory.

``ibmcloud login`` writes IAM access and refresh tokens into
``$IBMCLOUD_HOME/.bluemix/config.json``, so the home is always deleted after the
command (``cleanup_argv``). Only the login step receives ``IBMCLOUD_API_KEY``;
the user's command runs on the session that login created.
"""

import os
import tempfile
import uuid
from typing import Dict, List

SHARED_PLUGIN_DIR = os.getenv("AURORA_IBMCLOUD_PLUGINS", "/opt/ibmcloud/plugins")
HOME_PREFIX = "aurora-ibm-"
# Files at or above this size in a plugin directory are symlinked, smaller ones copied.
LINK_MIN_BYTES = 1048576

_POD_ISOLATION = os.getenv("ENABLE_POD_ISOLATION", "true") == "true"

# Builds the per-command home and logs in. Reads everything from the
# environment (never argv), so the API key is not in the process arguments.
# mkdir without -p refuses a pre-existing path, so a planted directory or
# symlink at the random home path aborts the login instead of being used.
_PREPARE_AND_LOGIN = r'''
set -eu
umask 077
H="$IBMCLOUD_HOME"
S="${AURORA_IBMCLOUD_PLUGINS:-/opt/ibmcloud/plugins}"
mkdir -m 700 "$H"
P="$H/.bluemix/plugins"
mkdir -p "$P"
if [ -d "$S" ]; then
  if [ -f "$S/config.json" ]; then cp "$S/config.json" "$P/config.json"; chmod u+w "$P/config.json"; fi
  for d in "$S"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    (cd "$d" && find . -type d) | while IFS= read -r sub; do mkdir -p "$P/$name/$sub"; done
    (cd "$d" && find . -type f) | while IFS= read -r f; do
      if [ "$(stat -c %s "$d/$f")" -ge __LINK_MIN_BYTES__ ]; then
        ln -s "$d/$f" "$P/$name/$f"
      else
        cp "$d/$f" "$P/$name/$f"; chmod u+w "$P/$name/$f"
      fi
    done
  done
fi
ibmcloud config --check-version=false >/dev/null 2>&1 || true
# Targeting a resource group needs access to it; without one, still log in so
# everything except resource-group-scoped commands (e.g. Code Engine) works.
if [ -n "${AURORA_IBM_RESOURCE_GROUP:-}" ] && \
   ibmcloud login -r "$IBMCLOUD_REGION" -g "$AURORA_IBM_RESOURCE_GROUP" -q; then
  exit 0
fi
ibmcloud login -r "$IBMCLOUD_REGION" -q
'''.replace("__LINK_MIN_BYTES__", str(LINK_MIN_BYTES))

# Only ever deletes a path that looks like one of our homes.
_CLEANUP = r'''
case "${IBMCLOUD_HOME:-}" in
  */aurora-ibm-*) ibmcloud logout >/dev/null 2>&1 || true; rm -rf -- "$IBMCLOUD_HOME" ;;
esac
'''


def new_home_path() -> str:
    """A fresh, unpredictable home path. It is created by the login script, not here."""
    base = "/tmp" if _POD_ISOLATION else tempfile.gettempdir()
    return os.path.join(base, f"{HOME_PREFIX}{uuid.uuid4().hex}")


def build_envs(
    *,
    base_env: Dict[str, str],
    home: str,
    region: str,
    api_key: str,
    resource_group: str | None,
) -> tuple[Dict[str, str], Dict[str, str]]:
    """Return ``(command_env, login_env)``.

    ``command_env`` has no API key; ``login_env`` adds the key and login inputs.
    """
    command_env = {
        **base_env,
        "IBMCLOUD_HOME": home,
        "IBMCLOUD_REGION": region,
        "IBMCLOUD_VERSION_CHECK": "false",
        "IBMCLOUD_COLOR": "false",
    }
    login_env = {
        **command_env,
        "IBMCLOUD_API_KEY": api_key,
        "AURORA_IBMCLOUD_PLUGINS": SHARED_PLUGIN_DIR,
    }
    if resource_group:
        login_env["AURORA_IBM_RESOURCE_GROUP"] = resource_group
    return command_env, login_env


def prepare_and_login_argv() -> List[str]:
    return ["bash", "-c", _PREPARE_AND_LOGIN]


def cleanup_argv() -> List[str]:
    return ["bash", "-c", _CLEANUP]
