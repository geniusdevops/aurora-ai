# IBM Cloud integration — implementation plan

> **Revision 2 (2026-10-08).** Revised after a code review that checked every reference against `HEAD`. Line numbers have been refreshed, but code keeps moving, so confirm each anchor before editing. Decisions added in this revision are marked **[decided in review]**; anyone who disagrees should raise it before PR 1. A summary of the changes is under "Revision notes" at the end.

## Context
Aurora's AWS integration goes deep:
- an onboarding page and cross-account credentials;
- the agent's `cloud_exec` tool, with multi-account fan-out, read-only enforcement and per-session pods;
- Celery discovery that builds the Memgraph dependency graph;
- a CloudWatch alarm webhook, alert correlation and background RCA.

We want the same depth for **IBM Cloud**. There is no IBM code today (a repo-wide grep finds nothing).

Decisions taken with the user:
- **Auth:** a Service ID API key, stored in Vault and exchanged for short-lived IAM tokens. An optional second, read-only key is used in Ask mode. It plays the role of AWS `read_only_role_arn`.
- **Alerts:** an IBM Cloud Monitoring (Sysdig) webhook notification channel.
- **v1 scope:** multiple IBM accounts, IKS and Red Hat OpenShift (ROKS) clusters, and deep VPC enrichment.
- **Out of v1:** Terraform/`iac_tool`, Event Notifications and Trusted Profiles.

Decisions added in review:
- **Ask mode fails closed [decided in review].** If an account has no `read_only_api_key`, `cloud_exec('ibm', …)` in Ask mode refuses with a clear message and does not fall back to the write key. This follows Azure (`cloud_auth.py:39-61`): IBM IAM has nothing like the AWS session policy, so the read-only classifier would otherwise be the only protection. The onboarding page strongly recommends adding a read-only key.
- **One URL scheme [decided in review].** `ibm_bp` is registered at the root with no `url_prefix`, the same way `aws_bp` is (`main_compute.py:595`). Every route spells out its full path: `/ibm/connect`, `/ibm/accounts`, `/ibm/monitoring/webhook/<owner_id>` and so on. The CORS entry, `_OPEN_PREFIXES`, the client proxy and the webhook URL all use `/ibm/...`. There is no `/ibm_api` prefix.
- **Credential hygiene in pods [decided in review].** Every `cloud_exec('ibm')` call, and every fan-out worker, uses a private `IBMCLOUD_HOME` inside the pod. It is removed by a trusted in-pod `ibmcloud logout; rm -rf "$IBMCLOUD_HOME"` in `finally`. No IBM login state outlives the command in a session pod.

Templates to follow:
- **Scaleway:** the footprint of an API-key connector.
- **AWS:** multi-account fan-out and per-account deletion.
- **CloudWatch:** alerts and the webhook URL.
- **Elastic:** the per-connection webhook secret.

The "New Connector Checklist" in `AGENTS.md:208-243` applies. Two items there are stale:
- Skills are registered through SKILL.md front-matter, not `registry.py`.
- `CONNECTOR_DIRS` lives in `utils/providers.py`, not in the test file.

## Naming (fixed up front — several lists depend on it)
| Thing | Value | Why |
|---|---|---|
| Cloud provider id | `ibm` | Used by `user_connections.provider`, the Vault provider, discovery and `CLOUD_EXEC_PROVIDERS` |
| CLI alias | `ibmcloud` → normalized to `ibm` | Normalized by `_normalize_cloud_exec_provider` (`cloud_exec_tool.py:44`), next to `fly` → `flyio` (`:55`) |
| Alert source / connector id | `ibmmonitoring` | One word of 13 characters. `incidents.source_type` and `incident_alerts.source_type` are `VARCHAR(20)` (`db_utils.py:773`, `:807`) and have no CHECK constraint. |
| Alert table | `ibm_monitoring_alerts` | |
| URL paths | `/ibm/...` (cloud), `/ibm/monitoring/...` (alerts) | The blueprint is at the root, like AWS. See the URL decision above. |
| Graph node property | `ibm_account_id` | Used for per-account deletion; mirrors `aws_account_id` |

## Phase 0 — Spike (1 day, before coding)
Confirm each item below against a real IBM account and record the results in the PR.
1. `ibmcloud login -r <region> -q` reads `IBMCLOUD_API_KEY` from the environment. Under pod isolation the env is sent as `export K='v'; cmd` inside the kubectl-exec command string (`terminal_run.py:177-185`, `tool_executor.py:94-104`), so the key is still visible in the exec argv and audit logs, as Azure's `--password` is today. Check whether `terminal_run` can feed secret env over stdin instead. If it can, do that for IBM and Azure. If not, record it as a known limitation.
2. All `ibmcloud` state lives under `IBMCLOUD_HOME`. List exactly which files `login` writes; the IAM access and refresh tokens are in `.bluemix/config.json`.
3. **Plugins and per-command homes.** Plugins install under `$IBMCLOUD_HOME/.bluemix/plugins`. Confirm that plugins installed at build time can be shared read-only into each private home, either by symlinking `.bluemix/plugins` to `/opt/ibmcloud/plugins` or through a plugin-dir setting if the CLI has one. Check this for the image runtime users `app` (`Dockerfile:305`) and `appuser` (`Dockerfile-user-terminal:183`).
4. Global Search: `POST https://api.global-search-tagging.cloud.ibm.com/v3/resources/search` with `search_cursor` paging. Record which `type` / `service_name` values come back for VSI, VPC, subnet, SG, LB, IKS/ROKS, Databases, COS **buckets**, Secrets Manager, Event Streams, Code Engine and DNS Services.
5. **IKS/ROKS kubeconfig.** Confirm that `ibmcloud ks cluster config -c <id>` (non-admin, IAM OIDC) works with a Service ID key. Also confirm that `ibmcloud oc cluster config -c <id>` gives a working kubeconfig for ROKS, so we don't need `oc login -u apikey -p <key>`, which would put the key in argv. Record where the master URL comes from (the containers API `GET /global/v2/getCluster`).
6. **The CLI's error output.** Does `FAILED` plus the message go to stdout or stderr? This decides whether `cloud_exec_tool.py:2349-2374` needs an IBM branch like Scaleway's stdout and stderr merge. Capture the "cluster could not be found" text, and the IAM error codes for invalid and deleted keys.
7. **Code Engine REST.** Confirm that `GET https://api.<region>.codeengine.cloud.ibm.com/v2/projects/{id}/apps` returns `run_env_variables`, so env vars can be read without `ibmcloud ce project select`, which is CLI state.
8. Confirm the Sysdig webhook channel supports custom headers. Capture sample payloads for firing and resolved alerts, including the event id used for dedup.

## Phase 1 — Foundations
### IBM REST client (new): `server/connectors/ibm_connector/client.py`
It uses plain `requests` and no IBM SDKs, which avoids the dependency conflicts that disabled the AWS MCP server. It provides:
- `get_iam_token(api_key)`:
  - `POST https://iam.cloud.ibm.com/identity/token` with `grant_type=urn:ibm:params:oauth:grant-type:apikey`.
  - The token is cached in-process per key hash until about 5 minutes before it expires, modelled on `_aws_cache` (`utils/auth/stateless_auth.py:52`).
  - It also exposes `invalidate_cached_ibm_tokens(api_key_hashes)` for disconnect, mirroring `invalidate_cached_aws_creds`.
- `get_account_info(token)`: the account id and Service ID, from `GET https://iam.cloud.ibm.com/v1/apikeys/details` with `IAM-ApiKey`.
- `global_search(token, query, fields)`:
  - pages until the cursor is empty;
  - fails closed on a repeated cursor or the page cap, raising a typed error like Azure's `ResourceGraphTruncatedError` (`services/discovery/providers/azure_asset_discovery.py:132`, raised at `:207` and `:217`).
- `vpc_get(token, region, path)`: `https://{region}.iaas.cloud.ibm.com/v1/...?version=YYYY-MM-DD&generation=2`, with `start` pagination.
- `containers_get(token, path)`: `https://containers.cloud.ibm.com/global/v2/...`. It is also used to read the cluster master URL.
- `dns_svcs_get(token, path)`.
- `code_engine_get(token, region, path)`: used for Code Engine env vars (Phase 5).
- Typed errors: `IBMAuthError` carries the IAM error codes (`BXNIM0415E` and similar).

### Credential storage
**Vault: one secret per org for provider `ibm`.**
- The `user_tokens` row is unique per `(org_id, provider)` (`db_utils.py:308`). Reads and deletes go through `org_read_predicate` (`secret_ref_utils.py:216-235`, `:371-400`). The secret is *named* after the user who last wrote it (`token_management.py:69`).
- Always read and write through the org-resolved `get_token_data(user_id, "ibm")`. Store with the generic `store_tokens_in_db` `else` branch (`token_management.py:452`). No IBM-specific branch is needed, because one `user_tokens` row can't describe several accounts anyway.
- The value is JSON:
  `{"accounts": {"<account_id>": {"api_key": "...", "read_only_api_key": "...?", "service_id": "...", "default_region": "us-south", "resource_group": "...?"}}}`.
- `store_tokens_in_db` always schedules prediscovery (`token_management.py:467-468`). For `ibm` that is the behaviour we want.

**`user_connections`: one row per account.**
- `account_id` is the IBM account GUID, with `connection_method='api_key'` and `region` set. The table is unique on `(user_id, provider, account_id)` (`db_utils.py:431`).
- Rows are written with `save_connection_metadata` (`utils/db/connection_utils.py:30`), which already writes `status='active'`.
- Rows are read with `get_all_user_connections(user_id, "ibm")` (`:275`). It is org-scoped and returns only `active` rows.
- **Don't call `set_connection_status`.** Other connectors pass it `connected` / `disconnected` (for example `scaleway_routes.py:131`), which would hide the row from every `status='active'` query. It is also user-scoped (`:146`).

**New helper module `utils/cloud/ibm_credentials.py`.** It provides `get_ibm_account_credentials(user_id, account_id, read_only)`, `upsert_ibm_account(...)` and `remove_ibm_account(...)`.
- Updates read, modify and write the org's Vault JSON, so they are serialised with **`pg_advisory_xact_lock(hashtext('ibm:creds:' || org_id))`**, following `services/actions/executor.py:134-140`.
- A Postgres lock is used rather than Redis because there is no shared Redis lock helper and `get_redis_client()` can return `None`.
- With `read_only=True` and no stored read-only key, it raises `IBMReadOnlyCredentialMissing`. Callers turn that into the fail-closed Ask-mode message.

**Secret providers.** Add `"ibm"` and `"ibmmonitoring"` to `SUPPORTED_SECRET_PROVIDERS` (`utils/secrets/secret_ref_utils.py:38-77`).

### CLI install
Install `ibmcloud` with the plugins `vpc-infrastructure`, `container-service`, `cloud-object-storage`, `cloud-databases`, `secrets-manager` and `code-engine`, plus `oc`.
- **Install the plugins into a shared, read-only location** (for example `/opt/ibmcloud/plugins`) that each per-command home links to, as confirmed in Phase 0, item 3. Don't install them into root's `~/.bluemix`, because the runtime users and the private homes would not see them.
- Add the install to these images:
  - `server/Dockerfile`: next to the Scaleway/Tailscale/flyctl layers, `:198-215`.
  - `server/Dockerfile-user-terminal`: next to Scaleway, `:148-158`. Add `.bluemix` to the dot-dir `mkdir` at `:174-175`.
  - `server/Dockerfile-chatbot-dev.dockerfile`: `:169-179`.
- Set `IBMCLOUD_VERSION_CHECK=false` and `IBMCLOUD_COLOR=false` in the isolated env.

### Login cache (new): `server/utils/cloud/ibm_login_cache.py`
This follows `utils/cloud/azure_login_cache.py`, whose API is `attach(env, command) -> Optional[CachedLogin]`, `CachedLogin.ensure / should_relogin / relogin`, `uses_local_cli_state` and `is_cached_dir`.
- **Like Azure, `attach()` returns `None` when `ENABLE_POD_ISOLATION` is on** (`azure_login_cache.py:242-246`). The cache can't vouch for a login that happened inside a pod. Pod isolation is on by default, so **in production every IBM command logs in**. The cache only helps local dev and the Celery discovery workers, which don't use pods.
- The cache key is an HMAC of `(account_id, api_key, region)`. The region is part of the key because `ibmcloud target -r` writes into the directory.
- Directories are created with mode 0700, and expire after an idle window or a maximum age (same as Azure: 1800 s idle, 8 h max).
- Commands that change CLI state (`login`, `logout`, `target`, `config`, `plugin`, `update`) never run against a cached directory.
- Cached directories are never registered for `rmtree` cleanup. Only private temp homes are.
- Tests mirror `tests/utils/test_azure_login_cache.py`, including the pod-isolation bypass.

## Phase 2 — Connect flow
### Backend
Add a new blueprint, `server/routes/ibm/__init__.py`, registered **at the root**, plus `ibm_routes.py`.
- Every route uses `@limiter.limit` and `@require_permission("connectors", ...)` (pattern at `scaleway_routes.py:66-68`).
- `tests/architectural/test_connector_rbac.py` enforces `require_permission` / `require_auth_only` for directories listed in `CONNECTOR_DIRS`. It does not check `@limiter`, but keep it anyway.
- Don't copy Scaleway's manual `before_request` OPTIONS handler (`routes/scaleway/__init__.py`). `AGENTS.md` says CORS preflight is handled globally.

Routes:
- `POST /ibm/connect` accepts `{api_key, read_only_api_key?, default_region, resource_group?}`. It:
  1. exchanges the key for a token;
  2. reads the account id with `get_account_info`;
  3. if a read-only key is given, checks it belongs to the same account;
  4. runs a sanity read (Global Search, `limit=1`);
  5. stores the account with `upsert_ibm_account`, then calls `save_connection_metadata(..., connection_method='api_key', region=default_region)`. It does **not** call `set_connection_status`.

  Calling it again with another account's key adds that account (multi-account).
- `GET /ibm/accounts` lists accounts. Each entry has `has_read_only_key`, so the UI can warn that Ask mode will be blocked for that account.
- `DELETE /ibm/accounts/<account_id>`:
  - removes the account from the Vault JSON;
  - marks the row inactive with `delete_connection_secret(user_id, "ibm", account_id)` (`connection_utils.py:371`, org-scoped);
  - invalidates that key's IAM token cache;
  - deletes that account's graph nodes with a new `delete_services_for_account(user_id, "ibm", account_id)`.
  - The new function generalises `delete_services_for_aws_account` (`memgraph_client.py:814`) and matches on `ibm_account_id`.
  - Known limitation, inherited from AWS: graph deletion matches on `user_id` (`:783`, `:821`), so it removes only the nodes discovered for that user.
- `GET /ibm/status` and `POST /ibm/disconnect` for all accounts. These follow Scaleway (`scaleway_routes.py:278-309`), with two differences: rows are deactivated through the org-scoped path, and the IAM token and login caches are cleared.
- `GET /ibm/regions` and `GET /ibm/resource-groups?account_id=` feed the onboarding dropdowns.

### Other backend wiring
- **Main app:** register the blueprint (next to Scaleway at `main_compute.py:641-642`, but with no `url_prefix`) and add a `/ibm/*` CORS entry (next to `:145`).
- **Connector directories:** add `ibm` to `CONNECTOR_DIRS` (`utils/providers.py:11`; Scaleway is at `:39`).
- **Status check:** add a `_check_ibm` checker to `PROVIDER_CHECKERS` (`routes/connector_status.py:825`). It must be cheap: it runs on every status poll, inside a 12-worker batch with a timeout. Do one IAM token exchange (cached) for the first active account, or use `_check_credentials_only`. Don't exchange every account's key on each poll.
- **canExecute:** add an `ibm` entry to `_augment_execution_capability` (`connector_status.py:986-1043`). Without it, IBM commands never show the "Run" button.
- **Generic disconnect:** add an `ibm` branch to `DELETE /api/connected-accounts/<target_user_id>/<provider>` (`routes/account_management.py:242-430`), next to the AWS and Azure branches. It deactivates every `ibm` row and clears the IAM token and login caches. Without it, disconnecting from the generic UI leaves rows `active` for fan-out and discovery.

### Frontend
- **Onboarding page** `client/src/app/ibm/onboarding/page.tsx`:
  - A step-by-step guide to creating the Service IDs, with `ibmcloud iam` commands to copy. Recommended access: a **read-only** Service ID (Viewer and Reader on All Account Management and IAM-enabled services), needed for Ask mode, and a separate Operator/Writer Service ID for Agent mode.
  - The key form, the account list (flagging accounts without a read-only key), add/remove account, and the region and resource-group pickers.
  - After connecting, set the `aurora_graph_discovery_trigger` flag, as AWS does (`aws/onboarding/page.tsx:476`).
- **Proxy route** `client/src/app/api/proxy/ibm/[...path]/route.ts`, mapping to `/ibm/` + path.
- **Connector registration:**
  - Add the card to `ConnectorRegistry.ts`, with a `stateEvent: 'ibmStateChanged'` and matching `revalidateOnEvents`.
  - Add Scaleway-style handling to `ConnectorCard.tsx` (`:64`, `:179-183`, `:457`) and `ConnectorDialogs.tsx` (`:12`, `:112-118`).
  - On disconnect, call `window.dispatchEvent(new Event('ibmStateChanged'))` and `queryClient.invalidate()`. This is the `AGENTS.md` checklist item.
- **Other new frontend files:** a management component `components/ibm-provider-integration.tsx`, and the icon `client/public/ibm.svg`.
- Add `ibm` to these provider lists:
  - `app/api/connected-accounts/[provider]/route.ts:28`, plus a DELETE special case like Scaleway's (`:95-106`)
  - `app/api/provider-preferences/route.ts:8`
  - `app/chat/components/useChatSendHandlers.ts:81`
  - `components/cloud-provider/core/ProviderPolling.ts`, in three places: around `:109`, the switch at `:162`, and the storage-event keys at `:315-319`
  - `hooks/useConnectedAccounts.ts:21`
  - `hooks/use-execution-capabilities.ts:40-58` (`providerForCommand`: map `ibmcloud` → `ibm`)
  - `app/org/components/OrgActivity.tsx` (icon at `:51`, label at `:79`)
  - `lib/services/graph-discovery.ts` (`GRAPH_DISCOVERY_PROVIDERS`, array at `:2`)
  - `components/tool-calls/CommandLogo.tsx` (logo map at `:103`, CLI detection at `:556`)
  - `components/tool-calls/tool-command-parser.ts` (`getProviderCli` `:8-13`, `RECOGNIZED_CLI_REGEX` `:17`)
- Leave out on purpose: the gcp/aws/azure-only lists in `ProviderPreferenceContext.tsx`, `providerSelector.tsx` and `ClientShell.tsx`, and the project-style UI (`projectUtils.ts`, `ProjectListItem.tsx`, `provider-root-project`). Revisit them if resource groups should behave like projects.

## Phase 3 — Agent execution (`cloud_exec('ibm', ...)`)
Everything here is in `server/chat/backend/agent/tools/cloud_exec_tool.py` unless noted.

### Environment setup: `setup_ibm_environment_isolated(user_id, account_id=None, region=None)`
- Returns `(success, resolved_scope, "api_key", env, auth_argv)`, the 5-tuple shape Azure uses (`setup_azure_environment_isolated`, `:162-217`).
- Reads the mode from `get_mode_from_context()` like the other setup functions, rather than taking a `read_only` parameter. In Ask mode it uses `read_only_api_key`. **If there is none, it returns failure with "Ask mode requires a read-only IBM API key for account X; add one on the IBM connector page."**
- `env` contains `PATH`, `HOME`, a **private** `IBMCLOUD_HOME` (a cached directory only when `ibm_login_cache.attach` returns one, which is never under pod isolation), `IBMCLOUD_API_KEY`, `IBMCLOUD_REGION` and the noise-suppression variables.
- `IBMCLOUD_HOME` must always be set. Otherwise the CLI falls back to the shared `/home/appuser/.bluemix` (`_ISOLATED_HOME`, `:24`).
- `auth_argv` is `["ibmcloud","login","-r",region,("-g",rg),"-q"]`.

### Logging in and cleaning up
- Reuse the Azure `auth_argv` mechanism (`:2188-2231`): run `ibmcloud login` once through `terminal_run(..., trusted=True)` before the user command, in the same pod.
- **Cleanup:** extend the `finally` block (`:2800-2809`), which today only handles `AZURE_CONFIG_DIR`, to cover `IBMCLOUD_HOME`.
  - Under pod isolation, run a trusted in-pod `ibmcloud logout >/dev/null 2>&1; rm -rf "$IBMCLOUD_HOME"`. A local `rmtree` only deletes the server container's copy, and the IAM tokens in `.bluemix/config.json` would otherwise stay readable by later `cat`/`find` commands in the same session pod (`command_policy.py:640`).
  - Without pod isolation, `rmtree` private homes only, never cached ones.
  - Apply the same pod cleanup to Azure's `AZURE_CONFIG_DIR` in the same PR, since it has the same problem.

### Multiple accounts: `_cloud_exec_ibm_multi_account`
This is based on `_cloud_exec_aws_multi_account` (`:1230`).
- Runs the Ask-mode check up front (`:1247-1263`). In Ask mode, any account without a read-only key returns an error entry rather than running.
- Uses `ThreadPoolExecutor(max_workers=min(n, 5))` with `contextvars.copy_context().run`.
- Unlike AWS (STS env only, no login) and Azure (one shared login for all subscriptions), **each IBM worker needs its own private `IBMCLOUD_HOME`, its own login and its own cleanup**. That is three pod execs per account and no cache in production, so the concurrency cap is 5, not 10.
- Returns `results_by_account`.
- Applies when there is no `account_id` and the user has more than one active IBM connection.

### Agent prompt and wrapper
- `cloud_exec_wrapper` (`cloud_tools.py:1361`) already exposes `account_id`. Update its docstring (`:1362-1365`, which mentions only AWS) to describe IBM account pinning.
- Add an IBM block to the per-provider multi-account prompt text (`chat/backend/agent/prompt/provider_rules.py:94-112`): fan out first, read `results_by_account`, then pin `account_id`.

### Other changes in `cloud_exec_tool.py`
- The dispatch branch next to Scaleway and Fly.io (`:1774-1786`).
- `_CLI_PREFIX['ibm'] = 'ibmcloud'` (`:1685`). It is used only to build `gated_cmd` for the command gate (`:1689`). **`cloud_exec('ibm', 'oc get pods')` would be gated as `ibmcloud oc get pods`**, and `ibmcloud oc` is a real command (the ROKS alias of `ks`). So for `ibm`, commands that start with `oc` or `kubectl` are gated and run without the prefix, and `supported_cli_tools` / `_first_token` (`:1997-2019`) agree with that.
- `supported_cli_tools` / `default_cli` (`:1997-2019`).
- Add the prefix when it's missing (`:2024-2036`).
- Add `--output json` where the plugin supports it (`:2038-2097`).
- The resource-name block (`:1849-1894`) and `_build_projection_command` (`:2631`).
- Error and stdout handling (`:2311-2383`). Whether an IBM stdout-merge branch is needed depends on Phase 0, item 6.

### Read-only classifier (`utils/security/read_only_classifier.py`)
`is_read_only_command` now lives here (`:503`); `cloud_exec_tool.py:37-40` only re-exports it. The module's design rule (`:18-22`) is **"gate on shape, never on a list of vendor names"**, so don't add an `_is_ibm_read_only`. Make shape-based changes instead:
- **Bare-noun reads.** Today `ibmcloud is instances`, `resource service-instances`, `regions` and bare `target` are denied with `no recognised read verb` (`:497-499`). Add a generic shape rule, "CLI, then a subcommand group, then a plural noun, with no positional operand and no write flags, is a listing". Also add `ls` and `get` read shapes if they're missing. Get sign-off from the classifier's owners. If they won't accept a shape rule, document these IBM commands as needing Agent mode.
- **Credential reads.** `iam api-keys`, `resource service-keys` and `secrets-manager secret …` are already blocked by `CREDENTIAL_WORDS` (`:127`). Keep regression tests for them.
- **`--admin`.** Add it to `CREDENTIAL_FLAGS` (`:145`). Today `ks cluster config --admin` is **allowed**, because `config` is a read verb (`:36`).
- **`oc`.** Add it to `OPERAND_POSITIONAL_CLIS` (`:111`) next to `kubectl`, so `oc logs <name-containing-a-write-word>` isn't over-blocked.
- **Tests:** extend `tests/security/test_read_only_classifier.py`. The classifier imports directly, so no loader is needed.

### Command policy (`utils/auth/command_policy.py`)
- Add `^ibmcloud\s+...` allow rules to the three templates, next to the Fly.io entries: Observability `:678-680`, Standard `:761-763` and Full `:827-829`. `oc` is already covered by the kubectl rules (`:643`, `:728`, `:810`).
- **Existing orgs:** new template rules only take effect when a template is applied or seeded (`:868-906`). Orgs that already have an allowlist enabled would get `POLICY_DENIED` on every `ibmcloud` command (`cloud_exec_tool.py:1696-1700`). Add a migration that appends the IBM rules to orgs whose policy matches an unmodified built-in template. Everyone else gets a release note telling them to re-apply.

### Provider lists that must include `ibm`
Paths are under `server/`:
- `chat/backend/agent/prompt/provider_rules.py:9` (`CLOUD_EXEC_PROVIDERS`) and `:94-112` (the multi-account text above)
- `chat/backend/agent/tools/terminal_exec_tool.py:276` (`CLOUD_ROUTES`: `('ibmcloud ', ...)`)
- `cloud_provider_utils.py:139` (keywords)
- `chat/backend/agent/utils/tool_call_history.py:31` (`PROVIDER_CLI`) and `:34` (`RECOGNIZED_CLI_PREFIXES`: add `"ibmcloud "` and `"oc "`)
- `chat/background/rca_prompt_builder.py:54`
- `chat/background/task.py:396`
- `chat/backend/agent/orchestrator/inputs.py:60` and `orchestrator/select_skills.py:22`
- `main_chatbot.py:1229` (`valid_providers`). This one is **mandatory**: unknown providers are dropped, and `cloud_exec` then fails with "No cloud provider detected" (`cloud_exec_tool.py:1656-1673`). Fly.io is missing here today too.
- `utils/cloud/cloud_utils.py:141`. Low impact: `set_provider_preference` has no production caller, but keep it consistent.
- `services/discovery/tasks.py:16` (`SUPPORTED_PROVIDERS`). This belongs to Phase 5, but is easy to miss.

The Fly.io PR missed several of these; don't repeat that.

### Skills (under `server/chat/backend/agent/skills/`)
- New `integrations/ibm/SKILL.md`:
  - front-matter `connection_check.method: provider_in_preference`, like Scaleway. The method actually checks `get_connected_providers()` (`registry.py:245-262`);
  - `rca_priority`;
  - tools `[cloud_exec]`.
- New `rca/provider_ibm.md`, modelled on `rca/provider_aws.md`. It explains fan-out, then pinning `account_id`, plus the IKS/ROKS kubeconfig flow from Phase 4. It loads only if `ibm` is in the provider sets passed to `registry.py:463-470`, i.e. the `rca_prompt_builder.py` and `task.py` lists above.
- Mention IBM in `core/cloud_access.md`, `core/tool_selection.md`, `rca/tool_mapping.md` and `core/identity.md`. If VSI SSH access is in scope, also mention it in `core/ssh_access.md`, `rca/ssh_investigation.md` and `rca/background/background_vm_access.md`.
- Agent-tool checklist items (`StructuredTool`, `is_<name>_connected`, `run_<name>_tool`) don't apply: IBM is a `cloud_exec` CLI provider, not a custom tool.

## Phase 4 — IKS / ROKS
### Discovery (`services/discovery/enrichment/kubernetes_enrichment.py`)
- **Credentials.** Add an `elif provider == "ibm"` branch to `_get_cluster_credentials` (`:140`; the existing branches are at `:159-166`) that calls `_get_ibm_cluster_credentials`.
  - `provider_envs` holds **one env per provider** (`discovery_service.py:312`). The `_aws_multi` key read at `:111` and `:490` is never populated, so there is no working multi-account precedent to copy. `_get_ibm_cluster_credentials` therefore builds its **own per-account env**: it reads the cluster's `ibm_account_id`, looks up that account's credentials, creates a private (or cached) `IBMCLOUD_HOME` and logs in to the cluster's region.
  - It runs `ibmcloud ks cluster config -c <cluster_id>` for IKS, or `ibmcloud oc cluster config -c <cluster_id>` for ROKS. Both are non-admin and use no `oc login`, which keeps the key out of argv (Phase 0, item 5).
  - `run_cli_command` runs a list without a shell (`cli_utils.py:85`), so a `$IBMCLOUD_API_KEY` in argv would never expand anyway.
  - It reads `cluster_id`, `ibm_account_id`, `region` and `master_url` from node **`metadata`**. Don't repeat the AKS bug of reading top-level keys (`:125-126`), which is a separate existing bug that this plan doesn't fix.
- **kubectl env.** Add an `ibm` branch to `_resolve_kubectl_env` (`:481`), so kubectl runs with the same `HOME`/`KUBECONFIG` that `cluster config` wrote. Otherwise it would fall back to `provider_envs["ibm"]` (another account) or a bare env.
- **Stale clusters.** `_STALE_CLUSTER_FRAGMENTS` (`:455`, checked by `_is_stale_cluster_error` at `:468`) already contains `"cluster not found"`, but IBM says "The specified cluster could not be found". Add the IBM text or error code captured in Phase 0, item 6.
- **Error prefix.** Prefix every IBM K8s error with `[ibm:<account_id>]`. Otherwise messages like "Failed to get credentials for cluster X: [cli] …" contain no "ibm" substring and land in `unknown_provider_errors` (Phase 5 error attribution).

### Agent
`kubectl` and `oc` on IBM clusters go through `cloud_exec('ibm', 'ks cluster config -c <id>')` and then run in the same session pod. Because of the per-command cleanup in Phase 3, the kubeconfig must be written **outside** `IBMCLOUD_HOME` (to the session's normal `KUBECONFIG`). Phase 0 checks that `cluster config` honours `KUBECONFIG` and that the IAM OIDC token embedded in the kubeconfig doesn't depend on `IBMCLOUD_HOME` surviving. If it does, document that each `kubectl` session needs a fresh `cluster config`. Describe this flow in `provider_ibm.md`.

## Phase 5 — Discovery and dependency graph
### Registration
- **Supported providers:** add `ibm` to `services/discovery/tasks.py:16` (`SUPPORTED_PROVIDERS`).
- **`discovery_service.py`:**
  - the import, and `PROVIDER_MODULES` (`:35`).
  - The `_setup_provider_env` branch (`:46`) must return a **non-None** env, because `:312` drops providers whose env is None from K8s enrichment. Discovery runs in Celery rather than a pod, so the login cache applies here.
  - Register only a *private* temp home for cleanup in `finally`, the way `_gcloud_tmpdir` is handled (`:244-245`, cleaned at `:193-195`). Never register a cached directory.
  - Keep `owner_id` available. `_setup_provider_env` pops it (`:78`), and `discover()` receives the org's representative user. Since the IBM secret is org-scoped (Phase 1), `get_ibm_account_credentials` must use the org-resolved `get_token_data`. Add a test with a representative user who isn't the credential owner.
  - `provider_enrichments` (`:349`).
  - The error-attribution tuples (`:332`, `:387`), which are substring matches on `("gcp","aws","azure")`. Add `"ibm"`, and rely on the `[ibm:<account_id>]` prefix.
- **IAM error fragments.** Add them to `_CREDENTIAL_ERROR_FRAGMENTS` (`tasks.py:142`): `BXNIM0415E`, `BXNIM0408E`, "Provided API key could not be found", "API key is invalid", plus anything else Phase 0 captures.
- **Per-account deactivation.** Today `_handle_provider_errors` (`tasks.py:273`) counts a failure only when *all* errors in a run are credential errors, and `_mark_provider_inactive` (`:214`, `:240-256`) then deactivates **every** connection for the provider. With one bad IBM account and the rest healthy, that would switch off all IBM accounts after 3 runs. Add account-scoped handling:
  - parse `[ibm:<account_id>]` out of credential errors;
  - count failures per account (`_CREDENTIAL_FAIL_THRESHOLD=3`, `:134`);
  - deactivate only that account's `user_connections` row.

  The provider-wide path stays for providers without account prefixes.

### Inventory: `providers/ibm_asset_discovery.py`
The entry point is `discover(user_id, credentials, env=None)`, the standard signature.
- Gets accounts with `get_all_user_connections(user_id, "ibm")` and runs one account per thread (max 10). This follows AWS `discover_all_accounts` (`aws_asset_discovery.py:291`, `:324`) for the per-account fan-out, not Azure, which uses one principal and batched queries.
- Runs **Global Search** per account, with fields `name, crn, type, service_name, region, resource_group_id, tags`.
- **VPC fill-in.** For each region that has VPC resources, list `instances`, `load_balancers`, `subnets`, `virtual_network_interfaces` and `endpoint_gateways` through the VPC API, and set `vpc_id` (the VPC CRN) and `endpoint` on the nodes. This must happen here because `write_services` (`discovery_service.py:291`) persists `vpc_id` and `endpoint` before enrichment runs.
  - `private_ip` and `security_groups` are kept on the in-memory node for Phase 3 inference (`:417`). `_build_service_row` does not persist them.
  - `network_proximity_inference` uses **only** `vpc_id` (`:80`).
- **`vpc_id` fallback for platform services.** Databases, COS, Secrets Manager, Event Streams and similar services have no VPC, so proximity inference would link almost nothing to them. Set their `vpc_id` as follows:
  1. the VPC of a VPE endpoint gateway that targets them, if there is one;
  2. otherwise `ibm-<account_id>/<resource_group_id>`.

  This mirrors GCP's `gcp-<project>` (merge logic at `network_proximity_inference.py:84-104`) and Azure's `azure-<sub>/<rg>` (`azure_asset_discovery.py:322-323`).
- **Node fields:**
  - `cloud_resource_id` = CRN.
  - `name`: the resource name, **made unique across all IBM accounts in the run**. The node id is `{user_id}:{provider}:{name}` (`memgraph_client.py:911`), so two accounts each with a VPC named `default` would otherwise merge into one node, last write wins, and per-account deletion would remove the merged node.
    - Rule: if a name appears more than once across the user's IBM inventory, append `-<last 8 chars of the CRN GUID>` to every copy.
    - Set `display_name` to the original name.
    - Replace any `:` in names, because a name with two or more colons is treated as an id by `_resolve_service_id` (`:869-878`).
    - Known residual risk: edges are matched on `(user_id, name)` across providers (`:381-382`), so an IBM node that shares a name with another provider's node can pick up its edges. That's an existing cross-provider problem and out of scope; note it in the PR.
  - `metadata` = `{ibm_account_id, resource_group_id, service_name, ibm_type, crn, region, cluster_id?, master_url?}`. Get `master_url` from `containers_get`; Global Search doesn't return it.
- **Graph writes.** Make `ibm_account_id` a first-class field: add it in `_build_service_row` (`memgraph_client.py:907`, next to `aws_account_id` at `:926`), **and** in the Cypher SET lists of `upsert_service` (`:147`, `:164`) and `batch_upsert_services` (`:209`, `:226`).
- **Resource mapping.** Add `IBM_RESOURCE_MAP` and `map_ibm_resource(service_name, type)` to `resource_mapper.py`. Every target `resource_type` below already exists.

  | IBM resource | Aurora `resource_type` |
  |---|---|
  | `is.instance`, bare-metal | `vm` |
  | `is.vpc` | `vpc` |
  | `is.subnet` | `subnet` |
  | `is.security-group` | `firewall` |
  | `is.load-balancer` | `load_balancer` |
  | `containers-kubernetes` (IKS or ROKS, set from the version) | `kubernetes_cluster` |
  | `databases-for-postgresql/mysql/mongodb/enterprisedb` | `database` |
  | `databases-for-redis` | `cache` |
  | `databases-for-elasticsearch/opensearch` | `search_engine` |
  | `cloud-object-storage` bucket | `storage_bucket` |
  | `secrets-manager` | `secret_store` |
  | `messagehub`, `messages-for-rabbitmq` | `message_queue` |
  | `codeengine` app/job | `serverless_function` |
  | `dns-svcs` | `dns_zone` |
  | `internet-svcs` | `cdn` |
  | `container-registry` | `container_registry` |
  | `is.share` | `filesystem` |

  The exact keys come from the Phase 0 spike.

### Enrichment: `enrichment/ibm_enrichment.py`
The signature is `enrich(user_id, ibm_nodes, credentials)`, returning `{"enrichment_data": {"ibm_relationships": [...]}, "errors": [...]}`. Everything goes under that IBM-only key, so it can't collide with other providers' keys. A collision already happens today: Azure's `dns_records` overwrites AWS Route 53 records through `enrichment_data.update()` at `discovery_service.py:359`. That bug is out of scope, but note it in the PR.

For each account and region it collects:
- **Security groups and rules.** SG→SG and /32 CIDR rules, resolved to their member instances. These edges go through `ibm_relationships`, **not** `security_group_inference`, which expects AWS-shaped `GroupId`/`IpPermissions` data (`:288`).
  - Dependency type comes from `infer_dependency_type_from_port` (`resource_mapper.py:242`) only when `port_min == port_max` and the port is a well-known one. Otherwise:
    - if the rule's target is a known IBM Databases or Event Streams node, use that node's resource type;
    - else use `"network"`.
  - Confidence is 0.9 for SG→SG and 0.7 for CIDR.
  - Elasticsearch/OpenSearch on 9200 isn't mapped today; add it.
- **Load balancer pools and members.** LB → instance or IP. Confidence 1.0.
- **VPE endpoint gateways.** The gateway's target is a Databases, COS or other service CRN. The gateway's IPs are linked to instances in the same VPC through security-group or subnet reachability. Confidence 0.85. This is an IBM-specific signal with no AWS counterpart.
- **DNS Services zone records.** A/CNAME records that resolve to an LB, an instance IP or a VPE address. Confidence 0.8.

### Code Engine env vars
Serverless enrichment fetches env vars itself, per node, in `_fetch_raw_env_vars` (`serverless_enrichment.py:369-407`). It returns `{"env_vars", "errors"}` and runs *after* `provider_enrichments`, assigning `enrichment_data["env_vars"]` directly (`discovery_service.py:380`).
- Add the IBM branch in `_fetch_raw_env_vars`.
- Thread the IBM credentials through `enrich` (`:451`) and `_process_serverless_node`. Today they pass only the AWS and GCP envs.
- Read `run_env_variables` with `code_engine_get` (REST, Phase 0, item 7) rather than `ibmcloud ce`, which needs a per-project `ce project select`.
- Don't emit `env_vars` from `ibm_enrichment`: serverless enrichment would overwrite it.

### Inference: `inference/ibm_relationship_inference.py`
A new module, modelled on `gcp_relationship_inference.py`.
- Resolves CRN → node name using `cloud_resource_id` plus suffix matching, so the name suffixes added above still resolve.
- Emits `DEPENDS_ON` edges with `discovered_from=["ibm_<signal>"]`.
- Register it in `_INFERENCE_MODULES` (`connection_inference.py:28`), and update the "11 methods" docstrings.
- Edges are de-duplicated per name pair, keeping the highest confidence.
- Network proximity works without changes once `vpc_id` (including the fallback) is set.

### Cleanup
Disconnecting an account or the whole provider removes its nodes (Phase 2 routes). Stale-node handling is generic.

## Phase 6 — IBM Cloud Monitoring alerts
### Routes: `server/routes/ibm/monitoring_routes.py`
A blueprint nested under the root-registered IBM blueprint, the same way CloudWatch is nested under AWS (`routes/aws/__init__.py:6-12`). All paths are spelled out in full as `/ibm/monitoring/...`.
- `status`, `connect`, `disconnect`, and `rca-settings` (via `register_rca_settings_routes`, `routes/ci_shared.py:14`).
- `connect` stores the webhook secret (`secrets.token_hex(32)`) and the regional Monitoring console base URL under provider `ibmmonitoring`.
  - The console URL goes in `client_id`, so `_build_source_url` can return it; Elastic does this in `token_management.py:282-299`.
  - `store_tokens_in_db` schedules prediscovery as a side effect. That's harmless.
- `webhook-url` builds the URL **the way CloudWatch does** (`cloudwatch_routes.py:442-453`):
  - the owner comes from `get_token_owner_id(user_id, "ibmmonitoring")`;
  - the base is `NEXT_PUBLIC_BACKEND_URL`, falling back to `NGROK_URL` on localhost;
  - **not `BACKEND_URL`**, which is the internal Docker hostname (`.env.example:67`).

  It returns `{base}/ibm/monitoring/webhook/<owner_id>`, the secret, and step-by-step instructions for the Sysdig notification channel, including adding a custom header `X-Aurora-Webhook-Secret`.
- `webhook` is public: add `/ibm/monitoring/webhook/` to `_OPEN_PREFIXES` in `main_compute.py:205-253`, next to CloudWatch at `:252`. In order, it:
  1. calls `validate_user_exists` (`utils/auth/stateless_auth.py:88`, as CloudWatch does at `cloudwatch_routes.py:480`);
  2. loads the stored `ibmmonitoring` secret and returns 404 if there is none;
  3. checks the secret in constant time with Elastic's `_extract_presented_secret` pattern (`elastic_routes.py:340-385`: header, then Bearer, then Basic password, compared with `hmac.compare_digest`);
  4. parses the JSON;
  5. enqueues `process_ibm_monitoring_alert.delay`.

  Keep the handler named `webhook`, so the RBAC test's `EXEMPT_FUNCTIONS` covers it (`test_connector_rbac.py:52-80`). Elastic's handler is named `alert_webhook` and isn't exempt that way.

### Celery task: `server/routes/ibm/monitoring_tasks.py`
The Celery name is `ibmmonitoring.process_alert`. Model it on `cloudwatch_tasks.py`, with these changes:
- **Persisting (`_persist_alert`, the counterpart of `_persist_alarm` at `:93`).** Deduplicate on the Sysdig event id with `INSERT ... ON CONFLICT DO NOTHING RETURNING id` against a partial unique index. CloudWatch *does* dedup (a SELECT at `:102-113`, plus the index `idx_cloudwatch_alarms_sns_dedup`), but it checks and then inserts, so two concurrent deliveries raise an IntegrityError and a retry.
- **Resolved alerts** (`state == OK` / `resolved: true`). Close the matching incident, as `_handle_resolved_alarm` does (`:145-208`), **and send the SSE update**. CloudWatch's version doesn't, so a resolved incident doesn't update live in the UI.
- Extract:
  - severity: Sysdig 0–7 mapped to critical/high/medium/low;
  - **service**, from scope labels in this order: `kube_deployment_name` / `kube_workload_name`, then `ibm_resource_name`, then `host_hostname`, then `kube_cluster_name`. The value must match the node names discovery writes, **including any CRN suffix added for duplicate names**. Resolve the label through the `display_name` → `name` lookup for that user's IBM nodes, so `TopologyStrategy` (50% of the correlation score, `alert_correlator.py:54`) gets a hit;
  - `alert_metadata`: alert id, condition, scope, segment and the **per-alert** Sysdig link. `_build_source_url` is per source and per user (`incidents_routes.py:37`, cached at `:229-233`), so it can only return the console base URL.
- `_try_correlate` (the counterpart of `_try_correlate_alarm` at `:234`): `AlertCorrelator().correlate` plus `apply_correlation_outcome`, wrapped in SAVEPOINTs (`:242-283`).
- Create the incident with `ON CONFLICT` (`:299`), and add the primary row to `incident_alerts` (`:339-357`).
- `_post_incident_actions` (`:366-446`): the SSE update, `generate_incident_summary`, the rate-limit check, `build_rca_prompt` and `run_background_chat`.
- Add the task to the Celery `include` list (`celery_config.py:131`).

### Database (`utils/db/db_utils.py`)
- DDL for `ibm_monitoring_alerts`:
  - columns: `id SERIAL` (because `source_alert_id` is an INTEGER), `user_id`, `org_id`, `event_id`, `alert_name`, `severity`, `state`, `scope`, `payload JSONB` and `received_at`;
  - a partial unique index on `(org_id, event_id) WHERE event_id IS NOT NULL`;
  - `alert_payload_tool` needs the `id`, `payload`, `user_id` and `received_at` columns.
- Add the table to `rls_tables` (the list starts at `:1555`), which `tests/architectural/test_rls_coverage.py` enforces. Also add it to `org_id_tables` (`:3088`); no test checks that list, so don't forget it.

### Source-type lists that need `ibmmonitoring`
Backend (under `server/`):
- `chat/background/task.py:296` (`_RCA_SOURCES`)
- `chat/background/summarization.py:58-121` (per-source summary details)
- `chat/backend/agent/tools/alert_payload_tool.py:18` (`_SOURCE_TABLE_MAP`)
- `routes/incidents_routes.py`:
  - `_build_source_url` (`:37`) returns the console base URL from `client_id`;
  - add a raw-payload branch after `incidentio` (`:691`, within `:495-703`).
- The `connector_status.py` checker.
- `utils/providers.py` provider list (around `:20-45`), so RBAC scanning covers the routes.
- Optional: the `aurora_mcp` `query_alerts` gating (`aurora_mcp/registry.py:115`, `tools_gated.py:109`). If you add it, update `_ALERTS_PATH_BY_SOURCE` at the same time, or the module-level assert fails. CloudWatch isn't there either.

Frontend:
- `lib/services/incidents.ts:11` (`AlertSource`). Icons resolve to `/${source}.svg`, so add `client/public/ibmmonitoring.svg`.
- A `ConnectorRegistry.ts` card.
- `OrgActivity.tsx`.
- A Monitoring toggle and setup section on the IBM onboarding page, like `CloudWatchAlertToggle` (`aws/onboarding/page.tsx:122`).

Skill: `server/chat/backend/agent/skills/integrations/ibmmonitoring/SKILL.md`, with `provider_in_preference` and `provider_key: ibmmonitoring`, like CloudWatch. It tells the agent to query metrics with `cloud_exec('ibm', ...)` or the Sysdig API.

### CloudWatch gaps to note in the PR, not fix here
- CloudWatch is missing from `_RCA_SOURCES`, `_SOURCE_TABLE_MAP` and `SUPPORTED_SECRET_PROVIDERS`. The last one means `get_user_token_data` returns None (`secret_ref_utils.py:214-217`), so the TopicArn gets re-pinned on every message (`cloudwatch_routes.py:249-300`).
- Its dedup checks and then inserts, which races under concurrent deliveries.
- Its resolve path sends no SSE update.

## Phase 7 — Docs
- `website/docs/integrations/connectors.md`:
  - Add a new `### IBM Cloud` section under `## Cloud Providers`. It covers Service ID setup, the recommended IAM access, why a read-only key is needed for Ask mode, multi-account, IKS/ROKS, and the fact that orgs with an existing command allowlist must re-apply a template.
  - Document the Monitoring webhook under `## Observability Tools` (`:1026`), not in the cloud-provider section.
- `README.md:136`: add IBM to the multi-cloud list.
- `.env.example`: no new required variables, since all credentials are per org. Mention the `ibmcloud` CLI in the dev setup docs. (`environment.md:539-546` documents a `NEXT_PUBLIC_ENABLE_SCALEWAY` flag that no code reads, so don't copy it.)
- Add a release note covering the command-policy migration and the Ask-mode requirement for a read-only key.

## Suggested PR breakdown
1. Foundations and connect flow (Phases 1–2), including the generic-disconnect branch. **Implemented on `feature/ibm-cloud-connector`.** Deviations from the phases above:
   - **Moved to PR 2, because nothing uses them before `cloud_exec` does:** the login cache, the `ibmcloud` CLI install in the Dockerfiles (it also waits on Phase 0, item 3), `canExecute`, and the chat/agent provider lists (`useChatSendHandlers`, `provider-preferences`, `ProviderPolling`, `useConnectedAccounts` `INFRA_PROVIDERS`, `CommandLogo`, `tool-command-parser`, `use-execution-capabilities`). Adding `ibm` to those lists earlier would let users pick IBM in chat before it can run commands.
   - `graph-discovery.ts` moves to PR 3.
   - **No management dialog component.** The connector card's default `path` behaviour opens `/ibm/onboarding`, which doubles as the management page (account list, add, remove, disconnect all). So `ConnectorCard.tsx`, `ConnectorDialogs.tsx` and `components/ibm-provider-integration.tsx` are unchanged or not needed.
   - **Refresh events:** the connector config has no `stateEvent` field (the `AGENTS.md` checklist item is stale). The page dispatches the existing `providerStateChanged` / `providerConnectionAction` events, which `useConnectedAccounts` already revalidates on.
   - **`client/public/ibm.svg` is a placeholder text mark.** Replace it with an approved IBM Cloud logo asset.
2. Agent `cloud_exec`: in-pod login and cleanup (including the Azure cleanup fix), the login cache, the CLI install, `canExecute`, read-only classifier shape rules, the policy and its migration, provider lists, and skills (Phase 3).
3. Discovery, enrichment and inference, plus IKS/ROKS and per-account deactivation (Phases 4–5).
4. Monitoring alerts (Phase 6) and docs (Phase 7).

## Verification
**Unit tests** (new, under `server/tests/`, with fixtures in `server/tests/fixtures/ibm/`; create `tests/services/discovery/`, which doesn't exist yet):
- `connectors/test_ibm_client.py`: IAM token caching, expiry and invalidation; Global Search cursor paging; fail-closed on a repeated cursor; `IBMAuthError` mapping.
- `utils/test_ibm_credentials.py`: the org-scoped read-modify-write under the advisory lock; `read_only=True` with no key raises `IBMReadOnlyCredentialMissing`.
- `utils/test_ibm_login_cache.py`: mirrors the Azure cache tests (key isolation, never shares state-changing commands, idle expiry), plus **`attach()` returns None under pod isolation**.
- `security/test_read_only_classifier.py`, extended with:
  - allow and deny tables for IBM command shapes;
  - credential reads stay blocked;
  - `ks cluster config --admin` is denied;
  - `oc logs <name>` isn't over-blocked.
- `chat/test_ibm_cloud_exec.py`:
  - Ask mode with no read-only key fails closed, for both single-account and fan-out;
  - `IBMCLOUD_HOME` is always set;
  - the in-pod cleanup command is issued in `finally`;
  - `oc` / `kubectl` commands under `ibm` are gated without the `ibmcloud` prefix.
- `auth/test_command_policy.py`: the IBM rules are in all three templates, and the migration appends them only to unmodified template policies.
- `services/discovery/test_ibm_discovery.py`:
  - CRN → node mapping;
  - **name de-duplication across accounts**, with `display_name` and no `:` in names;
  - `vpc_id` fill-in and the platform-service fallback;
  - `[ibm:<account_id>]` error prefixing;
  - IBM IAM errors matched by `tasks._is_credential_error`;
  - **one bad account out of two deactivates only that account after 3 runs**;
  - credentials resolve when the representative user isn't the credential owner.
- `services/discovery/test_ibm_k8s.py`: the per-account env in `_get_ibm_cluster_credentials`; the `_resolve_kubectl_env` IBM branch; the IBM stale-cluster text.
- `services/discovery/test_ibm_relationship_inference.py`: SG (including port ranges), LB, VPE and DNS samples produce the expected edges and confidences; CRN resolution works with suffixed names.
- `routes/ibm/test_monitoring_alert.py`, modelled on `tests/routes/incidentio/test_alert_rca.py`. It covers:
  - secret accept and reject, and 404 when there is no stored secret;
  - `ON CONFLICT` dedup with concurrent inserts;
  - firing vs resolved, with an SSE update on resolve;
  - severity mapping;
  - the service extraction order and suffixed-name resolution;
  - the webhook URL uses the public base URL;
  - `len("ibmmonitoring") <= 20`.

**Existing architecture tests must still pass:** `test_connector_rbac.py`, `test_rls_coverage.py`, `tests/secrets/test_secret_ref_utils.py`, `tests/auth/test_command_policy.py` and `tests/security/test_allowlist_bypass.py`. Run `cd server && pytest`.

**End to end** (`make dev` with `ENABLE_POD_ISOLATION=true`, and a real IBM account with a VSI, an LB, Databases for PostgreSQL, an IKS cluster and Monitoring):
1. **Connect.** Connect two accounts on `/ibm/onboarding`, only one of them with a read-only key. Both appear in the list, the account without a read-only key is flagged, and both have `active` rows in `user_connections`.
2. **Chat, Ask mode.**
   - "list my VPC instances across accounts" fans out and returns `results_by_account`. The account without a read-only key returns the fail-closed message.
   - A write such as `ibmcloud is instance-stop` is blocked, and so is `ibmcloud iam api-keys`.
   - Afterwards, `ls -la ~` and `find / -name config.json -path '*bluemix*'` in the same session pod find no IBM login state.
3. **Chat, Agent mode:** a write command goes through the approval prompt.
4. **Discovery.** `POST /api/graph/discover`. Memgraph Lab (`localhost:3001`) shows:
   - IBM `Service` nodes with `vpc_id` (including the fallback on Databases) and `ibm_account_id`;
   - `DEPENDS_ON` edges from `ibm_lb`, `ibm_sg`, `ibm_vpe` and `ibm_dns`;
   - IKS workloads;
   - no merged nodes for same-named resources in the two accounts.
5. **Alerts.** Trigger a Sysdig test notification. An incident is created with source `ibmmonitoring`, then the RCA runs. A second alert on a neighbouring service within 5 minutes shows a topology `correlation_hint`; this holds while `RECURRENCE_DETECTION_MODE=live`, the default (`recurrence_config.py:54`). A resolved notification closes the incident and the UI updates live. Sending the same notification twice creates one alert row.
6. **Bad key.** Use an invalid key for one account on 3 discovery runs in a row. Only that account is marked inactive; the other keeps working.
7. **Disconnect.** Disconnect from the generic Connected Accounts screen. All IBM rows are inactive, and neither discovery nor `cloud_exec` uses them.

## Revision notes (rev 2)
- Re-anchored every reference to current `HEAD`. Most rev-1 line numbers came from commit `9856584`.
- Decided three things: Ask mode fails closed without a read-only key; one root-level `/ibm/...` URL scheme; in-pod cleanup of `IBMCLOUD_HOME` after every command.
- Corrected these assumptions:
  - the login cache is a no-op under pod isolation;
  - `is_read_only_command` moved to `read_only_classifier.py`, which is shape-based and not per-vendor;
  - Vault credentials are org-scoped;
  - `set_connection_status` would hide accounts;
  - CloudWatch already dedups;
  - `network_proximity_inference` uses only `vpc_id`;
  - serverless env vars are fetched inside `_fetch_raw_env_vars`;
  - the webhook base URL must be public, not `BACKEND_URL`.
- Added:
  - cross-account node-name de-duplication;
  - per-account credential deactivation;
  - per-account K8s envs and the `_resolve_kubectl_env` branch;
  - a `vpc_id` fallback for platform services;
  - the plugin-location spike item;
  - the generic-disconnect branch;
  - `canExecute`;
  - the command-policy migration;
  - the missing provider lists (`provider_rules.py:94-112`, `RECOGNIZED_CLI_PREFIXES`, `use-execution-capabilities.ts`, the `ProviderPolling.ts` spots);
  - the frontend state events;
  - the advisory-lock choice;
  - SSE on resolve;
  - matching E2E checks.
