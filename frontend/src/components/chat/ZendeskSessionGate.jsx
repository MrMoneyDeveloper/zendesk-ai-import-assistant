import { useMemo } from "react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Input } from "../ui/input";

function statusToBadge(status) {
  if (status === "ok") return <Badge variant="success">ok</Badge>;
  if (status === "error") return <Badge variant="danger">error</Badge>;
  return <Badge variant="neutral">{status || "unknown"}</Badge>;
}

export default function ZendeskSessionGate({
  credentials,
  onCredentialsChange,
  onValidate,
  isValidating,
  validationResult,
  validationError,
  integrationsStatus,
  integrationsLoading,
  isRestoringSession,
}) {
  const errorText = useMemo(
    () => validationError?.response?.data?.detail || validationError?.message || "",
    [validationError]
  );

  const submit = (event) => {
    event.preventDefault();
    const payload = {
      subdomain: credentials.subdomain?.trim() || "",
      email: credentials.email?.trim() || "",
      api_token: credentials.api_token?.trim() || "",
    };
    if (!payload.subdomain || !payload.email || !payload.api_token) return;
    onValidate(payload);
  };

  return (
    <div className="min-h-screen bg-[#10131a] px-4 py-8 text-slate-100">
      <div className="mx-auto max-w-2xl">
        <Card>
          <CardHeader>
            <h1 className="text-lg font-semibold text-slate-100">Zendesk Session Required</h1>
            <p className="mt-1 text-sm text-slate-400">
              Validate Zendesk credentials before using prompts. Session is stored in this browser tab
              until you sign out.
            </p>
          </CardHeader>
          <CardContent>
            {isRestoringSession ? (
              <div className="mb-4 rounded-md border border-slate-800 bg-slate-950/60 p-3 text-sm text-slate-300">
                Restoring previous Zendesk session...
              </div>
            ) : null}

            <div className="mb-4 rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-300">
              <div className="mb-1 flex items-center gap-2">
                <span className="text-slate-200">Apps Script health:</span>
                {statusToBadge(integrationsStatus?.appscript?.health)}
              </div>
              {integrationsLoading ? <p>Checking integration status...</p> : null}
              {integrationsStatus?.appscript?.health_detail ? (
                <p className="text-rose-300">{integrationsStatus.appscript.health_detail}</p>
              ) : null}
            </div>

            <form className="space-y-3" onSubmit={submit}>
              <div>
                <label className="mb-1 block text-xs text-slate-300">Zendesk Subdomain</label>
                <Input
                  placeholder="example: acme"
                  value={credentials.subdomain}
                  onChange={(event) =>
                    onCredentialsChange((prev) => ({ ...prev, subdomain: event.target.value }))
                  }
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-300">Zendesk Email</label>
                <Input
                  placeholder="agent@acme.com"
                  value={credentials.email}
                  onChange={(event) =>
                    onCredentialsChange((prev) => ({ ...prev, email: event.target.value }))
                  }
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-300">Zendesk API Token</label>
                <Input
                  type="password"
                  placeholder="Zendesk API token"
                  value={credentials.api_token}
                  onChange={(event) =>
                    onCredentialsChange((prev) => ({ ...prev, api_token: event.target.value }))
                  }
                />
              </div>
              <Button type="submit" variant="outline" disabled={isValidating || isRestoringSession}>
                {isValidating ? "Validating..." : "Validate and Unlock"}
              </Button>
            </form>

            {validationResult ? (
              <div className="mt-4 rounded-md border border-slate-800 bg-slate-950/60 p-3 text-sm">
                <div className="mb-1">
                  Result:{" "}
                  {validationResult.ok ? (
                    <Badge variant="success">valid</Badge>
                  ) : (
                    <Badge variant="danger">invalid</Badge>
                  )}
                </div>
                <p className="text-slate-300">{validationResult.detail}</p>
              </div>
            ) : null}

            {errorText ? <p className="mt-3 text-sm text-rose-300">{errorText}</p> : null}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
