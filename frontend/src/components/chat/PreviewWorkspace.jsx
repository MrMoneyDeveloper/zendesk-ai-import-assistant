import { useMemo } from "react";
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

export default function PreviewWorkspace({
  previewData,
  generatedData,
  activeTab,
  onActiveTabChange,
  decisions,
  onDecisionChange,
  onApproveDecisions,
  isApproving,
}) {
  const records = previewData?.records || [];
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
    ],
    [decisions, onDecisionChange]
  );

  const table = useReactTable({
    data: records,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  if (!previewData) {
    return null;
  }

  return (
    <Card className="mt-8">
      <CardHeader className="flex items-center justify-between">
        <div className="text-sm font-medium text-slate-200">Preview Workspace</div>
        <Button variant="outline" size="sm" onClick={onApproveDecisions} disabled={isApproving}>
          {isApproving ? "Saving..." : "Save Approval Decisions"}
        </Button>
      </CardHeader>
      <CardContent>
        <Tabs value={activeTab} onValueChange={onActiveTabChange}>
          <TabsList>
            <TabsTrigger value="plan">Plan</TabsTrigger>
            <TabsTrigger value="generated">Generated</TabsTrigger>
            <TabsTrigger value="validation">Validation</TabsTrigger>
            <TabsTrigger value="selection">Selection</TabsTrigger>
          </TabsList>

          <TabsContent value="plan">
            <pre className="overflow-auto rounded-md border border-slate-800 bg-slate-950 p-3 text-xs text-slate-300">
              {JSON.stringify(previewData.planning_summary || {}, null, 2)}
            </pre>
          </TabsContent>

          <TabsContent value="generated">
            <pre className="overflow-auto rounded-md border border-slate-800 bg-slate-950 p-3 text-xs text-slate-300">
              {JSON.stringify(generatedData?.generated_counts || previewData.generated_counts || {}, null, 2)}
            </pre>
          </TabsContent>

          <TabsContent value="validation">
            <pre className="overflow-auto rounded-md border border-slate-800 bg-slate-950 p-3 text-xs text-slate-300">
              {JSON.stringify(previewData.validation_summary || {}, null, 2)}
            </pre>
          </TabsContent>

          <TabsContent value="selection">
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
