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
  darkMode = false,
  integrationsStatus,
  integrationsLoading,
  onRefreshIntegrations,
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
  const llmRoutes = generateMetadata?.llm_routes || {};
  const plannerRoute = llmRoutes?.planner || null;
  const generatorRoute = llmRoutes?.generator || null;
  const geminiRoute = llmRoutes?.gemini_supervisor || null;
  const defaultProvider = llmRoutes?.default_provider || "groq";
  const defaultModel = llmRoutes?.default_model || generatorRoute?.model || "unknown";
  const fallbackPolicy = llmRoutes?.fallback_policy || {};
  const narratorRoute = llmRoutes?.progress_narrator || {};
  const narrationState = generateMetadata?.progress_narration || {};
  const supervisorState = generateMetadata?.supervisor || {};
  const supervisorReviews = Array.isArray(supervisorState?.reviews) ? supervisorState.reviews : [];
  const latestSupervisorReview = supervisorReviews.at(-1) || null;
  const supervisorCallCounts = supervisorState?.call_counts || {};
  const supervisorBlockedChunks = Array.isArray(supervisorState?.blocked_chunk_ids)
    ? supervisorState.blocked_chunk_ids
    : [];
  const supervisorRetryAttempts = Array.isArray(supervisorState?.regeneration_attempts)
    ? supervisorState.regeneration_attempts
    : [];
  const coverageState = generateMetadata?.department_coverage || generateMetadata?.quality_gates?.coverage || {};
  const coverageDepartments = Array.isArray(coverageState?.departments) ? coverageState.departments : [];
  const coverageMissing = Array.isArray(coverageState?.missing) ? coverageState.missing : [];
  const chunkRows = Array.isArray(generateMetadata?.chunking?.chunks) ? generateMetadata.chunking.chunks : [];
  const fallbackCounts = chunkRows.reduce((acc, chunk) => {
    if (!chunk?.deterministic_fallback && !chunk?.forced_deterministic && !chunk?.template_first) return acc;
    const type = chunk?.object_type || "unknown";
    acc[type] = (acc[type] || 0) + 1;
    return acc;
  }, {});
  const fallbackEntries = Object.entries(fallbackCounts);
  const providerFailoverChunks = chunkRows.filter((chunk) => chunk?.provider_fallback_used).length;

  // Theme-aware class sets
  const panel = darkMode
    ? "mb-6 rounded-lg border border-[#7B1FFF]/30 bg-[#120522]/70 p-4"
    : "mb-6 rounded-lg border border-slate-200 bg-white p-4";

  const headingText = darkMode ? "text-slate-200" : "text-slate-900";
  const subtextColor = darkMode ? "text-slate-400" : "text-slate-500";
  const bodyText = darkMode ? "text-slate-300" : "text-slate-700";

  const card = darkMode
    ? "rounded-md border border-[#7B1FFF]/25 bg-[#07030F]/55 p-3 text-xs"
    : "rounded-md border border-slate-200 bg-slate-50 p-3 text-xs";

  const divider = darkMode
    ? "border-t border-[#7B1FFF]/20 pt-2 mt-2"
    : "border-t border-slate-200 pt-2 mt-2";

  const inputClass = darkMode
    ? "w-full rounded border border-[#7B1FFF]/35 bg-[#07030F]/70 px-2 py-1 text-slate-100 placeholder:text-slate-500 focus:outline-none focus:border-[#7B1FFF]/70"
    : "w-full rounded border border-slate-300 bg-white px-2 py-1 text-slate-800 placeholder:text-slate-400 focus:outline-none focus:border-violet-400";

  const linkClass = darkMode
    ? "text-sky-300 hover:text-sky-200"
    : "text-sky-600 hover:text-sky-500";

  const preClass = darkMode
    ? "mt-2 overflow-auto rounded border border-[#7B1FFF]/25 bg-[#07030F]/65 p-2 text-[11px] text-slate-300"
    : "mt-2 overflow-auto rounded border border-slate-200 bg-slate-100 p-2 text-[11px] text-slate-700";

  const warningText = darkMode ? "text-amber-300" : "text-amber-600";
  const errorText = darkMode ? "text-rose-300" : "text-rose-600";
  const mutedText = darkMode ? "text-slate-400" : "text-slate-500";
  const monoText = darkMode ? "font-mono text-slate-300" : "font-mono text-slate-600";

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
    <section className={panel}>
      <div className="mb-3 flex items-center justify-between">
        <h2 className={`text-sm font-semibold ${headingText}`}>Integration Diagnostics</h2>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onRefreshIntegrations}
            disabled={integrationsLoading}
          >
            Refresh
          </Button>
          {zendeskValidated ? (
            <Badge variant="success">Prompt Unlocked</Badge>
          ) : (
            <Badge variant="warning">Prompt Locked</Badge>
          )}
          {integrationsLoading ? (
            <span className={`text-xs ${mutedText}`}>Refreshing...</span>
          ) : null}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Apps Script card */}
        <div className={`${card} ${bodyText}`}>
          <p className={`mb-2 font-semibold ${headingText}`}>Apps Script</p>
          <p className={`mb-2 ${subtextColor}`}>
            Schema preflight sync runs automatically before generate, approve, and deploy actions.
          </p>
          <p className="mb-1">Configured: {boolToBadge(integrationsStatus?.appscript?.configured)}</p>
          <p className="mb-1">Health: {statusToBadge(integrationsStatus?.appscript?.health)}</p>
          {integrationsStatus?.appscript?.health_detail ? (
            <p className={errorText}>{integrationsStatus.appscript.health_detail}</p>
          ) : null}

          {stageMeta?.status ? (
            <div className={divider}>
              <p>Last stage write: {statusToBadge(stageMeta.status)}</p>
              {stageResult?.spreadsheet_url ? (
                <a
                  className={linkClass}
                  href={stageResult.spreadsheet_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open staging spreadsheet
                </a>
              ) : null}
              {stageResult?.object_rows_written ? (
                <pre className={preClass}>
                  {JSON.stringify(stageResult.object_rows_written, null, 2)}
                </pre>
              ) : null}
            </div>
          ) : null}

          {approvalSync?.status ? (
            <div className={divider}>
              <p>Last approval sync: {statusToBadge(approvalSync.status)}</p>
              {approvalSync?.result?.updated_records ? (
                <p className={`mt-1 ${mutedText}`}>
                  Updated rows: {approvalSync.result.updated_records}
                </p>
              ) : null}
              {approvalSync?.detail ? (
                <p className={errorText}>{approvalSync.detail}</p>
              ) : null}
            </div>
          ) : null}

          {plannerRoute || generatorRoute || geminiRoute ? (
            <div className={divider}>
              <p className={`mb-1 font-semibold ${headingText}`}>LLM orchestration</p>
              <p className={mutedText}>
                default: <span className={monoText}>{defaultProvider} / {defaultModel}</span>
                {defaultProvider === "gemini"
                  ? ` | Groq after ${Number(fallbackPolicy.gemini_max_retries || 0) + 1} failed attempts`
                  : ""}
              </p>
              {plannerRoute ? (
                <p className={mutedText}>
                  planner fallback: <span className={monoText}>{plannerRoute.model}</span> | strict schema:{" "}
                  {String(plannerRoute.strict_schema)}
                </p>
              ) : null}
              {generatorRoute ? (
                <p className={mutedText}>
                  generator fallback: <span className={monoText}>{generatorRoute.model}</span> | strict schema:{" "}
                  {String(generatorRoute.strict_schema)}
                </p>
              ) : null}
              {geminiRoute ? (
                <p className={mutedText}>
                  Gemini supervisor: <span className={monoText}>{geminiRoute.model || supervisorState.model}</span>{" "}
                  | status:{" "}
                  {supervisorState.available || geminiRoute.api_key_configured
                    ? "active"
                    : (supervisorState.availability_reason || "missing key")}
                  {" "} | auto patches:{" "}
                  {String(geminiRoute.auto_apply_patches ?? supervisorState.auto_apply_patches)}
                </p>
              ) : null}
              {supervisorState?.patch_counts ? (
                <p className={`mt-1 ${mutedText}`}>
                  Gemini patches: applied={Number(supervisorState.patch_counts.applied || 0)}, rejected={Number(supervisorState.patch_counts.rejected || 0)}
                </p>
              ) : null}
              {latestSupervisorReview ? (
                <p className={`mt-1 ${mutedText}`}>
                  Latest gate: raw={Number(latestSupervisorReview.raw_quality_score || 0).toFixed(2)} | effective={Number(latestSupervisorReview.effective_quality_score || 0).toFixed(2)} | {latestSupervisorReview.effective_approved ? "approved" : "rejected"}
                </p>
              ) : null}
              {Number(supervisorCallCounts.consolidated || 0) > 0 ? (
                <p className={`mt-1 ${mutedText}`}>
                  Reviews: consolidated={Number(supervisorCallCounts.consolidated || 0)}, retries={Number(supervisorCallCounts.retry || 0)}, fallbacks={Number(supervisorCallCounts.fallback || 0)}
                </p>
              ) : null}
              {supervisorRetryAttempts.length > 0 || supervisorBlockedChunks.length > 0 ? (
                <p className={`mt-1 ${supervisorBlockedChunks.length > 0 ? warningText : mutedText}`}>
                  Quality recovery: retries={supervisorRetryAttempts.length}, blocked chunks={supervisorBlockedChunks.length}
                </p>
              ) : null}
              {providerFailoverChunks > 0 ? (
                <p className={`mt-1 ${warningText}`}>
                  Provider recovery: {providerFailoverChunks} chunk(s) moved from Gemini to Groq.
                </p>
              ) : null}
              {narratorRoute?.enabled ? (
                <p className={`mt-1 ${mutedText}`}>
                  Progress narration: {narratorRoute.provider} / <span className={monoText}>{narratorRoute.model}</span>
                  {narrationState?.completed_calls !== undefined
                    ? ` | ${Number(narrationState.completed_calls || 0)} wave summaries`
                    : ""}
                </p>
              ) : null}
              {generateMetadata?.ambiguity_score !== undefined ? (
                <p className={`mt-1 ${mutedText}`}>
                  ambiguity score: {Number(generateMetadata.ambiguity_score).toFixed(2)} (threshold{" "}
                  {Number(generateMetadata.ambiguity_threshold || 0).toFixed(2)})
                </p>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* Zendesk Session card */}
        <div className={`${card} ${bodyText}`}>
          <p className={`mb-2 font-semibold ${headingText}`}>Zendesk Session</p>
          <p className="mb-2">
            Deploy endpoint:{" "}
            {integrationsStatus?.zendesk?.deploy_endpoint_enabled ? (
              <Badge variant="success">active</Badge>
            ) : (
              <Badge variant="warning">inactive</Badge>
            )}
          </p>
          <p className="mb-2">
            Session credentials:{" "}
            {zendeskValidated ? (
              <Badge variant="success">valid</Badge>
            ) : (
              <Badge variant="warning">not validated</Badge>
            )}
          </p>
          <form className="space-y-2" onSubmit={submitValidate}>
            <input
              value={subdomain}
              onChange={(event) => setSubdomain(event.target.value)}
              className={inputClass}
              placeholder="subdomain (example: acme)"
            />
            <input
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className={inputClass}
              placeholder="agent/admin email"
            />
            <input
              value={apiToken}
              onChange={(event) => setApiToken(event.target.value)}
              className={inputClass}
              placeholder="Zendesk API token"
              type="password"
            />
            <Button type="submit" size="sm" variant="outline" disabled={isValidatingZendesk}>
              {isValidatingZendesk ? "Validating..." : "Validate Zendesk Credentials"}
            </Button>
          </form>

          {zendeskValidationResult ? (
            <div className={divider}>
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
                <p className={`mt-1 ${mutedText}`}>
                  Target instance: {zendeskValidationResult.base_url}
                </p>
              ) : null}
              {zendeskValidationResult.authenticated_user ? (
                <p className="mt-1">
                  Authenticated as: {zendeskValidationResult.authenticated_user} (
                  {zendeskValidationResult.authenticated_user_role || "unknown role"})
                </p>
              ) : null}
            </div>
          ) : null}

          <div className={divider}>
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
              <p className={`mt-1 ${mutedText}`}>
                loaded items:{" "}
                {Object.values(contextStatus.catalogs).reduce(
                  (acc, entries) => acc + (Array.isArray(entries) ? entries.length : 0),
                  0
                )}{" "}
                | selected: {selectedContextCount}
              </p>
            ) : null}
            {contextStatus?.warnings?.length > 0 ? (
              <p className={`mt-1 ${warningText}`}>{contextStatus.warnings[0]}</p>
            ) : null}
          </div>
        </div>

        {/* Quality gates card */}
        <div className={`${card} ${bodyText}`}>
          <p className={`mb-2 font-semibold ${headingText}`}>Quality Gates</p>
          <p className="mb-1">
            Department coverage:{" "}
            {coverageState?.enabled ? (
              coverageState.status === "passed" ? (
                <Badge variant="success">passed</Badge>
              ) : (
                <Badge variant="warning">{coverageState.status || "pending"}</Badge>
              )
            ) : (
              <Badge variant="neutral">not active</Badge>
            )}
          </p>
          {coverageState?.totals ? (
            <p className={mutedText}>
              matched={Number(coverageState.totals.matched || 0)} / required={Number(coverageState.totals.required || 0)}
            </p>
          ) : null}
          {coverageDepartments.length > 0 ? (
            <div className="mt-2 max-h-40 overflow-auto">
              <table className="w-full text-left text-[11px]">
                <thead className={mutedText}>
                  <tr>
                    <th className="py-1 pr-2 font-medium">Department</th>
                    <th className="py-1 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {coverageDepartments.slice(0, 10).map((department) => (
                    <tr key={department.department}>
                      <td className="py-1 pr-2">{department.department}</td>
                      <td className="py-1">
                        {department.status === "passed" ? (
                          <Badge variant="success">passed</Badge>
                        ) : (
                          <Badge variant="warning">missing</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {coverageMissing.length > 0 ? (
            <div className={divider}>
              <p className={warningText}>Missing coverage: {coverageMissing.length}</p>
              <ul className="mt-1 space-y-1">
                {coverageMissing.slice(0, 5).map((item, index) => (
                  <li key={`${item.department}-${item.object_type}-${index}`} className={mutedText}>
                    {item.department}: {item.object_type} missing {Number(item.missing || 0)}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {fallbackEntries.length > 0 ? (
            <div className={divider}>
              <p className={mutedText}>
                Deterministic/template chunks:{" "}
                {fallbackEntries.map(([type, count]) => `${type}=${count}`).join(", ")}
              </p>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
