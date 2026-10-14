---
name: ibm
id: ibm
description: "IBM Cloud integration for VPC infrastructure, IKS and Red Hat OpenShift (ROKS) clusters, and platform services via the ibmcloud CLI, across multiple accounts"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
index: "IBM Cloud — VPC (VSIs, subnets, security groups, load balancers), IKS/ROKS, Databases, COS, Code Engine, multi-account"
rca_priority: 10
allowed-tools: cloud_exec
metadata:
  author: aurora
  version: "1.0"
---

# IBM Cloud Integration

## Overview
IBM Cloud accounts are connected with Service ID API keys. Every command runs as `ibmcloud` in a fresh, already-logged-in session for one account and region; the session is discarded afterwards, so CLI state (`ibmcloud target`, plugin settings) never carries over between calls.

## Instructions

### CLI COMMANDS (use cloud_exec with 'ibm')

**CRITICAL: Always use cloud_exec('ibm', 'command') for IBM Cloud commands, NOT terminal_exec.** The `ibmcloud` prefix is added for you, and `--output json` is added where supported.

**Multiple accounts:** without `account_id`, an `ibmcloud` command runs against every connected account and returns `results_by_account`. Once you know the account, pass `account_id='<ACCOUNT_ID>'` (the 32-character IBM account ID) on every call.

**Discovery:**
- Regions: `cloud_exec('ibm', 'regions')`
- Account: `cloud_exec('ibm', 'account show')`
- Search any resource type: `cloud_exec('ibm', "resource search 'service_name:is AND type:security-group'")`
- Service instances (Databases, COS, Secrets Manager, ...): `cloud_exec('ibm', "resource search 'type:resource-instance'")`

**VPC infrastructure (`is` plugin):**
- `cloud_exec('ibm', 'is instances')`, `is vpcs`, `is subnets`, `is volumes`, `is images`
- Load balancers: `cloud_exec('ibm', 'is load-balancers')`
- Security groups and rules: `cloud_exec('ibm', 'is security-groups')`, `is security-group-rules <SG_ID>`
- One instance: `cloud_exec('ibm', 'is instance <INSTANCE_ID>')`
- Commands run in the account's default region (shown in `resource_name`). VPC resources in other regions are not listed by `is` commands; find them with `resource search`, which covers every region.

**Kubernetes (IKS) and OpenShift (ROKS):**
- List clusters: `cloud_exec('ibm', 'ks cluster ls')`
- Cluster details: `cloud_exec('ibm', 'ks cluster get -c <CLUSTER>')`
- Workers: `cloud_exec('ibm', 'ks worker ls --cluster <CLUSTER>')`
- **Get the kubeconfig first** (non-admin): `cloud_exec('ibm', 'ks cluster config -c <CLUSTER>')`. This works for both IKS and ROKS; do not use `oc cluster config` (a bare `oc` runs the OpenShift CLI, not the IBM plugin).
- Then: `cloud_exec('ibm', 'kubectl get pods -n <NS>')`. Use `kubectl` for ROKS clusters too (the `oc` binary is not installed).

**Platform services:**
- Code Engine: `cloud_exec('ibm', 'ce project list')`, then `ce app list` (needs a resource group; the connected account's group is targeted automatically)
- Databases: `cloud_exec('ibm', 'cdb deployments')`

### ASK MODE (read-only)
- Ask mode uses the account's **read-only** API key. If the account has none, commands are refused with `READ_ONLY_CREDENTIAL_MISSING`: tell the user to add a read-only key on the IBM Cloud connector page. Do not retry.
- Allowed: commands with a read verb (`ls`, `list`, `get`, `show`, `search`) and plain plural listings (`is instances`, `is vpcs`, `regions`).
- Hyphenated listings (`is security-groups`, `resource service-instances`) are refused in Ask mode. Use `resource search` instead, e.g. `resource search 'service_name:is AND type:security-group'`.
- Never read credentials: `iam api-keys`, `resource service-keys`, `secrets-manager secret ...` and `--admin` kubeconfigs are always refused.

### CRITICAL RULES
- **ALWAYS** use `cloud_exec('ibm', ...)`, never `terminal_exec`.
- IBM puts the verb last for VPC writes (`is instance-stop`, `is instance-delete`); treat them as writes.
- Do not run `ibmcloud login`, `logout`, `target` or `config`: each command already runs in its own logged-in session.
