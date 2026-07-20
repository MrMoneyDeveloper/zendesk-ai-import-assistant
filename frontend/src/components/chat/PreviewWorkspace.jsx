import { useMemo, useRef, useState } from "react";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  CircleAlert,
  GitCompareArrows,
  PencilLine,
  Plus,
  ExternalLink,
  LoaderCircle,
  MessageCircleQuestion,
  RefreshCw,
  Send,
  ShieldCheck,
} from "lucide-react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader } from "../ui/card";
import { QuestionAnswer } from "./ContextQuestionWorkspace";

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

function humanize(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function readableValue(value) {
  if (value === null || value === undefined || value === "") return "Empty";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    return value.map((item) => {
      if (item && typeof item === "object") return item.name || item.title || item.value || "Configured item";
      return String(item);
    }).join(", ");
  }
  if (typeof value === "object") {
    return Object.entries(value)
      .map(([key, child]) => `${humanize(key)}: ${readableValue(child)}`)
      .join("; ");
  }
  return String(value).replaceAll("_", " ");
}

function LogicList({ entries = [], kind }) {
  if (!entries.length) {
    return <p className="text-xs text-slate-500">No {kind.toLowerCase()} configured.</p>;
  }
  return (
    <div className="space-y-2">
      {entries.map((entry, index) => {
        const isCondition = kind === "Conditions";
        const scope = String(entry?.scope || entry?.condition_scope || "all").toUpperCase();
        return (
          <div key={`${entry?.field || kind}-${index}`} className="flex min-w-0 items-start gap-2 border-l-2 border-violet-500/35 pl-2.5 text-xs leading-5 text-slate-300">
            <span className="mt-0.5 shrink-0 text-[10px] font-semibold text-violet-300">
              {isCondition ? scope : `${index + 1}`}
            </span>
            <p className="min-w-0 break-words">
              <span className="font-medium text-slate-100">{humanize(entry?.field)}</span>
              {isCondition ? ` ${String(entry?.operator || "is").replaceAll("_", " ")} ` : " becomes "}
              <span>{readableValue(entry?.value)}</span>
            </p>
          </div>
        );
      })}
    </div>
  );
}

function ConfigurationLogic({ configuration, label }) {
  const source = configuration || {};
  return (
    <section className="min-w-0">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-slate-100">{label}</h4>
        {source.active !== null && source.active !== undefined ? (
          <Badge variant={source.active ? "success" : "neutral"}>{source.active ? "active" : "inactive"}</Badge>
        ) : null}
      </div>
      <div>
        <p className="mb-2 text-[11px] font-semibold uppercase text-slate-500">Conditions</p>
        <LogicList entries={source.conditions || []} kind="Conditions" />
      </div>
      <div className="mt-4 border-t border-white/10 pt-3">
        <p className="mb-2 text-[11px] font-semibold uppercase text-slate-500">Actions</p>
        <LogicList entries={source.actions || []} kind="Actions" />
      </div>
    </section>
  );
}

function UpdateComparison({ record }) {
  const changed = (record.change_summary || []).filter((item) => item.changed);
  return (
    <div className="mt-4 border-t border-[#7B1FFF]/25 pt-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <GitCompareArrows size={17} className="text-violet-300" />
          Current vs proposed
        </div>
        <div className="flex flex-wrap gap-1.5">
          {changed.map((item) => (
            <Badge key={item.field} variant="warning">{item.label} changed</Badge>
          ))}
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
        <div className="rounded-md border border-slate-700/70 bg-[#07030F]/35 p-4">
          <ConfigurationLogic configuration={record.before_configuration} label="Current in Zendesk" />
        </div>
        <div className="hidden items-center text-violet-300 lg:flex"><ArrowRight size={20} /></div>
        <div className="rounded-md border border-violet-500/35 bg-violet-500/10 p-4">
          <ConfigurationLogic configuration={record.after_configuration} label="Proposed final state" />
        </div>
      </div>
    </div>
  );
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
  onAskChangeQuestion,
  isAskingChangeQuestion = false,
  changeQuestionHistory = [],
  onDeployToZendesk,
  isDeploying,
  deployResult,
  deploymentMetadata,
  helpCenterUrl,
  onHelpCenterUrlChange,
  helpCenterReadiness,
  onVerifyHelpCenter,
  isVerifyingHelpCenter,
  helpCenterArticleMode,
  onHelpCenterArticleModeChange,
  onDeployHelpCenter,
}) {
  const [recordSearch, setRecordSearch] = useState("");
  const [showTechnical, setShowTechnical] = useState(false);
  const [includeHelpCenter, setIncludeHelpCenter] = useState(false);
  const [confirmPublish, setConfirmPublish] = useState(false);
  const [changeQuestion, setChangeQuestion] = useState("");
  const [changeQuestionError, setChangeQuestionError] = useState("");
  const changeQuestionInputRef = useRef(null);
  const records = useMemo(() => previewData?.records || [], [previewData?.records]);
  const isUpdatePreview = records.some((record) => record.operation_mode === "update");

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
  const helpCenterRecords = records.filter((row) => (
    ["category", "categories", "section", "sections", "article", "articles"]
      .includes(String(row.object_type || "").toLowerCase())
  ));
  const approvedHelpCenterRecords = helpCenterRecords.filter((row) => (
    (decisions[row.record_id] || row.import_decision) === "approved" && row.deployable
  ));
  const helpCenterCounts = helpCenterRecords.reduce((acc, row) => {
    const type = String(row.object_type || "").toLowerCase();
    const key = type.startsWith("categor") ? "categories" : type.startsWith("section") ? "sections" : "articles";
    acc[key] += 1;
    return acc;
  }, { categories: 0, sections: 0, articles: 0 });
  const latestScope = deployResult?.metadata?.deployment_scope;
  const supportPhaseObserved = Boolean(
    latestScope === "support"
    || deploymentMetadata?.phases?.support
    || deploymentMetadata?.phases?.all
  );
  const readinessState = helpCenterReadiness?.state || "not_checked";
  const readinessReady = Boolean(helpCenterReadiness?.ready);
  const requiresPublishConfirmation = helpCenterArticleMode === "publish";
  const canDeployHelpCenter = Boolean(
    includeHelpCenter
    && readinessReady
    && approvedHelpCenterRecords.length > 0
    && (!requiresPublishConfirmation || confirmPublish)
    && !isDeploying
  );

  const submitChangeQuestion = async (event) => {
    event.preventDefault();
    const question = changeQuestion.trim();
    if (question.length < 5 || !onAskChangeQuestion || isAskingChangeQuestion) return;
    setChangeQuestionError("");
    try {
      await onAskChangeQuestion(question);
      setChangeQuestion("");
    } catch (error) {
      setChangeQuestionError(error?.message || "Could not answer this question.");
    }
  };

  const focusChangeQuestion = () => {
    changeQuestionInputRef.current?.focus();
    changeQuestionInputRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <Card className="mt-8 border-[#7B1FFF]/30 bg-[#120522]/70">
      <CardHeader className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-slate-200">
            {isUpdatePreview ? "Review exact update" : "Review and Confirm"}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            {isUpdatePreview
              ? "Compare the synchronized object with its proposed final state before approving the replacement."
              : "Confirm the records, deploy Support objects first, then choose whether to create Help Center content."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {onAskChangeQuestion ? (
            <Button variant="outline" size="sm" className="gap-2" onClick={focusChangeQuestion}>
              <MessageCircleQuestion size={15} />
              Ask before applying
            </Button>
          ) : null}
          <Button variant="outline" size="sm" onClick={onApproveDecisions} disabled={isApproving || isDeploying}>
            {isApproving ? "Saving..." : "Save Decisions"}
          </Button>
          <Button
            variant="default"
            size="sm"
            className="gap-2"
            onClick={onApproveAndDeploy || onDeployToZendesk}
            disabled={isDeploying || isApproving}
          >
            {isUpdatePreview ? <PencilLine size={15} /> : <Send size={15} />}
            {isDeploying ? "Deploying..." : isUpdatePreview ? "Apply Approved Update" : "Deploy Support Objects"}
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

        {onAskChangeQuestion ? (
          <section id="change-impact-question" className="-mx-6 mb-5 border-y border-[#7B1FFF]/25 bg-[#07030F]/35 px-6 py-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-violet-500/15 text-violet-200">
                  <MessageCircleQuestion size={18} />
                </span>
                <div>
                  <h3 className="text-sm font-semibold text-slate-100">Ask before applying</h3>
                  <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-400">
                    Ask how these creations or updates affect current routing, agents, customers, reporting, dependencies, or existing objects.
                  </p>
                </div>
              </div>
              <span className="inline-flex items-center gap-1.5 text-xs text-emerald-300">
                <ShieldCheck size={14} /> Read-only review
              </span>
            </div>

            <form onSubmit={submitChangeQuestion} className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
              <textarea
                ref={changeQuestionInputRef}
                aria-label="Ask about proposed changes"
                value={changeQuestion}
                onChange={(event) => setChangeQuestion(event.target.value)}
                placeholder="e.g. Will these triggers overlap with existing routing, and what should I verify before applying them?"
                className="min-h-20 w-full resize-y rounded-md border border-[#7B1FFF]/35 bg-[#120522]/70 px-3 py-2 text-sm leading-6 text-slate-100 outline-none placeholder:text-slate-500 focus:border-violet-400"
                disabled={isAskingChangeQuestion}
              />
              <Button
                type="submit"
                className="h-10 gap-2 self-end"
                disabled={isAskingChangeQuestion || changeQuestion.trim().length < 5}
              >
                {isAskingChangeQuestion ? <LoaderCircle size={15} className="animate-spin" /> : <MessageCircleQuestion size={15} />}
                {isAskingChangeQuestion ? "Checking..." : "Ask about impact"}
              </Button>
            </form>
            <p className="mt-2 text-[11px] text-slate-500">
              Asking does not save decisions, approve records, or call a Zendesk write endpoint.
            </p>
            {changeQuestionError ? (
              <p className="mt-2 border-l-2 border-rose-500 pl-3 text-xs text-rose-200">{changeQuestionError}</p>
            ) : null}

            <div className="mt-1 space-y-5">
              {changeQuestionHistory.slice(-4).map((item, index) => (
                <QuestionAnswer
                  key={`${item.answered_at || index}-${item.question}`}
                  result={item}
                  question={item.question}
                  compact={index === 0}
                />
              ))}
            </div>
          </section>
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
          <div className="rounded-md border border-[#7B1FFF]/25 bg-[#07030F]/55 p-4 text-sm">
            <div className="mb-2 flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-md ${filteredRecords[0].operation_mode === "update" ? "bg-violet-500/15 text-violet-300" : "bg-emerald-500/15 text-emerald-300"}`}>
                  {filteredRecords[0].operation_mode === "update" ? <PencilLine size={17} /> : <Plus size={18} />}
                </span>
                <div className="min-w-0">
                  <p className="break-words font-semibold text-slate-100">{filteredRecords[0].title}</p>
                  <p className="mt-1 text-xs text-slate-400">
                    {filteredRecords[0].operation_mode === "update" ? "Updating" : "Creating"} {humanize(filteredRecords[0].object_type)}
                    {filteredRecords[0].target_object_id ? ` | Zendesk ID ${filteredRecords[0].target_object_id}` : ""}
                  </p>
                </div>
              </div>
              {statusBadge(filteredRecords[0].validation_status)}
            </div>
            {filteredRecords[0].operation_mode === "update" ? (
              <UpdateComparison record={filteredRecords[0]} />
            ) : (
              <div className="mt-4 border-t border-[#7B1FFF]/20 pt-4">
                <ConfigurationLogic
                  configuration={{
                    title: filteredRecords[0].title,
                    conditions: filteredRecords[0].conditions || [],
                    actions: filteredRecords[0].actions || [],
                  }}
                  label="Will be created"
                />
              </div>
            )}
            {(filteredRecords[0].warnings || []).length > 0 ? (
              <div className="mt-4 border-l-2 border-amber-500 bg-amber-950/15 px-3 py-2 text-xs text-amber-100">
                {filteredRecords[0].warnings.map((warning) => <p key={warning}>{warning}</p>)}
              </div>
            ) : null}
            <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-[#7B1FFF]/20 pt-3">
              <span className="text-xs text-slate-400">Decision</span>
              <DecisionPills
                value={decisions[filteredRecords[0].record_id] || filteredRecords[0].import_decision || "pending_review"}
                deployable={Boolean(filteredRecords[0].deployable)}
                onChange={(nextDecision) => onDecisionChange(filteredRecords[0].record_id, nextDecision)}
              />
            </div>
          </div>
        ) : (
          <div>
            <div className="mb-4 space-y-2">
              {filteredRecords.map((record) => (
                <details key={`${record.record_id}-logic`} className="rounded-md border border-[#7B1FFF]/20 bg-[#07030F]/40 px-3 py-2">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-xs">
                    <span className="min-w-0 truncate font-medium text-slate-100">{record.title}</span>
                    <span className="shrink-0 text-slate-400">{humanize(record.object_type)}</span>
                  </summary>
                  <div className="mt-3 border-t border-[#7B1FFF]/15 pt-3">
                    {record.operation_mode === "update" ? (
                      <UpdateComparison record={record} />
                    ) : (
                      <ConfigurationLogic
                        configuration={{ conditions: record.conditions || [], actions: record.actions || [] }}
                        label="Configuration logic"
                      />
                    )}
                  </div>
                </details>
              ))}
            </div>
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
              <p className="mt-1">Operation: {planning.operation_mode || "create"}</p>
              {planning.target_object_id ? (
                <p className="mt-1">Bound target: {planning.target_object_type} ID {planning.target_object_id}</p>
              ) : null}
              <p className="mt-1">Confidence: {planning.confidence ?? "-"}</p>
              <p className="mt-1">Dependency mode: {planning.dependency_mode || "-"}</p>
            </div>
          ) : null}
        </div>

        {helpCenterRecords.length > 0 && supportPhaseObserved ? (
          <section className="-mx-6 mt-6 border-y border-[#7B1FFF]/25 bg-[#07030F]/45 px-6 py-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="flex min-w-0 gap-3">
                <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-violet-500/15 text-violet-200">
                  <BookOpen size={18} />
                </span>
                <div>
                  <h3 className="text-sm font-semibold text-slate-100">Create Help Center content?</h3>
                  <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-400">
                    Support deployment is separate. Verify the target brand, then create categories and sections before its approved articles.
                  </p>
                </div>
              </div>
              <div className="text-right text-xs text-slate-400">
                <p>{helpCenterCounts.categories} categories | {helpCenterCounts.sections} sections | {helpCenterCounts.articles} articles</p>
                <p className="mt-1">{approvedHelpCenterRecords.length} approved for this phase</p>
              </div>
            </div>

            <label className="mt-5 flex cursor-pointer items-start gap-3 text-sm text-slate-200">
              <input
                type="checkbox"
                checked={includeHelpCenter}
                onChange={(event) => setIncludeHelpCenter(event.target.checked)}
                className="mt-0.5 h-4 w-4 accent-violet-500"
              />
              <span>
                Create the approved Help Center hierarchy and articles
                <span className="mt-0.5 block text-xs text-slate-400">No Help Center write occurs until verification passes and you confirm below.</span>
              </span>
            </label>

            {includeHelpCenter ? (
              <div className="mt-5 space-y-4 border-t border-[#7B1FFF]/20 pt-4">
                <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
                  <label className="block min-w-0">
                    <span className="text-xs font-medium text-slate-300">Brand Help Center URL</span>
                    <input
                      type="url"
                      value={helpCenterUrl}
                      onChange={(event) => onHelpCenterUrlChange(event.target.value)}
                      placeholder="https://brand.zendesk.com/hc/en-us"
                      className="mt-1.5 h-10 w-full rounded-md border border-[#7B1FFF]/35 bg-[#07030F]/70 px-3 text-sm text-slate-100 outline-none focus:border-violet-400"
                    />
                  </label>
                  <Button
                    variant="outline"
                    className="mt-auto gap-2"
                    onClick={onVerifyHelpCenter}
                    disabled={isVerifyingHelpCenter || isDeploying}
                  >
                    <RefreshCw size={15} className={isVerifyingHelpCenter ? "animate-spin" : ""} />
                    {isVerifyingHelpCenter ? "Verifying..." : "Verify"}
                  </Button>
                </div>

                {helpCenterReadiness ? (
                  <div className={`border-l-2 px-3 py-2 text-xs ${
                    readinessReady
                      ? "border-emerald-500 bg-emerald-950/15 text-emerald-200"
                      : "border-amber-500 bg-amber-950/15 text-amber-100"
                  }`}>
                    <div className="flex items-start gap-2">
                      {readinessReady ? <CheckCircle2 size={16} /> : <CircleAlert size={16} />}
                      <div className="min-w-0">
                        <p className="font-semibold">{readinessReady ? "Help Center ready" : "Action required"}</p>
                        <p className="mt-1 leading-5">{helpCenterReadiness.detail}</p>
                        {helpCenterReadiness.brand?.name ? (
                          <p className="mt-1 text-slate-300">Brand: {helpCenterReadiness.brand.name} | Locale: {helpCenterReadiness.locale}</p>
                        ) : null}
                      </div>
                    </div>
                    {!readinessReady && (helpCenterReadiness.instructions || []).length > 0 ? (
                      <ol className="mt-3 list-decimal space-y-1 pl-5 text-slate-300">
                        {helpCenterReadiness.instructions.map((instruction) => (
                          <li key={instruction}>{instruction}</li>
                        ))}
                      </ol>
                    ) : null}
                    {(helpCenterReadiness.checks || []).length > 0 ? (
                      <div className="mt-3 grid gap-1 border-t border-white/10 pt-2 text-slate-300 sm:grid-cols-2">
                        {helpCenterReadiness.checks.map((check) => (
                          <p key={check.name}>
                            <span className="font-medium">{check.name.replaceAll("_", " ")}:</span> {check.detail}
                          </p>
                        ))}
                      </div>
                    ) : null}
                    {!readinessReady && helpCenterReadiness.documentation_url ? (
                      <a
                        href={helpCenterReadiness.documentation_url}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-3 inline-flex items-center gap-1 font-medium text-violet-200 hover:text-violet-100"
                      >
                        Zendesk enablement instructions <ExternalLink size={13} />
                      </a>
                    ) : null}
                  </div>
                ) : (
                  <p className="text-xs text-slate-400">Verification uses read-only brand and Guide API requests.</p>
                )}

                <div>
                  <p className="text-xs font-medium text-slate-300">Article state</p>
                  <div className="mt-2 inline-flex rounded-md border border-[#7B1FFF]/30 p-1">
                    <button
                      type="button"
                      onClick={() => {
                        onHelpCenterArticleModeChange("draft");
                        setConfirmPublish(false);
                      }}
                      className={`h-8 px-3 text-xs ${helpCenterArticleMode === "draft" ? "rounded bg-violet-500/25 text-white" : "text-slate-400"}`}
                    >
                      Drafts
                    </button>
                    <button
                      type="button"
                      onClick={() => onHelpCenterArticleModeChange("publish")}
                      className={`h-8 px-3 text-xs ${helpCenterArticleMode === "publish" ? "rounded bg-violet-500/25 text-white" : "text-slate-400"}`}
                    >
                      Publish
                    </button>
                  </div>
                  <p className="mt-2 text-xs text-slate-400">
                    Drafts are created without making article content public. Publishing requires a separate confirmation.
                  </p>
                </div>

                {requiresPublishConfirmation ? (
                  <label className="flex items-start gap-3 border-l-2 border-amber-500 bg-amber-950/15 px-3 py-2 text-xs text-amber-100">
                    <input
                      type="checkbox"
                      checked={confirmPublish}
                      onChange={(event) => setConfirmPublish(event.target.checked)}
                      className="mt-0.5 h-4 w-4 accent-amber-500"
                    />
                    I confirm these approved articles should be published immediately, not created as drafts.
                  </label>
                ) : null}

                <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[#7B1FFF]/20 pt-4">
                  <p className="text-xs text-slate-400">
                    State: {readinessState.replaceAll("_", " ")}. Failed records remain retryable; successful records are not deployed twice.
                  </p>
                  <Button
                    variant="default"
                    className="gap-2"
                    onClick={onDeployHelpCenter}
                    disabled={!canDeployHelpCenter}
                  >
                    <Send size={15} />
                    {isDeploying
                      ? "Deploying..."
                      : helpCenterArticleMode === "publish"
                        ? "Create hierarchy and publish"
                        : "Create hierarchy and drafts"}
                  </Button>
                </div>
              </div>
            ) : null}
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}
