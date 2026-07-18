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

function humanize(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatSnapshotValue(value) {
  if (value === null || value === undefined || value === "") return "Not set";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    const labels = value.map((item) => {
      if (item && typeof item === "object") {
        return item.name || item.title || item.value || item.id || "Configured item";
      }
      return String(item);
    });
    return labels.join(", ") || "None";
  }
  if (typeof value === "object") {
    return Object.entries(value)
      .slice(0, 6)
      .map(([key, item]) => `${humanize(key)}: ${formatSnapshotValue(item)}`)
      .join("; ");
  }
  const text = String(value).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  return text.length > 180 ? `${text.slice(0, 177)}...` : text;
}

function snapshotDetails(target) {
  const snapshot = target?.snapshot || {};
  const conditionSource = snapshot.conditions && typeof snapshot.conditions === "object"
    ? snapshot.conditions
    : snapshot;
  const conditions = [
    ...(Array.isArray(conditionSource.all)
      ? conditionSource.all.map((item) => ({ ...item, scope: "All" }))
      : []),
    ...(Array.isArray(conditionSource.any)
      ? conditionSource.any.map((item) => ({ ...item, scope: "Any" }))
      : []),
  ];
  const actions = Array.isArray(snapshot.actions) ? snapshot.actions : [];
  const properties = [
    ["Status", snapshot.active],
    ["Type", snapshot.type],
    ["Category", snapshot.category_id],
    ["Section", snapshot.section_id],
    ["Locale", snapshot.locale],
    ["Draft", snapshot.draft],
    ["Ticket fields", snapshot.ticket_field_ids],
    ["Options", snapshot.custom_field_options],
    ["Output columns", snapshot.output?.columns],
    ["Description", snapshot.description],
    ["Body", snapshot.body],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
  return { conditions, actions, properties };
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
  const [catalogFilter, setCatalogFilter] = useState("");
  const catalogs = useMemo(() => contextStatus?.catalogs || {}, [contextStatus?.catalogs]);
  const syncId = contextStatus?.sync_id || "";
  const fetchedAt = contextStatus?.fetched_at;
  const syncComplete = Boolean(contextStatus?.complete);

  const editableGroups = useMemo(() => {
    return Object.entries(catalogs)
      .map(([catalogKey, entries]) => ({
        catalogKey,
        label: CATALOG_LABELS[catalogKey] || catalogKey.replaceAll("_", " "),
        entries: (entries || [])
          .filter((entry) => entry?.editable && entry?.snapshot)
          .sort((a, b) => String(a.name).localeCompare(String(b.name))),
      }))
      .filter((group) => group.entries.length > 0);
  }, [catalogs]);

  const requestedCatalogKey = catalogFilter || updateTarget?.catalog_key || "";
  const selectedCatalogKey = editableGroups.some(
    (group) => group.catalogKey === requestedCatalogKey
  ) ? requestedCatalogKey : "";
  const selectedGroup = editableGroups.find((group) => group.catalogKey === selectedCatalogKey);
  const visibleTargets = useMemo(() => {
    const query = search.trim().toLowerCase();
    const entries = selectedGroup?.entries || [];
    const filtered = entries.filter(
      (entry) => !query || `${entry.name} ${entry.id}`.toLowerCase().includes(query)
    );
    if (
      updateTarget
      && updateTarget.catalog_key === selectedCatalogKey
      && !filtered.some((entry) => String(entry.id) === String(updateTarget.id))
    ) {
      return [updateTarget, ...filtered];
    }
    return filtered;
  }, [search, selectedCatalogKey, selectedGroup, updateTarget]);

  const targetByKey = useMemo(() => {
    const map = new Map();
    editableGroups.forEach((group) => {
      group.entries.forEach((entry) => map.set(`${entry.object_type}:${entry.id}`, entry));
    });
    return map;
  }, [editableGroups]);

  const editableCount = Object.values(catalogs).reduce(
    (total, entries) => total + (entries || []).filter((entry) => entry?.editable && entry?.snapshot).length,
    0
  );
  const totalCount = Object.values(catalogs).reduce(
    (total, entries) => total + (entries || []).length,
    0
  );
  const targetCounts = snapshotCounts(updateTarget);
  const targetDetails = snapshotDetails(updateTarget);
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
                {editableCount} editable objects across {editableGroups.length} configuration types.
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

          <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,0.7fr)_minmax(0,1.15fr)_minmax(0,0.85fr)]">
            <label className="min-w-0">
              <span className={`mb-1 block text-[11px] font-medium ${muted}`}>Object type</span>
              <select
                aria-label="Zendesk object type"
                value={selectedCatalogKey}
                onChange={(event) => {
                  setCatalogFilter(event.target.value);
                  setSearch("");
                  onUpdateTargetChange?.(null);
                }}
                disabled={contextLoading || editableGroups.length === 0}
                className={`h-10 w-full rounded-md border px-3 text-sm outline-none disabled:opacity-60 ${darkMode ? "border-[#7B1FFF]/35 bg-[#120522]/70 text-slate-100" : "border-slate-300 bg-white text-slate-800"}`}
              >
                <option value="">Choose a type...</option>
                {editableGroups.map((group) => (
                  <option key={group.catalogKey} value={group.catalogKey}>
                    {group.label} ({group.entries.length})
                  </option>
                ))}
              </select>
            </label>

            <label className="min-w-0">
              <span className={`mb-1 block text-[11px] font-medium ${muted}`}>Object to update</span>
              <select
                aria-label="Existing Zendesk object"
                value={selectedKey}
                onChange={(event) => onUpdateTargetChange?.(targetByKey.get(event.target.value) || null)}
                disabled={contextLoading || !selectedCatalogKey || visibleTargets.length === 0}
                className={`h-10 w-full rounded-md border px-3 text-sm outline-none disabled:opacity-60 ${darkMode ? "border-[#7B1FFF]/35 bg-[#120522]/70 text-slate-100" : "border-slate-300 bg-white text-slate-800"}`}
              >
                <option value="">
                  {!selectedCatalogKey
                    ? "Choose an object type first..."
                    : visibleTargets.length === 0
                      ? "No matching objects"
                      : "Choose an existing object..."}
                </option>
                {visibleTargets.map((entry) => (
                  <option key={`${entry.object_type}:${entry.id}`} value={`${entry.object_type}:${entry.id}`}>
                    {entry.name} (ID {entry.id})
                  </option>
                ))}
              </select>
            </label>

            <label className="min-w-0">
              <span className={`mb-1 block text-[11px] font-medium ${muted}`}>Filter this type</span>
              <input
                aria-label="Filter synchronized objects"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Name or Zendesk ID"
                disabled={!selectedCatalogKey}
                className={`h-10 w-full rounded-md border px-3 text-sm outline-none disabled:opacity-60 ${darkMode ? "border-[#7B1FFF]/35 bg-[#120522]/70 text-slate-100" : "border-slate-300 bg-white text-slate-800"}`}
              />
            </label>
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
            <div className={`mt-4 border-t pt-4 ${darkMode ? "border-violet-500/25" : "border-slate-200"}`}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="text-xs font-semibold">Gemini starting point</p>
                  <p className="mt-1 break-words text-sm font-medium">{updateTarget.name}</p>
                </div>
                <div className={`text-right text-[11px] ${muted}`}>
                  <p>{humanize(updateTarget.catalog_key || updateTarget.object_type)}</p>
                  <p>Zendesk ID {updateTarget.id}</p>
                </div>
              </div>
              <div className={`mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] ${muted}`}>
                <span>{targetCounts.conditions} current conditions</span>
                <span>{targetCounts.actions} current actions</span>
                <span>{updateTarget.snapshot_hash ? "Snapshot integrity recorded" : "Snapshot hash unavailable"}</span>
              </div>

              <div className="mt-4 grid gap-5 md:grid-cols-2">
                <div className="min-w-0">
                  <p className={`text-[11px] font-semibold uppercase ${muted}`}>Current conditions</p>
                  {targetDetails.conditions.length > 0 ? (
                    <div className="mt-2 space-y-2">
                      {targetDetails.conditions.slice(0, 8).map((condition, index) => (
                        <p key={`${condition.scope}-${condition.field}-${index}`} className="text-xs leading-5">
                          <span className={`mr-2 font-medium ${muted}`}>{condition.scope}</span>
                          {humanize(condition.field)} {humanize(condition.operator || "is").toLowerCase()} {formatSnapshotValue(condition.value)}
                        </p>
                      ))}
                      {targetDetails.conditions.length > 8 ? (
                        <p className={`text-[11px] ${muted}`}>+{targetDetails.conditions.length - 8} more conditions</p>
                      ) : null}
                    </div>
                  ) : (
                    <p className={`mt-2 text-xs ${muted}`}>No condition rules on this object.</p>
                  )}
                </div>

                <div className="min-w-0">
                  <p className={`text-[11px] font-semibold uppercase ${muted}`}>Current actions and settings</p>
                  {targetDetails.actions.length > 0 ? (
                    <div className="mt-2 space-y-2">
                      {targetDetails.actions.slice(0, 8).map((action, index) => (
                        <p key={`${action.field}-${index}`} className="text-xs leading-5">
                          <span className={`mr-2 font-medium ${muted}`}>{humanize(action.field)}</span>
                          {formatSnapshotValue(action.value)}
                        </p>
                      ))}
                      {targetDetails.actions.length > 8 ? (
                        <p className={`text-[11px] ${muted}`}>+{targetDetails.actions.length - 8} more actions</p>
                      ) : null}
                    </div>
                  ) : targetDetails.properties.length > 0 ? (
                    <div className="mt-2 space-y-2">
                      {targetDetails.properties.slice(0, 8).map(([label, value]) => (
                        <p key={label} className="text-xs leading-5">
                          <span className={`mr-2 font-medium ${muted}`}>{label}</span>
                          {formatSnapshotValue(value)}
                        </p>
                      ))}
                    </div>
                  ) : (
                    <p className={`mt-2 text-xs ${muted}`}>No action or setting details were returned.</p>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <p className="mt-3 text-xs text-amber-500">Choose one synchronized object before entering update instructions.</p>
          )}
        </div>
      ) : null}
    </section>
  );
}
