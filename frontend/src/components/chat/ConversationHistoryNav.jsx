import { useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Clock3,
  LoaderCircle,
  MessageSquare,
  Search,
  Sparkles,
} from "lucide-react";


function statusTone(status, darkMode) {
  if (["deployed", "approved", "preview_ready"].includes(status)) {
    return darkMode ? "bg-emerald-400" : "bg-emerald-500";
  }
  if (["failed", "deploy_failed", "validated_failed"].includes(status)) {
    return darkMode ? "bg-rose-400" : "bg-rose-500";
  }
  if (["validated_warning", "deployed_partial", "partially_approved"].includes(status)) {
    return darkMode ? "bg-amber-300" : "bg-amber-500";
  }
  return darkMode ? "bg-violet-300" : "bg-violet-500";
}

function formatDate(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const today = new Date();
  if (parsed.toDateString() === today.toDateString()) {
    return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return parsed.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function ConversationHistoryNav({
  conversations = [],
  activeConversationId = null,
  activeBatchId = null,
  search = "",
  onSearchChange,
  onSelectConversation,
  onSelectBatch,
  isLoading = false,
  isResuming = false,
  darkMode = false,
}) {
  const [expandedIds, setExpandedIds] = useState(() => new Set());

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return conversations;
    return conversations.filter((conversation) => (
      String(conversation.title || "").toLowerCase().includes(query)
      || String(conversation.message_preview || "").toLowerCase().includes(query)
      || (conversation.batches || []).some((batch) => (
        String(batch.batch_id || "").toLowerCase().includes(query)
        || String(batch.prompt_preview || "").toLowerCase().includes(query)
      ))
    ));
  }, [conversations, search]);

  const toggleExpanded = (conversationId) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(conversationId)) next.delete(conversationId);
      else next.add(conversationId);
      return next;
    });
  };

  const border = darkMode ? "border-[#7B1FFF]/20" : "border-slate-200";
  const muted = darkMode ? "text-[#B9A7D9]" : "text-slate-500";
  const input = darkMode
    ? "border-[#7B1FFF]/25 bg-[#120522]/80 text-slate-100 placeholder:text-[#826FA5]"
    : "border-slate-200 bg-slate-50 text-slate-800 placeholder:text-slate-400";

  return (
    <section className="flex min-h-0 flex-1 flex-col" aria-label="Past chats">
      <div className="mb-2 flex items-center justify-between px-1">
        <div className="flex items-center gap-2">
          <MessageSquare size={15} className={muted} />
          <h2 className="text-xs font-semibold uppercase text-inherit">Past chats</h2>
        </div>
        <span className={`text-[11px] ${muted}`}>{conversations.length}</span>
      </div>

      <label className={`mb-2 flex h-9 items-center gap-2 rounded-md border px-2 ${input}`}>
        <Search size={14} className="shrink-0 opacity-70" />
        <span className="sr-only">Search past chats</span>
        <input
          value={search}
          onChange={(event) => onSearchChange?.(event.target.value)}
          placeholder="Search chats..."
          className="min-w-0 flex-1 bg-transparent text-xs outline-none"
        />
      </label>

      <div className="min-h-0 flex-1 overflow-y-auto pr-1">
        {isLoading ? (
          <div className={`flex items-center gap-2 px-2 py-3 text-xs ${muted}`}>
            <LoaderCircle size={14} className="animate-spin" />
            Loading chats...
          </div>
        ) : null}

        {!isLoading && filtered.length === 0 ? (
          <p className={`px-2 py-3 text-xs leading-5 ${muted}`}>
            {search.trim() ? "No chats match this search." : "Your first chat will appear here."}
          </p>
        ) : null}

        <div className="space-y-1">
          {filtered.map((conversation) => {
            const isActive = activeConversationId === conversation.conversation_id;
            const isExpanded = expandedIds.has(conversation.conversation_id);
            const batches = conversation.batches || [];
            return (
              <div key={conversation.conversation_id}>
                <div
                  className={`flex min-h-12 items-stretch rounded-md border transition ${
                    isActive
                      ? darkMode
                        ? "border-[#9B35FF]/70 bg-[#7B1FFF]/20"
                        : "border-violet-300 bg-violet-50"
                      : darkMode
                        ? "border-transparent hover:border-[#7B1FFF]/25 hover:bg-[#7B1FFF]/10"
                        : "border-transparent hover:border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => toggleExpanded(conversation.conversation_id)}
                    className={`flex w-8 shrink-0 items-center justify-center ${muted}`}
                    aria-label={`${isExpanded ? "Collapse" : "Expand"} ${conversation.title}`}
                    aria-expanded={isExpanded}
                    title={`${isExpanded ? "Collapse" : "Expand"} chat runs`}
                  >
                    {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setExpandedIds((current) => {
                        const next = new Set(current);
                        next.add(conversation.conversation_id);
                        return next;
                      });
                      onSelectConversation?.(conversation.conversation_id);
                    }}
                    className="min-w-0 flex-1 py-2 pr-2 text-left"
                    disabled={isResuming}
                    title={`Continue ${conversation.title}`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-xs font-medium">{conversation.title}</span>
                      {conversation.title_source === "ai" ? (
                        <Sparkles
                          size={11}
                          className="shrink-0 text-violet-500"
                          aria-label="AI-assigned title"
                        />
                      ) : null}
                    </span>
                    <span className={`mt-0.5 flex items-center gap-1.5 text-[10px] ${muted}`}>
                      <Clock3 size={10} />
                      {formatDate(conversation.updated_at)}
                      <span aria-hidden="true">.</span>
                      {conversation.message_count || 0} messages
                    </span>
                  </button>
                </div>

                {isExpanded ? (
                  <div className={`ml-4 border-l pl-3 ${border}`}>
                    {batches.length > 0 ? (
                      batches.map((batch) => (
                        <button
                          key={batch.batch_id}
                          type="button"
                          onClick={() => onSelectBatch?.(conversation.conversation_id, batch.batch_id)}
                          className={`my-1 flex min-h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[11px] transition ${
                            activeBatchId === batch.batch_id
                              ? darkMode
                                ? "bg-[#7B1FFF]/20 text-white"
                                : "bg-violet-50 text-violet-800"
                              : darkMode
                                ? "text-[#B9A7D9] hover:bg-[#7B1FFF]/10"
                                : "text-slate-600 hover:bg-slate-50"
                          }`}
                          title={batch.prompt_preview || batch.batch_id}
                        >
                          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusTone(batch.status, darkMode)}`} />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate">{batch.prompt_preview || batch.batch_id}</span>
                            <span className="block truncate font-mono text-[9px] opacity-70">
                              {batch.batch_id}
                            </span>
                          </span>
                        </button>
                      ))
                    ) : (
                      <button
                        type="button"
                        onClick={() => onSelectConversation?.(conversation.conversation_id)}
                        className={`my-1 w-full rounded-md px-2 py-2 text-left text-[11px] ${muted}`}
                      >
                        Continue this conversation
                      </button>
                    )}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
