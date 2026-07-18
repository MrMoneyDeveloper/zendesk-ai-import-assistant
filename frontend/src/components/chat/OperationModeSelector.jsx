import { useMemo, useState } from "react";
import {
  CheckCircle2,
  CircleAlert,
  Database,
  PencilLine,
  Plus,
  RefreshCw,
} from "lucide-react";

const CATALOG_LABELS = {
  brands: "Brands",
  groups: "Groups",
  ticket_forms: "Ticket forms",
  triggers: "Triggers",
  automations: "Automations",
  macros: "Macros",
  views: "Views",
  ticket_fields: "Ticket fields",
  categories: "Help Center categories",
  sections: "Help Center sections",
  articles: "Help Center articles",
  sla_policies: "SLA policies",
  schedules: "Business schedules",
  user_fields: "User fields",
  organization_fields: "Organization fields",
  custom_objects: "Custom objects",
};

function snapshotCounts(target) {
  const snapshot = target?.snapshot || {};
  const conditionSource = snapshot.conditions && typeof snapshot.conditions === "object"
    ? snapshot.conditions
    : snapshot;
  const conditions = [
    ...(Array.isArray(conditionSource.all) ? conditionSource.all : []),
    ...(Array.isArray(conditionSource.any) ? conditionSource.any : []),
  ];
  const actions = Array.isArray(snapshot.actions) ? snapshot.actions : [];
  return { conditions: conditions.length, actions: actions.length };
}

export default function OperationModeSelector({
  darkMode = false,
  operationMode,
  onOperationModeChange,
  contextStatus,
  contextLoading = false,
  onRefreshContext,
  updateTarget,
  onUpdateTargetChange,
}) {
  const [search, setSearch] = useState("");
  const catalogs = useMemo(() => contextStatus?.catalogs || {}, [contextStatus?.catalogs]);
  const syncId = contextStatus?.sync_id || "";
  const fetchedAt = contextStatus?.fetched_at;
  const syncComplete = Boolean(contextStatus?.complete);

  const groupedOptions = useMemo(() => {
    const query = search.trim().toLowerCase();
    return Object.entries(catalogs)
      .map(([catalogKey, entries]) => ({
        catalogKey,
        label: CATALOG_LABELS[catalogKey] || catalogKey.replaceAll("_", " "),
        entries: (entries || [])
          .filter((entry) => entry?.editable && entry?.snapshot)
          .filter((entry) => !query || `${entry.name} ${entry.id} ${catalogKey}`.toLowerCase().includes(query))
          .sort((a, b) => String(a.name).localeCompare(String(b.name))),
      }))
      .filter((group) => group.entries.length > 0);
  }, [catalogs, search]);

  const targetByKey = useMemo(() => {
    const map = new Map();
    groupedOptions.forEach((group) => {
      group.entries.forEach((entry) => map.set(`${entry.object_type}:${entry.id}`, entry));
    });
    return map;
  }, [groupedOptions]);

  const editableCount = Object.values(catalogs).reduce(
    (total, entries) => total + (entries || []).filter((entry) => entry?.editable).length,
    0
  );
  const totalCount = Object.values(catalogs).reduce(
    (total, entries) => total + (entries || []).length,
    0
  );
  const populatedCatalogs = Object.values(catalogs).filter((entries) => (entries || []).length > 0).length;
  const targetCounts = snapshotCounts(updateTarget);
  const selectedKey = updateTarget ? `${updateTarget.object_type}:${updateTarget.id}` : "";
  const surface = darkMode
    ? "border-[#7B1FFF]/28 bg-[#120522]/55 text-slate-100"
    : "border-slate-200 bg-white text-slate-900";
  const muted = darkMode ? "text-[#B9A7D9]" : "text-slate-500";

  return (
    <section className={`border-y px-0 py-5 ${surface}`} aria-labelledby="operation-mode-heading">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 sm:px-5">
        <div>
          <h2 id="operation-mode-heading" className="text-sm font-semibold">Choose what this run may change</h2>
          <p className={`mt-1 text-xs leading-5 ${muted}`}>
            Select a mode before entering instructions. Update mode is restricted to one synchronized Zendesk object.
          </p>
        </div>
        <div className={`flex items-center gap-2 text-xs ${muted}`}>
          <Database size={15} />
          {contextLoading ? "Synchronizing instance..." : `${totalCount} objects synchronized`}
        </div>
      </div>

      <div className="mt-4 grid gap-3 px-4 sm:grid-cols-2 sm:px-5">
        <button
          type="button"
          onClick={() => onOperationModeChange?.("create")}
          aria-pressed={operationMode === "create"}
          className={`min-h-[94px] rounded-lg border p-4 text-left transition ${
            operationMode === "create"
              ? "border-emerald-500 bg-emerald-500/10"
              : darkMode
                ? "border-[#7B1FFF]/25 bg-[#07030F]/40 hover:border-[#7B1FFF]/55"
                : "border-slate-200 bg-slate-50 hover:border-violet-300"
          }`}
        >
          <span className="flex items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-emerald-500/15 text-emerald-500">
              <Plus size={19} />
            </span>
            <span>
              <span className="block text-sm font-semibold">Create new</span>
              <span className={`mt-1 block text-xs leading-5 ${muted}`}>Build new configuration without replacing an existing object.</span>
            </span>
          </span>
        </button>

        <button
          type="button"
          onClick={() => onOperationModeChange?.("update")}
          aria-pressed={operationMode === "update"}
          className={`min-h-[94px] rounded-lg border p-4 text-left transition ${
            operationMode === "update"
              ? "border-violet-500 bg-violet-500/12"
              : darkMode
                ? "border-[#7B1FFF]/25 bg-[#07030F]/40 hover:border-[#7B1FFF]/55"
                : "border-slate-200 bg-slate-50 hover:border-violet-300"
          }`}
        >
          <span className="flex items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-violet-500/15 text-violet-500">
              <PencilLine size={18} />
            </span>
            <span>
              <span className="block text-sm font-semibold">Update existing</span>
              <span className={`mt-1 block text-xs leading-5 ${muted}`}>Select one exact object, compare its current logic, then replace it by ID.</span>
            </span>
          </span>
        </button>
      </div>

      {operationMode === "update" ? (
        <div className={`mx-4 mt-4 rounded-lg border p-4 sm:mx-5 ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/45" : "border-slate-200 bg-slate-50"}`}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs font-semibold">Select the exact update target</p>
              <p className={`mt-1 text-[11px] ${muted}`}>
                {editableCount} editable objects across {populatedCatalogs} populated configuration types.
              </p>
            </div>
            <button
              type="button"
              onClick={onRefreshContext}
              disabled={contextLoading}
              className={`inline-flex h-9 items-center gap-2 rounded-md border px-3 text-xs font-medium disabled:opacity-50 ${darkMode ? "border-[#7B1FFF]/35 text-violet-100" : "border-slate-300 text-slate-700"}`}
            >
              <RefreshCw size={14} className={contextLoading ? "animate-spin" : ""} />
              Sync now
            </button>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,0.75fr)_minmax(0,1.25fr)]">
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Filter by type, name, or Zendesk ID"
              className={`h-10 w-full rounded-md border px-3 text-sm outline-none ${darkMode ? "border-[#7B1FFF]/35 bg-[#120522]/70 text-slate-100" : "border-slate-300 bg-white text-slate-800"}`}
            />
            <select
              aria-label="Existing Zendesk object"
              value={selectedKey}
              onChange={(event) => onUpdateTargetChange?.(targetByKey.get(event.target.value) || null)}
              disabled={contextLoading || groupedOptions.length === 0}
              className={`h-10 w-full rounded-md border px-3 text-sm outline-none disabled:opacity-60 ${darkMode ? "border-[#7B1FFF]/35 bg-[#120522]/70 text-slate-100" : "border-slate-300 bg-white text-slate-800"}`}
            >
              <option value="">Choose an existing object...</option>
              {groupedOptions.map((group) => (
                <optgroup key={group.catalogKey} label={group.label}>
                  {group.entries.map((entry) => (
                    <option key={`${entry.object_type}:${entry.id}`} value={`${entry.object_type}:${entry.id}`}>
                      {entry.name} (ID {entry.id})
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>

          <div className={`mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] ${muted}`}>
            <span className="inline-flex items-center gap-1.5">
              {syncComplete ? <CheckCircle2 size={13} className="text-emerald-500" /> : <CircleAlert size={13} className="text-amber-500" />}
              {syncComplete ? "Core catalog fully paginated" : "Partial catalog; review sync warnings"}
            </span>
            <span>{fetchedAt ? `Synced ${new Date(fetchedAt).toLocaleTimeString()}` : "Waiting for sync"}</span>
            <span>{syncId ? `Snapshot ${syncId.slice(-8)}` : "No snapshot ID"}</span>
          </div>

          {updateTarget ? (
            <details className={`mt-4 rounded-md border px-3 py-2 ${darkMode ? "border-violet-500/25 bg-violet-500/10" : "border-violet-200 bg-white"}`}>
              <summary className="cursor-pointer list-none text-xs font-semibold">
                {updateTarget.name} <span className={`font-normal ${muted}`}>ID {updateTarget.id}</span>
              </summary>
              <div className={`mt-2 grid gap-2 text-xs sm:grid-cols-3 ${muted}`}>
                <p><span className="font-medium">Type:</span> {(updateTarget.catalog_key || updateTarget.object_type).replaceAll("_", " ")}</p>
                <p><span className="font-medium">Conditions:</span> {targetCounts.conditions}</p>
                <p><span className="font-medium">Actions:</span> {targetCounts.actions}</p>
              </div>
            </details>
          ) : (
            <p className="mt-3 text-xs text-amber-500">Choose one synchronized object before entering update instructions.</p>
          )}
        </div>
      ) : null}
    </section>
  );
}
