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

  return (
    <div className="mb-6 rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="text-sm text-slate-300">Batch:</span>
          <span className="font-mono text-xs text-slate-400">{batchId || "Not started"}</span>
          <Badge variant={variant}>{status || "idle"}</Badge>
        </div>
        {testResult && (
          <div className="text-xs text-slate-300">
            API: {testResult.provider} | {testResult.model} | {testResult.status}
          </div>
        )}
      </div>

      <div className="mt-3 rounded-md border border-amber-700/40 bg-amber-950/20 p-3 text-xs text-amber-200">
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

      <div className="mt-3 grid gap-2 text-xs text-slate-300 lg:grid-cols-3">
        <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
          <p className="mb-1 font-semibold text-slate-200">1. Stage to Sheet</p>
          {phaseBadge(phaseStageDone, phaseStageFailed)}
        </div>
        <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
          <p className="mb-1 font-semibold text-slate-200">2. Save Approval</p>
          {phaseBadge(phaseApprovalDone, phaseApprovalFailed)}
          {phaseApprovalDone ? (
            <p className="mt-1 text-slate-400">approved records: {approvedCount}</p>
          ) : null}
        </div>
        <div className="rounded border border-slate-800 bg-slate-950/60 p-2">
          <p className="mb-1 font-semibold text-slate-200">3. Deploy to Zendesk</p>
          {deployEnabled ? <Badge variant="success">enabled</Badge> : <Badge variant="warning">inactive</Badge>}
          {deployEnabled ? <p className="mt-1 text-slate-400">on existing: {onExistingMode}</p> : null}
        </div>
      </div>

      {latestHistory.length > 0 ? (
        <div className="mt-3 rounded border border-slate-800 bg-slate-950/60 p-2 text-xs text-slate-300">
          <p className="mb-2 font-semibold text-slate-200">Recent Activity</p>
          <div className="space-y-1">
            {latestHistory.map((entry, idx) => (
              <p key={`${entry.status}-${entry.at}-${idx}`}>
                <span className="font-mono text-slate-400">{entry.at}</span> |{" "}
                <span className="font-semibold">{entry.status}</span> | {entry.message}
              </p>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
