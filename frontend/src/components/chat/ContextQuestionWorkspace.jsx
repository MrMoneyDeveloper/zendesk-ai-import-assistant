import {
  CheckCircle2,
  CircleAlert,
  Database,
  LoaderCircle,
  Search,
  ShieldCheck,
} from "lucide-react";

function ResultList({ title, rows = [], tone = "default", darkMode = true }) {
  if (!rows.length) return null;
  const toneClass = tone === "risk"
    ? darkMode ? "border-amber-500/40 text-amber-100" : "border-amber-500/50 text-amber-900"
    : tone === "unknown"
      ? darkMode ? "border-slate-500/40 text-slate-300" : "border-slate-400 text-slate-600"
      : darkMode ? "border-violet-500/35 text-slate-200" : "border-violet-400 text-slate-700";
  return (
    <section className={`border-l-2 pl-3 ${toneClass}`}>
      <h4 className="text-[11px] font-semibold uppercase">{title}</h4>
      <ul className="mt-2 space-y-1.5 text-xs leading-5">
        {rows.map((row, index) => <li key={`${title}-${index}-${row}`}>{row}</li>)}
      </ul>
    </section>
  );
}

export function QuestionAnswer({ result, question, compact = false, darkMode = true }) {
  if (!result) return null;
  const scope = result.scope || {};
  const citations = result.citations || [];
  const answerText = darkMode ? "text-slate-100" : "text-slate-800";
  const mutedText = darkMode ? "text-slate-400" : "text-slate-500";
  return (
    <article className={`${compact ? "pt-4" : `border-t pt-5 ${darkMode ? "border-[#7B1FFF]/20" : "border-slate-200"}`} `}>
      {question ? <p className={`text-xs font-medium ${darkMode ? "text-violet-200" : "text-violet-700"}`}>{question}</p> : null}
      <div className={`mt-3 flex flex-wrap items-center gap-2 text-[11px] ${mutedText}`}>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-emerald-200">
          <ShieldCheck size={12} /> Read-only
        </span>
        <span>{result.provider || "Deterministic"}{result.model ? ` | ${result.model}` : ""}</span>
        <span>{Math.round(Number(result.confidence || 0) * 100)}% confidence</span>
        {result.fallback_used ? <span className="text-amber-300">Fallback used</span> : null}
      </div>

      <p className={`mt-4 whitespace-pre-wrap text-sm leading-6 ${answerText}`}>{result.answer}</p>

      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <ResultList title="Confirmed findings" rows={result.findings} darkMode={darkMode} />
        <ResultList title="Expected impact" rows={result.impact} darkMode={darkMode} />
        <ResultList title="Risks" rows={result.risks} tone="risk" darkMode={darkMode} />
        <ResultList title="Checks before action" rows={result.recommended_checks} darkMode={darkMode} />
        <ResultList title="Could not verify" rows={result.cannot_verify} tone="unknown" darkMode={darkMode} />
      </div>

      {citations.length > 0 ? (
        <div className={`mt-5 border-t pt-3 ${darkMode ? "border-[#7B1FFF]/15" : "border-slate-200"}`}>
          <p className={`text-[11px] font-semibold uppercase ${mutedText}`}>Grounded sources</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {citations.map((citation) => (
              <span
                key={`${citation.source}-${citation.object_id}`}
                className={`max-w-full rounded-md border px-2 py-1 text-[11px] ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/45 text-slate-300" : "border-slate-200 bg-slate-50 text-slate-600"}`}
                title={citation.evidence || "Reviewed source"}
              >
                {citation.source === "proposal" ? "Proposal" : "Zendesk"}: {citation.name} (ID {citation.object_id})
              </span>
            ))}
          </div>
        </div>
      ) : null}

      <div className={`mt-4 flex flex-wrap gap-x-4 gap-y-1 text-[11px] ${mutedText}`}>
        <span>Reviewed {Number(scope.catalog_included || 0)} relevant current objects from {Number(scope.catalog_total || 0)}</span>
        {Number(scope.proposal_total || 0) > 0 ? (
          <span>Proposal records: {Number(scope.proposal_total || 0)}</span>
        ) : null}
      </div>

      {(result.warnings || []).length > 0 ? (
        <div className="mt-3 flex items-start gap-2 border-l-2 border-amber-500 bg-amber-950/15 px-3 py-2 text-xs text-amber-100">
          <CircleAlert size={15} className="mt-0.5 shrink-0" />
          <p>{result.warnings.join(" ")}</p>
        </div>
      ) : null}
    </article>
  );
}

export default function ContextQuestionWorkspace({ history = [], isLoading = false, darkMode = false }) {
  const surface = darkMode
    ? "border-[#7B1FFF]/28 bg-[#120522]/55"
    : "border-slate-200 bg-white";
  return (
    <section className={`mt-5 border-y px-4 py-5 sm:px-5 ${surface}`} aria-live="polite">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-amber-500/15 text-amber-400">
            {isLoading ? <LoaderCircle size={18} className="animate-spin" /> : <Search size={18} />}
          </span>
          <div>
            <h2 className={`text-sm font-semibold ${darkMode ? "text-slate-100" : "text-slate-900"}`}>Read-only Zendesk answers</h2>
            <p className={`mt-1 text-xs leading-5 ${darkMode ? "text-[#B9A7D9]" : "text-slate-500"}`}>
              Answers use a fresh synchronized catalog and never approve, create, update, or deploy records.
            </p>
          </div>
        </div>
        <span className={`inline-flex items-center gap-1.5 text-xs ${darkMode ? "text-emerald-300" : "text-emerald-700"}`}>
          <Database size={14} /> Current instance context
        </span>
      </div>

      {isLoading ? (
        <div className={`mt-5 flex items-center gap-3 border-t pt-4 text-sm ${darkMode ? "border-[#7B1FFF]/20 text-slate-300" : "border-slate-200 text-slate-600"}`}>
          <LoaderCircle size={17} className="animate-spin text-violet-500" />
          Searching synchronized configuration and checking the answer against source objects...
        </div>
      ) : null}

      {history.length === 0 && !isLoading ? (
        <div className={`mt-5 flex items-start gap-3 border-t pt-4 text-sm ${darkMode ? "border-[#7B1FFF]/20 text-slate-300" : "border-slate-200 text-slate-600"}`}>
          <CheckCircle2 size={17} className="mt-0.5 shrink-0 text-emerald-500" />
          Ask about routing, forms, fields, macros, Help Center content, schedules, SLA policies, duplicates, or dependencies.
        </div>
      ) : null}

      <div className="space-y-5">
        {history.map((item, index) => (
          <QuestionAnswer
            key={`${item.answered_at || index}-${item.question}`}
            result={item}
            question={item.question}
            darkMode={darkMode}
          />
        ))}
      </div>
    </section>
  );
}
