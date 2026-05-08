import { History, RefreshCw, Search, Workflow } from "lucide-react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";

function statusBadgeVariant(status) {
  if (["deployed", "approved", "preview_ready"].includes(status)) return "success";
  if (["deploy_failed", "failed", "validated_failed"].includes(status)) return "danger";
  if (["deployed_partial", "validated_warning"].includes(status)) return "warning";
  return "neutral";
}

function sectionLabel(key) {
  const labels = {
    groups: "Groups",
    ticket_forms: "Forms",
    brands: "Brands",
    help_centers: "Help Centers",
    categories: "Categories",
    sections: "Sections",
  };
  return labels[key] || key;
}

export default function Sidebar({
  productName = "CX Experts Assistant",
  historyItems = [],
  currentBatchId = null,
  historySearch = "",
  onHistorySearchChange,
  onSelectBatch,
  contextCatalog = null,
  contextLoading = false,
  selectedContextKeys = {},
  onToggleContext,
  onRefreshContext,
}) {
  const normalizedSearch = historySearch.trim().toLowerCase();
  const filteredHistory = historyItems.filter((item) => {
    if (!normalizedSearch) return true;
    return (
      String(item.batch_id || "").toLowerCase().includes(normalizedSearch)
      || String(item.prompt_preview || "").toLowerCase().includes(normalizedSearch)
      || String(item.status || "").toLowerCase().includes(normalizedSearch)
    );
  });

  return (
    <aside className="hidden w-80 flex-col border-r border-slate-800 bg-[#161a21] p-3 text-slate-300 lg:flex">
      <div className="mb-3 px-2 text-lg font-semibold text-white">{productName}</div>

      <div className="mb-3 rounded-md border border-slate-700 bg-slate-900/50 p-2">
        <label className="mb-1 block px-1 text-[11px] uppercase tracking-wide text-slate-400">
          Search History
        </label>
        <div className="flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900 px-2">
          <Search size={14} className="text-slate-500" />
          <input
            value={historySearch}
            onChange={(event) => onHistorySearchChange?.(event.target.value)}
            placeholder="batch id or prompt..."
            className="h-8 w-full bg-transparent text-sm text-slate-200 outline-none"
          />
        </div>
      </div>

      <div className="mb-3 flex-1 overflow-auto rounded-md border border-slate-800 bg-slate-950/40 p-2">
        <div className="mb-2 flex items-center gap-2 text-xs text-slate-400">
          <History size={14} />
          Recent Batches
        </div>
        <div className="space-y-1">
          {filteredHistory.slice(0, 20).map((item) => (
            <button
              key={item.batch_id}
              type="button"
              onClick={() => onSelectBatch?.(item.batch_id)}
              className={`w-full rounded-md border px-2 py-2 text-left ${
                currentBatchId === item.batch_id
                  ? "border-sky-700 bg-sky-950/20"
                  : "border-slate-800 bg-slate-900/30 hover:bg-slate-800/70"
              }`}
            >
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="font-mono text-[11px] text-slate-300">{item.batch_id}</span>
                <Badge variant={statusBadgeVariant(item.status)}>{item.status}</Badge>
              </div>
              <p className="text-xs text-slate-400">{item.prompt_preview || "No prompt preview."}</p>
            </button>
          ))}
          {filteredHistory.length === 0 ? (
            <p className="px-1 py-2 text-xs text-slate-500">No matching batches.</p>
          ) : null}
        </div>
      </div>

      <div className="mt-3 rounded-md border border-slate-700 bg-slate-900/50 p-2">
        <div className="mb-2 flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs text-slate-300">
            <Workflow size={14} />
            Active Context
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-xs"
            onClick={onRefreshContext}
            disabled={contextLoading}
          >
            <RefreshCw size={12} className={contextLoading ? "animate-spin" : ""} />
          </Button>
        </div>
        {contextLoading ? <p className="text-xs text-slate-500">Refreshing context...</p> : null}
        {!contextLoading && !contextCatalog ? (
          <p className="text-xs text-slate-500">Validate session to load brands, groups, forms, and help center context.</p>
        ) : null}
        {contextCatalog ? (
          <div className="max-h-48 space-y-2 overflow-auto pr-1">
            {Object.entries(contextCatalog).map(([catalogKey, entries]) => (
              <div key={catalogKey}>
                <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">
                  {sectionLabel(catalogKey)}
                </p>
                <div className="space-y-1">
                  {(entries || []).slice(0, 6).map((entry) => {
                    const selectedKey = `${entry.object_type}:${entry.id}`;
                    const selected = Boolean(selectedContextKeys[selectedKey]);
                    return (
                      <button
                        key={selectedKey}
                        type="button"
                        onClick={() => onToggleContext?.(entry)}
                        className={`w-full rounded border px-2 py-1 text-left text-xs ${
                          selected
                            ? "border-emerald-700 bg-emerald-950/20 text-emerald-200"
                            : "border-slate-800 bg-slate-900/30 text-slate-300 hover:bg-slate-800/70"
                        }`}
                      >
                        {entry.name}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </aside>
  );
}
