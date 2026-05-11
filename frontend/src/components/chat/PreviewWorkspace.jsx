import { useMemo, useState } from "react";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader } from "../ui/card";

const columnHelper = createColumnHelper();
const DECISION_OPTIONS = [
  { value: "approved", label: "Approve" },
  { value: "skipped", label: "Skip" },
  { value: "edit_later", label: "Edit later" },
];

function statusBadge(status) {
  if (status === "passed") return <Badge variant="success">passed</Badge>;
  if (status === "warning") return <Badge variant="warning">warning</Badge>;
  return <Badge variant="danger">failed</Badge>;
}

function deployBadge(value) {
  if (value === "deployed") return <Badge variant="success">deployed</Badge>;
  if (value === "failed") return <Badge variant="danger">failed</Badge>;
  if (value === "skipped") return <Badge variant="warning">skipped</Badge>;
  return <Badge variant="neutral">pending</Badge>;
}

function DecisionPills({ value, onChange, deployable = true }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {DECISION_OPTIONS.map((option) => {
        const active = value === option.value;
        const disabled = option.value === "approved" && !deployable;
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => !disabled && onChange(option.value)}
            disabled={disabled}
            className={`rounded-full border px-2 py-1 text-[11px] transition ${
              active
                ? "border-[#7B1FFF]/70 bg-[#7B1FFF]/24 text-[#F4EEFF]"
                : "border-[#7B1FFF]/30 bg-[#07030F]/40 text-[#B9A7D9] hover:bg-[#7B1FFF]/14"
            } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
          >
            {option.label}
          </button>
        );
      })}
      {value === "pending_review" ? (
        <span className="rounded-full border border-[#7B1FFF]/25 bg-[#07030F]/35 px-2 py-1 text-[10px] text-slate-400">
          Pending
        </span>
      ) : null}
    </div>
  );
}

export default function PreviewWorkspace({
  previewData,
  generatedData,
  decisions,
  onDecisionChange,
  onApproveDecisions,
  onApproveAndDeploy,
  isApproving,
  onDeployToZendesk,
  isDeploying,
  deployResult,
}) {
  const [recordSearch, setRecordSearch] = useState("");
  const [showTechnical, setShowTechnical] = useState(false);
  const records = useMemo(() => previewData?.records || [], [previewData?.records]);

  const filteredRecords = useMemo(() => {
    const query = recordSearch.trim().toLowerCase();
    if (!query) return records;
    return records.filter((row) => (
      String(row.record_id || "").toLowerCase().includes(query)
      || String(row.title || "").toLowerCase().includes(query)
      || String(row.object_type || "").toLowerCase().includes(query)
      || String(row.validation_status || "").toLowerCase().includes(query)
      || String(row.deployment_status || "").toLowerCase().includes(query)
    ));
  }, [records, recordSearch]);

  const columns = useMemo(
    () => [
      columnHelper.accessor("record_id", {
        header: "Record",
        cell: (info) => <span className="font-mono text-xs text-slate-300">{info.getValue()}</span>,
      }),
      columnHelper.accessor("title", {
        header: "Title",
        cell: (info) => <span className="text-slate-100">{info.getValue()}</span>,
      }),
      columnHelper.accessor("object_type", {
        header: "Type",
        cell: (info) => <Badge>{info.getValue()}</Badge>,
      }),
      columnHelper.accessor("validation_status", {
        header: "Validation",
        cell: (info) => statusBadge(info.getValue()),
      }),
      columnHelper.display({
        id: "decision",
        header: "Decision",
        cell: ({ row }) => {
          const recordId = row.original.record_id;
          const value = decisions[recordId] || row.original.import_decision || "pending_review";
          return (
            <DecisionPills
              value={value}
              deployable={Boolean(row.original.deployable)}
              onChange={(nextDecision) => onDecisionChange(recordId, nextDecision)}
            />
          );
        },
      }),
      columnHelper.accessor("deployment_status", {
        header: "Deploy",
        cell: (info) => deployBadge(info.getValue() || "pending"),
      }),
      columnHelper.accessor("zendesk_object_id", {
        header: "Zendesk ID",
        cell: (info) => <span className="font-mono text-xs text-slate-300">{info.getValue() || "-"}</span>,
      }),
      columnHelper.accessor("execution_message", {
        header: "Execution",
        cell: (info) => <span className="text-xs text-slate-300">{info.getValue() || "-"}</span>,
      }),
    ],
    [decisions, onDecisionChange]
  );

  const table = useReactTable({
    data: filteredRecords,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  if (!previewData) {
    return null;
  }

  const planning = previewData.planning_summary || {};
  const validation = previewData.validation_summary || {};
  const generatedCounts = generatedData?.generated_counts || previewData.generated_counts || {};
  const generatedSummary = Object.entries(generatedCounts)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" | ");

  const total = records.length;
  const approved = records.filter((row) => (decisions[row.record_id] || row.import_decision) === "approved").length;
  const warnings = Number(validation.warnings || 0);
  const blocked = Number(validation.blocked || 0);
  const duplicateWarningRows = records.filter((row) =>
    (row.warnings || []).some((warning) => String(warning).toLowerCase().includes("duplicate candidate"))
  );

  return (
    <Card className="mt-8 border-[#7B1FFF]/30 bg-[#120522]/70">
      <CardHeader className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-slate-200">Review and Confirm</p>
          <p className="mt-1 text-xs text-slate-400">
            Confirm what to send, then deploy.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={onApproveDecisions} disabled={isApproving || isDeploying}>
            {isApproving ? "Saving..." : "Save Decisions"}
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={onApproveAndDeploy || onDeployToZendesk}
            disabled={isDeploying || isApproving}
          >
            {isDeploying ? "Deploying..." : "Approve with Client"}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {deployResult ? (
          <div className="mb-3 rounded-md border border-[#7B1FFF]/45 bg-[#5B35FF]/14 p-3 text-xs text-sky-200">
            <p className="font-semibold">Latest Deploy Result</p>
            <p className="mt-1">
              Status: {deployResult.status} | attempted={deployResult.summary?.attempted || 0} | deployed=
              {deployResult.summary?.deployed || 0} | failed={deployResult.summary?.failed || 0} | skipped=
              {deployResult.summary?.skipped || 0}
            </p>
          </div>
        ) : null}

        <div className="mb-3 grid gap-2 md:grid-cols-4">
          <div className="rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-2 text-xs text-slate-300">
            <p className="text-slate-400">Object</p>
            <p className="mt-1 font-semibold text-slate-100">{planning.object_type || "-"}</p>
          </div>
          <div className="rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-2 text-xs text-slate-300">
            <p className="text-slate-400">Records</p>
            <p className="mt-1 font-semibold text-slate-100">{total}</p>
          </div>
          <div className="rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-2 text-xs text-slate-300">
            <p className="text-slate-400">Approved</p>
            <p className="mt-1 font-semibold text-slate-100">{approved}</p>
          </div>
          <div className="rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-2 text-xs text-slate-300">
            <p className="text-slate-400">Warnings / Blocked</p>
            <p className="mt-1 font-semibold text-slate-100">{warnings} / {blocked}</p>
          </div>
        </div>

        {generatedSummary ? (
          <p className="mb-3 text-xs text-slate-400">Generated: {generatedSummary}</p>
        ) : null}

        {duplicateWarningRows.length > 0 ? (
          <div className="mb-3 rounded border border-amber-700/40 bg-amber-950/20 p-3 text-xs text-amber-200">
            <p className="font-semibold">Potential duplicates found in your Zendesk instance</p>
            {duplicateWarningRows.slice(0, 3).map((row) => (
              <p key={`${row.record_id}-dup`} className="mt-1">
                {row.record_id}: {row.title}
              </p>
            ))}
            <p className="mt-1 text-amber-300">
              Review these rows before approving deployment.
            </p>
          </div>
        ) : null}

        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="text-xs text-slate-400">
            If you asked for one item, you review one item. If you asked for many, review each row below.
          </p>
          <input
            value={recordSearch}
            onChange={(event) => setRecordSearch(event.target.value)}
            placeholder="Filter records..."
            className="w-56 rounded-md border border-[#7B1FFF]/35 bg-[#07030F]/70 px-2 py-1 text-xs text-slate-200"
          />
        </div>

        {total === 1 && filteredRecords[0] ? (
          <div className="rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-3 text-sm">
            <div className="mb-2 flex items-center justify-between">
              <p className="font-semibold text-slate-100">{filteredRecords[0].title}</p>
              {statusBadge(filteredRecords[0].validation_status)}
            </div>
            <p className="text-xs text-slate-400">
              {filteredRecords[0].record_id} | {filteredRecords[0].object_type}
            </p>
            <div className="mt-3 flex items-center gap-2">
              <span className="text-xs text-slate-400">Decision</span>
              <DecisionPills
                value={decisions[filteredRecords[0].record_id] || filteredRecords[0].import_decision || "pending_review"}
                deployable={Boolean(filteredRecords[0].deployable)}
                onChange={(nextDecision) => onDecisionChange(filteredRecords[0].record_id, nextDecision)}
              />
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full border-collapse">
              <thead>
                {table.getHeaderGroups().map((headerGroup) => (
                  <tr key={headerGroup.id} className="border-b border-[#7B1FFF]/20">
                    {headerGroup.headers.map((header) => (
                      <th key={header.id} className="px-3 py-2 text-left text-xs font-semibold text-slate-400">
                        {header.isPlaceholder
                          ? null
                          : flexRender(header.column.columnDef.header, header.getContext())}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {table.getRowModel().rows.map((row) => (
                  <tr key={row.id} className="border-b border-[#7B1FFF]/10">
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="px-3 py-2 text-xs">
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-4">
          <Button
            variant="ghost"
            size="sm"
            className="text-xs text-slate-400"
            onClick={() => setShowTechnical((prev) => !prev)}
          >
            {showTechnical ? "Hide technical details" : "Show technical details"}
          </Button>
          {showTechnical ? (
            <div className="mt-2 rounded border border-[#7B1FFF]/25 bg-[#07030F]/55 p-3 text-xs text-slate-300">
              <p>Intent: {planning.intent || "-"}</p>
              <p className="mt-1">Confidence: {planning.confidence ?? "-"}</p>
              <p className="mt-1">Dependency mode: {planning.dependency_mode || "-"}</p>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
