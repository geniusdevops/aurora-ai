"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { AlertCircle, AlertTriangle, ExternalLink, Loader2, LogOut, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

// IBM Cloud multizone regions. A static list so the region can be chosen before
// any key is connected; /api/proxy/ibm/regions needs an existing account.
const IBM_REGIONS = [
  { id: "us-south", label: "Dallas (us-south)" },
  { id: "us-east", label: "Washington DC (us-east)" },
  { id: "ca-tor", label: "Toronto (ca-tor)" },
  { id: "br-sao", label: "São Paulo (br-sao)" },
  { id: "eu-gb", label: "London (eu-gb)" },
  { id: "eu-de", label: "Frankfurt (eu-de)" },
  { id: "eu-es", label: "Madrid (eu-es)" },
  { id: "jp-tok", label: "Tokyo (jp-tok)" },
  { id: "jp-osa", label: "Osaka (jp-osa)" },
  { id: "au-syd", label: "Sydney (au-syd)" },
];

const SETUP_COMMANDS = `# Read-only Service ID (used in Ask mode)
ibmcloud iam service-id-create aurora-readonly -d "Aurora read-only"
ibmcloud iam service-policy-create aurora-readonly --roles Viewer,Reader
ibmcloud iam service-policy-create aurora-readonly --account-management --roles Viewer
ibmcloud iam service-api-key-create aurora-readonly-key aurora-readonly

# Write Service ID (used in Agent mode)
ibmcloud iam service-id-create aurora-agent -d "Aurora agent"
ibmcloud iam service-policy-create aurora-agent --roles Operator,Writer
ibmcloud iam service-api-key-create aurora-agent-key aurora-agent`;

interface IbmAccount {
  accountId: string;
  serviceId: string | null;
  defaultRegion: string | null;
  resourceGroup: string | null;
  hasReadOnlyKey: boolean;
}

function notifyProviderStateChanged() {
  window.dispatchEvent(new CustomEvent("providerStateChanged"));
  window.dispatchEvent(new CustomEvent("providerConnectionAction"));
}

export default function IbmOnboardingPage() {
  const router = useRouter();
  const { toast } = useToast();

  const [accounts, setAccounts] = useState<IbmAccount[]>([]);
  const [isLoadingAccounts, setIsLoadingAccounts] = useState(true);
  const [isConnecting, setIsConnecting] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [apiKey, setApiKey] = useState("");
  const [readOnlyApiKey, setReadOnlyApiKey] = useState("");
  const [defaultRegion, setDefaultRegion] = useState("us-south");
  const [resourceGroup, setResourceGroup] = useState("");

  const loadAccounts = useCallback(async () => {
    setIsLoadingAccounts(true);
    try {
      const response = await fetch("/api/proxy/ibm/accounts");
      if (response.ok) {
        const data = await response.json();
        setAccounts(data.accounts ?? []);
      }
    } catch (err) {
      console.error("Error loading IBM Cloud accounts:", err);
    } finally {
      setIsLoadingAccounts(false);
    }
  }, []);

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!apiKey.trim()) {
      setError("An API key is required");
      return;
    }

    setIsConnecting(true);
    setError(null);
    try {
      const response = await fetch("/api/proxy/ibm/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          apiKey: apiKey.trim(),
          readOnlyApiKey: readOnlyApiKey.trim() || undefined,
          defaultRegion,
          resourceGroup: resourceGroup.trim() || undefined,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "Failed to connect IBM Cloud");
      }

      setApiKey("");
      setReadOnlyApiKey("");
      setResourceGroup("");
      localStorage.setItem("isIbmConnected", "true");
      localStorage.setItem("aurora_graph_discovery_trigger", "1");
      notifyProviderStateChanged();

      toast({
        title: "IBM Cloud account connected",
        description: data.hasReadOnlyKey
          ? `Account ${data.accountId} connected.`
          : `Account ${data.accountId} connected without a read-only key. Ask mode will not run commands against it.`,
      });
      await loadAccounts();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to connect IBM Cloud");
    } finally {
      setIsConnecting(false);
    }
  };

  const handleRemove = async (accountId: string) => {
    setRemovingId(accountId);
    try {
      const response = await fetch(`/api/proxy/ibm/accounts/${encodeURIComponent(accountId)}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || "Failed to remove account");
      }
      const remaining = accounts.filter((a) => a.accountId !== accountId);
      setAccounts(remaining);
      if (remaining.length === 0) localStorage.removeItem("isIbmConnected");
      notifyProviderStateChanged();
      toast({ title: "Account removed", description: `IBM Cloud account ${accountId} removed.` });
    } catch (err: unknown) {
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : "Failed to remove account",
        variant: "destructive",
      });
    } finally {
      setRemovingId(null);
    }
  };

  const handleDisconnectAll = async () => {
    setIsDisconnecting(true);
    try {
      const response = await fetch("/api/connected-accounts/ibm", { method: "DELETE" });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || "Failed to disconnect IBM Cloud");
      }
      setAccounts([]);
      localStorage.removeItem("isIbmConnected");
      notifyProviderStateChanged();
      toast({ title: "Disconnected", description: "All IBM Cloud accounts were disconnected." });
    } catch (err: unknown) {
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : "Failed to disconnect IBM Cloud",
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

  const isBusy = isConnecting || isDisconnecting || removingId !== null;

  return (
    <ConnectorAuthGuard connectorName="IBM Cloud">
      <div className="container mx-auto py-8 px-4 max-w-3xl space-y-6">
        <div>
          <h1 className="text-3xl font-bold">IBM Cloud Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect one or more IBM Cloud accounts with Service ID API keys.
          </p>
        </div>

        {(isLoadingAccounts || accounts.length > 0) && (
          <Card>
            <CardHeader className="flex flex-row items-start justify-between space-y-0">
              <div>
                <CardTitle>Connected accounts</CardTitle>
                <CardDescription>Aurora can query every account listed here.</CardDescription>
              </div>
              {accounts.length > 0 && (
                <Button variant="destructive" size="sm" onClick={handleDisconnectAll} disabled={isBusy}>
                  {isDisconnecting ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <LogOut className="h-4 w-4 mr-2" />
                  )}
                  Disconnect all
                </Button>
              )}
            </CardHeader>
            <CardContent>
              {isLoadingAccounts ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading accounts...
                </div>
              ) : (
                <ul className="divide-y">
                  {accounts.map((account) => (
                    <li key={account.accountId} className="flex items-center justify-between gap-4 py-3">
                      <div className="min-w-0">
                        <p className="font-mono text-sm truncate">{account.accountId}</p>
                        <p className="text-xs text-muted-foreground">
                          Region {account.defaultRegion ?? "—"}
                          {account.resourceGroup ? ` · resource group ${account.resourceGroup}` : ""}
                        </p>
                        {!account.hasReadOnlyKey && (
                          <p className="mt-1 flex items-center gap-1 text-xs text-amber-600 dark:text-amber-500">
                            <AlertTriangle className="h-3 w-3" />
                            No read-only key: Ask mode will not run commands against this account.
                          </p>
                        )}
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleRemove(account.accountId)}
                        disabled={isBusy}
                        aria-label={`Remove account ${account.accountId}`}
                      >
                        {removingId === account.accountId ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Trash2 className="h-4 w-4" />
                        )}
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>{accounts.length > 0 ? "Add another account" : "Connect your IBM Cloud account"}</CardTitle>
            <CardDescription>
              Create Service IDs in the account and paste their API keys below.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-3 text-sm">
              <p className="text-muted-foreground">
                Use two Service IDs: a <strong>read-only</strong> one (Viewer and Reader on All Account
                Management and IAM-enabled services) that Aurora uses in Ask mode, and a separate one with
                Operator/Writer access for Agent mode. Without a read-only key, Ask mode refuses to run
                commands against the account.
              </p>
              <pre className="overflow-x-auto rounded bg-muted p-3 text-xs">{SETUP_COMMANDS}</pre>
              <a
                href="https://cloud.ibm.com/iam/serviceids"
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1"
              >
                Open IBM Cloud Service IDs
                <ExternalLink className="w-3 h-3" />
              </a>
            </div>

            {error && (
              <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4 flex items-start gap-3">
                <AlertCircle className="h-5 w-5 text-destructive flex-shrink-0 mt-0.5" />
                <p className="text-sm text-destructive">{error}</p>
              </div>
            )}

            <form onSubmit={handleConnect} className="space-y-4">
              <div className="grid gap-2">
                <Label htmlFor="apiKey">API key (Agent mode) *</Label>
                <Input
                  id="apiKey"
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  required
                  disabled={isBusy}
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="readOnlyApiKey">Read-only API key (Ask mode, recommended)</Label>
                <Input
                  id="readOnlyApiKey"
                  type="password"
                  autoComplete="off"
                  value={readOnlyApiKey}
                  onChange={(e) => setReadOnlyApiKey(e.target.value)}
                  disabled={isBusy}
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="defaultRegion">Default region</Label>
                <Select value={defaultRegion} onValueChange={setDefaultRegion} disabled={isBusy}>
                  <SelectTrigger id="defaultRegion">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {IBM_REGIONS.map((r) => (
                      <SelectItem key={r.id} value={r.id}>
                        {r.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="resourceGroup">Default resource group ID (optional)</Label>
                <Input
                  id="resourceGroup"
                  type="text"
                  placeholder="32-character resource group ID"
                  value={resourceGroup}
                  onChange={(e) => setResourceGroup(e.target.value)}
                  disabled={isBusy}
                />
              </div>

              <div className="flex items-center justify-end pt-2">
                <Button type="submit" disabled={isBusy || !apiKey.trim()}>
                  {isConnecting ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Connecting...
                    </>
                  ) : accounts.length > 0 ? (
                    "Add account"
                  ) : (
                    "Connect IBM Cloud"
                  )}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>

        <div className="text-center">
          <Button variant="ghost" onClick={() => router.push("/connectors")}>
            ← Back to Connectors
          </Button>
        </div>
      </div>
    </ConnectorAuthGuard>
  );
}
