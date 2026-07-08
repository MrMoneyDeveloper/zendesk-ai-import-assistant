import { Badge } from "../ui/badge";

const statusToVariant = {
  preview_ready: "success",
  clarification_required: "warning",
  validated_warning: "warning",
  validated_failed: "danger",
  deploying: "warning",
  deployed: "success",
  deployed_partial: "warning",
  deploy_failed: "danger",
  failed: "danger",
};

const terminalStatuses = new Set([
  "approved",
  "partially_approved",
  "preview_ready",
  "clarification_required",
  "validated_passed",
  "validated_warning",
  "validated_failed",
  "deploying",
  "deployed",
  "deployed_partial",
  "deploy_failed",
]);

function phaseBadge(done, failed = false) {
  if (failed) return <Badge variant="danger">error</Badge>;
  if (done) return <Badge variant="success">done</Badge>;
  return <Badge variant="warning">pending</Badge>;
}

export default function StatusRibbon({
  darkMode = false,
  batchId,
  status,
  testResult,
  jobData,
  generateMetadata,
  approvalResult,
  deployEnabled = false,
  deployTarget = "",
  onExistingMode = "create_new",
}) {
  const variant = statusToVariant[status] || "neutral";
  const stageStatus = generateMetadata?.staging?.status;
  const approvalStatus = approvalResult?.metadata?.approval_sync?.status;
  const approvedCount = approvalResult?.summary?.approved || 0;

  const phaseStageDone = stageStatus === "ok" || terminalStatuses.has(status);
  const phaseStageFailed = stageStatus === "error" || status === "failed";
  const phaseApprovalDone =
    approvalStatus === "ok" || status === "approved" || status === "partially_approved";
  const phaseApprovalFailed = approvalStatus === "error";

  const latestHistory = [...(jobData?.status_history || [])].slice(-4).reverse();

  // Theme-aware classes
  const wrapper = darkMode
    ? "mb-6 rounded-lg border border-[#7B1FFF]/30 bg-[#120522]/70 px-4 py-3"
    : "mb-6 rounded-lg border border-slate-200 bg-white px-4 py-3";

  const labelText = darkMode ? "text-slate-300" : "text-slate-500";
  const monoText = darkMode ? "font-mono text-xs text-slate-400" : "font-mono text-xs text-slate-500";
  const apiText = darkMode ? "text-xs text-slate-300" : "text-xs text-slate-600";

  const pipelineBanner = darkMode
    ? "mt-3 rounded-md border border-amber-700/40 bg-amber-950/20 p-3 text-xs text-amber-200"
    : "mt-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800";

  const phaseCard = darkMode
    ? "rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-2"
    : "rounded border border-slate-200 bg-slate-50 p-2";

  const phaseHeading = darkMode ? "mb-1 font-semibold text-slate-200" : "mb-1 font-semibold text-slate-800";
  const phaseBody = darkMode ? "text-xs text-slate-300" : "text-xs text-slate-600";
  const mutedText = darkMode ? "mt-1 text-slate-400" : "mt-1 text-slate-500";

  const historyCard = darkMode
    ? "mt-3 rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-2 text-xs text-slate-300"
    : "mt-3 rounded border border-slate-200 bg-slate-50 p-2 text-xs text-slate-600";

  const historyHeading = darkMode ? "mb-2 font-semibold text-slate-200" : "mb-2 font-semibold text-slate-800";
  const historyMono = darkMode ? "font-mono text-slate-400" : "font-mono text-slate-400";

  return (
    <div className={wrapper}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className={`text-sm ${labelText}`}>Batch:</span>
          <span className={monoText}>{batchId || "Not started"}</span>
          <Badge variant={variant}>{status || "idle"}</Badge>
        </div>
        {testResult && (
          <div className={apiText}>
            API: {testResult.provider} | {testResult.model} | {testResult.status}
          </div>
        )}
      </div>

      <div className={pipelineBanner}>
        <p className="font-semibold">Pipeline mode</p>
        <p className="mt-1">
          Approved records can be deployed to Zendesk from the Preview Workspace after staging and approval.
          {deployTarget ? (
            <>
              {" "}Current target: <span className="font-mono">{deployTarget}</span>.
            </>
          ) : null}
        </p>
      </div>

      <div className={`mt-3 grid gap-2 lg:grid-cols-3 ${phaseBody}`}>
        <div className={phaseCard}>
          <p className={phaseHeading}>1. Stage to Sheet</p>
          {phaseBadge(phaseStageDone, phaseStageFailed)}
        </div>
        <div className={phaseCard}>
          <p className={phaseHeading}>2. Save Approval</p>
          {phaseBadge(phaseApprovalDone, phaseApprovalFailed)}
          {phaseApprovalDone ? (
            <p className={mutedText}>approved records: {approvedCount}</p>
          ) : null}
        </div>
        <div className={phaseCard}>
          <p className={phaseHeading}>3. Deploy to Zendesk</p>
          {deployEnabled ? <Badge variant="success">enabled</Badge> : <Badge variant="warning">inactive</Badge>}
          {deployEnabled ? <p className={mutedText}>on existing: {onExistingMode}</p> : null}
        </div>
      </div>

      {latestHistory.length > 0 ? (
        <div className={historyCard}>
          <p className={historyHeading}>Recent Activity</p>
          <div className="space-y-1">
            {latestHistory.map((entry, idx) => (
              <p key={`${entry.status}-${entry.at}-${idx}`}>
                <span className={historyMono}>{entry.at}</span> |{" "}
                <span className="font-semibold">{entry.status}</span> | {entry.message}
              </p>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
