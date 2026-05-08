import { useState } from "react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";

function boolToBadge(value) {
  return value ? <Badge variant="success">configured</Badge> : <Badge variant="danger">missing</Badge>;
}

function statusToBadge(status) {
  if (status === "ok") return <Badge variant="success">ok</Badge>;
  if (status === "error") return <Badge variant="danger">error</Badge>;
  return <Badge variant="neutral">{status || "unknown"}</Badge>;
}

export default function IntegrationPanel({
  integrationsStatus,
  integrationsLoading,
  generateMetadata,
  approvalMetadata,
  onValidateZendesk,
  isValidatingZendesk,
  zendeskValidationResult,
  zendeskValidated = false,
  contextStatus = null,
  contextLoading = false,
  selectedContextCount = 0,
}) {
  const [subdomain, setSubdomain] = useState("");
  const [email, setEmail] = useState("");
  const [apiToken, setApiToken] = useState("");

  const stageMeta = generateMetadata?.staging || {};
  const stageResult = stageMeta?.result || {};
  const approvalSync = approvalMetadata?.approval_sync || {};

  const submitValidate = (event) => {
    event.preventDefault();
    if (!subdomain.trim() || !email.trim() || !apiToken.trim()) return;
    onValidateZendesk({
      subdomain: subdomain.trim(),
      email: email.trim(),
      api_token: apiToken.trim(),
    });
  };

  return (
    <section className="mb-6 rounded-lg border border-slate-800 bg-slate-900/70 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">Integration Diagnostics</h2>
        <div className="flex items-center gap-2">
          {zendeskValidated ? (
            <Badge variant="success">Prompt Unlocked</Badge>
          ) : (
            <Badge variant="warning">Prompt Locked</Badge>
          )}
          {integrationsLoading ? <span className="text-xs text-slate-500">Refreshing...</span> : null}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-300">
          <p className="mb-2 font-semibold text-slate-200">Apps Script</p>
          <p className="mb-2 text-slate-400">
            Schema preflight sync runs automatically before generate, approve, and deploy actions.
          </p>
          <p className="mb-1">Configured: {boolToBadge(integrationsStatus?.appscript?.configured)}</p>
          <p className="mb-1">Health: {statusToBadge(integrationsStatus?.appscript?.health)}</p>
          {integrationsStatus?.appscript?.health_detail ? (
            <p className="text-rose-300">{integrationsStatus.appscript.health_detail}</p>
          ) : null}
          {stageMeta?.status ? (
            <div className="mt-2 border-t border-slate-800 pt-2">
              <p>
                Last stage write: {statusToBadge(stageMeta.status)}
              </p>
              {stageResult?.spreadsheet_url ? (
                <a
                  className="text-sky-300 hover:text-sky-200"
                  href={stageResult.spreadsheet_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open staging spreadsheet
                </a>
              ) : null}
              {stageResult?.object_rows_written ? (
                <pre className="mt-2 overflow-auto rounded border border-slate-800 bg-slate-950 p-2 text-[11px]">
                  {JSON.stringify(stageResult.object_rows_written, null, 2)}
                </pre>
              ) : null}
            </div>
          ) : null}
          {approvalSync?.status ? (
            <div className="mt-2 border-t border-slate-800 pt-2">
              <p>
                Last approval sync: {statusToBadge(approvalSync.status)}
              </p>
              {approvalSync?.result?.updated_records ? (
                <p className="mt-1 text-slate-400">
                  Updated rows: {approvalSync.result.updated_records}
                </p>
              ) : null}
              {approvalSync?.detail ? <p className="text-rose-300">{approvalSync.detail}</p> : null}
            </div>
          ) : null}
        </div>

        <div className="rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-300">
          <p className="mb-2 font-semibold text-slate-200">Zendesk Session</p>
          <p className="mb-2">
            Deploy endpoint:{" "}
            {integrationsStatus?.zendesk?.deploy_endpoint_enabled ? <Badge variant="success">active</Badge> : <Badge variant="warning">inactive</Badge>}
          </p>
          <p className="mb-2">
            Session credentials: {zendeskValidated ? <Badge variant="success">valid</Badge> : <Badge variant="warning">not validated</Badge>}
          </p>
          <form className="space-y-2" onSubmit={submitValidate}>
            <input
              value={subdomain}
              onChange={(event) => setSubdomain(event.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1"
              placeholder="subdomain (example: acme)"
            />
            <input
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1"
              placeholder="agent/admin email"
            />
            <input
              value={apiToken}
              onChange={(event) => setApiToken(event.target.value)}
              className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1"
              placeholder="Zendesk API token"
              type="password"
            />
            <Button type="submit" size="sm" variant="outline" disabled={isValidatingZendesk}>
              {isValidatingZendesk ? "Validating..." : "Validate Zendesk Credentials"}
            </Button>
          </form>
          {zendeskValidationResult ? (
            <div className="mt-3 border-t border-slate-800 pt-2">
              <p>
                Result:{" "}
                {zendeskValidationResult.ok ? (
                  <Badge variant="success">valid</Badge>
                ) : (
                  <Badge variant="danger">invalid</Badge>
                )}
              </p>
              <p className="mt-1">{zendeskValidationResult.detail}</p>
              {zendeskValidationResult.base_url ? (
                <p className="mt-1 text-slate-400">Target instance: {zendeskValidationResult.base_url}</p>
              ) : null}
              {zendeskValidationResult.authenticated_user ? (
                <p className="mt-1">
                  Authenticated as: {zendeskValidationResult.authenticated_user} (
                  {zendeskValidationResult.authenticated_user_role || "unknown role"})
                </p>
              ) : null}
            </div>
          ) : null}
          <div className="mt-3 border-t border-slate-800 pt-2">
            <p>
              Context sync:{" "}
              {contextLoading ? (
                <Badge variant="warning">syncing</Badge>
              ) : contextStatus?.ok ? (
                <Badge variant="success">active</Badge>
              ) : (
                <Badge variant="neutral">not loaded</Badge>
              )}
            </p>
            {contextStatus?.catalogs ? (
              <p className="mt-1 text-slate-400">
                loaded items:{" "}
                {Object.values(contextStatus.catalogs).reduce(
                  (acc, entries) => acc + (Array.isArray(entries) ? entries.length : 0),
                  0
                )}{" "}
                | selected: {selectedContextCount}
              </p>
            ) : null}
            {contextStatus?.warnings?.length > 0 ? (
              <p className="mt-1 text-amber-300">
                {contextStatus.warnings[0]}
              </p>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
