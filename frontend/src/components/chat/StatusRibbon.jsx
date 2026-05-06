import { Badge } from "../ui/badge";

const statusToVariant = {
  preview_ready: "success",
  validated_warning: "warning",
  validated_failed: "danger",
  failed: "danger",
};

export default function StatusRibbon({ batchId, status, testResult }) {
  const variant = statusToVariant[status] || "neutral";
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3">
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
  );
}
