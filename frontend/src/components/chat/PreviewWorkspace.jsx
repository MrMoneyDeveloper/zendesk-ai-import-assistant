import { useMemo, useState } from "react";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../ui/tabs";

const columnHelper = createColumnHelper();

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

export default function PreviewWorkspace({
  previewData,
  generatedData,
  activeTab,
  onActiveTabChange,
  decisions,
  onDecisionChange,
  onApproveDecisions,
  isApproving,
  onDeployToZendesk,
  isDeploying,
  deployResult,
}) {
  const [recordSearch, setRecordSearch] = useState("");
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
            <select
              className="rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-100"
              value={value}
              onChange={(event) => onDecisionChange(recordId, event.target.value)}
            >
              <option value="pending_review">pending_review</option>
              <option value="approved">approved</option>
              <option value="skipped">skipped</option>
              <option value="edit_later">edit_later</option>
            </select>
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

  const validation = previewData.validation_summary || {};
  const planning = previewData.planning_summary || {};
  const generatedCounts = generatedData?.generated_counts || previewData.generated_counts || {};
  const warningRows = records.filter((row) => (row.warnings || []).length > 0);
  const blockedRows = records.filter((row) => row.blocked_reason);

  return (
    <Card className="mt-8">
      <CardHeader className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-slate-200">Preview Workspace</p>
          <p className="mt-1 text-xs text-slate-400">
            Review decisions first, then deploy approved records.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={onApproveDecisions} disabled={isApproving || isDeploying}>
            {isApproving ? "Saving..." : "Save Approval Decisions"}
          </Button>
          <Button variant="default" size="sm" onClick={onDeployToZendesk} disabled={isDeploying || isApproving}>
            {isDeploying ? "Deploying..." : "Deploy Approved to Zendesk"}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {deployResult ? (
          <div className="mb-3 rounded-md border border-sky-700/40 bg-sky-950/20 p-3 text-xs text-sky-200">
            <p className="font-semibold">Latest Deploy Result</p>
            <p className="mt-1">
              Status: {deployResult.status} | attempted={deployResult.summary?.attempted || 0} | deployed=
              {deployResult.summary?.deployed || 0} | failed={deployResult.summary?.failed || 0} | skipped=
              {deployResult.summary?.skipped || 0}
            </p>
          </div>
        ) : null}

        <Tabs value={activeTab} onValueChange={onActiveTabChange}>
          <TabsList>
            <TabsTrigger value="plan">Plan</TabsTrigger>
            <TabsTrigger value="generated">Generated</TabsTrigger>
            <TabsTrigger value="validation">Validation</TabsTrigger>
            <TabsTrigger value="selection">Selection</TabsTrigger>
          </TabsList>

          <TabsContent value="plan">
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-md border border-slate-800 bg-slate-950/70 p-3 text-sm">
                <p className="text-xs text-slate-400">Object Type</p>
                <p className="mt-1 font-semibold text-slate-100">{planning.object_type || "triggers"}</p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-950/70 p-3 text-sm">
                <p className="text-xs text-slate-400">Confidence</p>
                <p className="mt-1 font-semibold text-slate-100">{planning.confidence ?? "-"}</p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-950/70 p-3 text-sm">
                <p className="text-xs text-slate-400">Dependency Mode</p>
                <p className="mt-1 font-semibold text-slate-100">
                  {planning.dependency_mode || "match_existing_or_create_new"}
                </p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-950/70 p-3 text-sm">
                <p className="text-xs text-slate-400">Selected Context Items</p>
                <p className="mt-1 font-semibold text-slate-100">
                  {planning.related_objects_selected ?? 0}
                </p>
              </div>
              <div className="rounded-md border border-slate-800 bg-slate-950/70 p-3 text-sm md:col-span-2">
                <p className="text-xs text-slate-400">Intent</p>
                <p className="mt-1 text-slate-200">{planning.intent || "-"}</p>
                {planning.dependency_notes ? (
                  <p className="mt-2 text-xs text-slate-400">Dependency notes: {planning.dependency_notes}</p>
                ) : null}
              </div>
            </div>
          </TabsContent>

          <TabsContent value="generated">
            <div className="grid gap-3 md:grid-cols-3">
              {Object.keys(generatedCounts).length === 0 ? (
                <p className="text-sm text-slate-400">No generated counts available yet.</p>
              ) : (
                Object.entries(generatedCounts).map(([key, value]) => (
                  <div key={key} className="rounded-md border border-slate-800 bg-slate-950/70 p-3 text-sm">
                    <p className="text-xs uppercase tracking-wide text-slate-500">{key}</p>
                    <p className="mt-1 text-xl font-semibold text-slate-100">{value}</p>
                  </div>
                ))
              )}
            </div>
          </TabsContent>

          <TabsContent value="validation">
            <div className="grid gap-3 md:grid-cols-3">
              <div className="rounded-md border border-emerald-800/50 bg-emerald-950/20 p-3 text-sm">
                <p className="text-xs text-emerald-300">Passed</p>
                <p className="mt-1 text-xl font-semibold text-emerald-200">{validation.passed || 0}</p>
              </div>
              <div className="rounded-md border border-amber-800/50 bg-amber-950/20 p-3 text-sm">
                <p className="text-xs text-amber-300">Warnings</p>
                <p className="mt-1 text-xl font-semibold text-amber-200">{validation.warnings || 0}</p>
              </div>
              <div className="rounded-md border border-rose-800/50 bg-rose-950/20 p-3 text-sm">
                <p className="text-xs text-rose-300">Blocked</p>
                <p className="mt-1 text-xl font-semibold text-rose-200">{validation.blocked || 0}</p>
              </div>
            </div>

            {warningRows.length > 0 ? (
              <div className="mt-4 rounded-md border border-slate-800 bg-slate-950/70 p-3">
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Warnings</p>
                <div className="space-y-2 text-xs text-slate-300">
                  {warningRows.map((row) => (
                    <p key={`${row.record_id}-warn`}>
                      <span className="font-mono text-slate-400">{row.record_id}</span> | {row.warnings.join(" ")}
                    </p>
                  ))}
                </div>
              </div>
            ) : null}

            {blockedRows.length > 0 ? (
              <div className="mt-4 rounded-md border border-rose-800/50 bg-rose-950/20 p-3">
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-rose-300">Blocked</p>
                <div className="space-y-2 text-xs text-rose-200">
                  {blockedRows.map((row) => (
                    <p key={`${row.record_id}-blocked`}>
                      <span className="font-mono">{row.record_id}</span> | {row.blocked_reason}
                    </p>
                  ))}
                </div>
              </div>
            ) : null}
          </TabsContent>

          <TabsContent value="selection">
            <div className="mb-3 flex items-center justify-between gap-3">
              <p className="text-xs text-slate-400">
                Set decisions per row, save approvals, then deploy.
              </p>
              <input
                value={recordSearch}
                onChange={(event) => setRecordSearch(event.target.value)}
                placeholder="Filter records..."
                className="w-56 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
              />
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full border-collapse">
                <thead>
                  {table.getHeaderGroups().map((headerGroup) => (
                    <tr key={headerGroup.id} className="border-b border-slate-800">
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
                    <tr key={row.id} className="border-b border-slate-900/70">
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
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}
