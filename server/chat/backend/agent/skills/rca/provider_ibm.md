---
id: provider_ibm
name: IBM Cloud RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
  provider_key: ibm
index: "IBM Cloud VPC/IKS/ROKS investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## IBM Cloud / IKS / ROKS Investigation

IMPORTANT: If multiple IBM Cloud accounts are connected, your FIRST `cloud_exec('ibm', ...)` call (without account_id) fans out to ALL accounts. Check `results_by_account` in the response.
- Identify which account(s) have the issue from the results.
- For ALL subsequent calls, pass `account_id='<ACCOUNT_ID>'`. Example: `cloud_exec('ibm', 'is instances', account_id='<ACCOUNT_ID>')`
- Do NOT keep querying all accounts after you know where the problem is.

RCA runs in Ask mode with the account's read-only key. If a call returns `READ_ONLY_CREDENTIAL_MISSING`, report that the account needs a read-only key and continue with the other accounts.

- Find the affected resource: `cloud_exec('ibm', "resource search 'name:<NAME>'", account_id='<ACCOUNT_ID>')`
- VPC instances: `cloud_exec('ibm', 'is instances', account_id='<ACCOUNT_ID>')`
- One instance (status, health reasons): `cloud_exec('ibm', 'is instance <INSTANCE_ID>', account_id='<ACCOUNT_ID>')` (Agent mode) or filter `is instances` output in Ask mode
- Security groups and load balancers: `cloud_exec('ibm', "resource search 'service_name:is AND (type:security-group OR type:load-balancer)'", account_id='<ACCOUNT_ID>')`
- Clusters: `cloud_exec('ibm', 'ks cluster ls', account_id='<ACCOUNT_ID>')`
- Cluster health: `cloud_exec('ibm', 'ks cluster get -c <CLUSTER>', account_id='<ACCOUNT_ID>')` and `cloud_exec('ibm', 'ks worker ls --cluster <CLUSTER>', account_id='<ACCOUNT_ID>')`
- **IMPORTANT**: get the kubeconfig before kubectl: `cloud_exec('ibm', 'ks cluster config -c <CLUSTER>', account_id='<ACCOUNT_ID>')` (IKS and ROKS)
- Pods: `cloud_exec('ibm', 'kubectl get pods -n <NAMESPACE> -o wide')`
- Describe pods: `cloud_exec('ibm', 'kubectl describe pod <POD> -n <NAMESPACE>')`
- Logs: `cloud_exec('ibm', 'kubectl logs <POD> -n <NAMESPACE> --since=1h')`
- Events: `cloud_exec('ibm', 'kubectl get events -n <NAMESPACE> --sort-by=.lastTimestamp')`
- OpenShift (ROKS): use `kubectl` with the same arguments; the `oc` binary is not installed.
