import { useEffect, useMemo, useRef, useState } from "react";
import { LayoutTemplate, Mic, Paperclip, Sparkles, Square, WandSparkles, X } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "../ui/button";

const schema = z.object({
  prompt: z.string().min(5, "Prompt must be at least 5 characters."),
});

const OBJECT_FOCUS_OPTIONS = [
  { key: "brands", label: "Brands" },
  { key: "categories", label: "Categories" },
  { key: "sections", label: "Sections" },
  { key: "triggers", label: "Triggers" },
  { key: "automations", label: "Automations" },
  { key: "macros", label: "Macros" },
  { key: "views", label: "Views" },
  { key: "groups", label: "Groups" },
  { key: "ticket_forms", label: "Forms" },
  { key: "ticket_fields", label: "Fields" },
  { key: "articles", label: "Articles" },
];

export default function PromptComposer({
  onSubmitPrompt,
  onExtractAttachment,
  onRemoveAttachment,
  isLoading,
  isLocked = false,
  lockReason = "",
  dependencyMode = "match_existing_or_create_new",
  onDependencyModeChange,
  onExistingMode = "create_new",
  onOnExistingModeChange,
  existingItemBehavior = "relate_or_update",
  onExistingItemBehaviorChange,
  selectedContextCount = 0,
  focusObjectTypes = [],
  onFocusObjectTypesChange,
  attachments = [],
  existingItemOptions = [],
  selectedExistingItems = [],
  selectedExistingItemKeySet = new Set(),
  onAddExistingContext,
  onRemoveExistingContext,
  articleHelpCenterHint = "",
  embedded = false,
  compact = false,
  darkMode = false,
  externalPrompt = "",
  onExternalPromptConsumed,
}) {
  const fileInputRef = useRef(null);
  const recognitionRef = useRef(null);
  const [isListening, setIsListening] = useState(false);
  const [helperMessage, setHelperMessage] = useState("");
  const [existingSelectionKey, setExistingSelectionKey] = useState("");

  const form = useForm({
    resolver: zodResolver(schema),
    defaultValues: { prompt: "" },
  });

  // When a quick action or example sets an external prompt, load it into the textarea
  useEffect(() => {
    if (!externalPrompt) return;
    form.setValue("prompt", externalPrompt, { shouldValidate: true, shouldDirty: true });
    onExternalPromptConsumed?.();
  }, [externalPrompt, form, onExternalPromptConsumed]);

  useEffect(
    () => () => {
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
    },
    []
  );

  const activeFocusSet = useMemo(() => new Set(focusObjectTypes || []), [focusObjectTypes]);
  const hasExplicitFocus = (focusObjectTypes || []).length > 0;
  const activeModeLabel = existingItemBehavior === "create_new" ? "Create New" : "Base";
  const panelClass = darkMode
    ? "border-[#7B1FFF]/30 bg-[#120522]/70 text-slate-100"
    : "border-violet-200 bg-white text-slate-950 shadow-sm";
  const mutedText = darkMode ? "text-[#B9A7D9]" : "text-slate-500";
  const subtlePanel = darkMode
    ? "border-[#7B1FFF]/30 bg-[#120522]/70"
    : "border-slate-200 bg-white";
  const chipIdle = darkMode
    ? "border-[#7B1FFF]/30 text-[#B9A7D9] hover:bg-[#7B1FFF]/12"
    : "border-violet-200 text-violet-700 hover:bg-violet-50";
  const chipActive = darkMode
    ? "border-[#7B1FFF]/70 bg-[#7B1FFF]/20 text-[#F4EEFF]"
    : "border-violet-300 bg-violet-50 text-violet-700";

  const toggleFocus = (key) => {
    const current = new Set(activeFocusSet);
    if (current.has(key)) {
      current.delete(key);
    } else {
      current.add(key);
    }
    onFocusObjectTypesChange?.(Array.from(current));
  };

  const addExistingContextSelection = () => {
    if (!existingSelectionKey) return;
    onAddExistingContext?.(existingSelectionKey);
    setExistingSelectionKey("");
  };

  const submit = form.handleSubmit(async (values) => {
    if (isLocked || isLoading) return;
    const accepted = await onSubmitPrompt(values.prompt);
    if (accepted !== false) {
      form.reset({ prompt: "" });
      setHelperMessage("Prompt submitted.");
    }
  });

  const appendToPrompt = (text) => {
    if (!text) return;
    const current = form.getValues("prompt") || "";
    const next = current ? `${current}\n${text}` : text;
    form.setValue("prompt", next, { shouldValidate: true, shouldDirty: true });
  };

  const onAttachmentClick = () => {
    if (isLocked || isLoading) return;
    fileInputRef.current?.click();
  };

  const onAttachmentChange = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setHelperMessage("Extracting attachment...");
    try {
      const result = await onExtractAttachment?.(file);
      if (result?.filename) {
        setHelperMessage(
          `Attached ${result.filename}${result.truncated ? " (truncated)" : ""}.`
        );
      } else {
        setHelperMessage("Attachment extracted.");
      }
    } catch {
      setHelperMessage("Could not extract this attachment.");
    } finally {
      event.target.value = "";
    }
  };

  const toggleMic = () => {
    if (isLocked || isLoading) return;
    setHelperMessage("");

    if (isListening && recognitionRef.current) {
      recognitionRef.current.stop();
      setIsListening(false);
      return;
    }

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setHelperMessage("Dictation is not available in this browser.");
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      setIsListening(true);
      setHelperMessage("Listening...");
    };
    recognition.onend = () => {
      setIsListening(false);
      setHelperMessage((prev) => (prev === "Listening..." ? "Dictation complete." : prev));
    };
    recognition.onerror = () => {
      setIsListening(false);
      setHelperMessage("Could not capture dictation.");
    };
    recognition.onresult = (event) => {
      const transcript = event.results?.[0]?.[0]?.transcript || "";
      appendToPrompt(transcript);
      setHelperMessage("Dictation appended.");
    };

    recognitionRef.current = recognition;
    recognition.start();
  };

  return (
    <div className={embedded ? "w-full" : "mx-auto mb-8 w-full max-w-5xl"}>
      {!embedded ? (
        <h1 className="mb-4 text-center text-5xl font-medium text-slate-100">
          Where should we begin?
        </h1>
      ) : null}

      {hasExplicitFocus && !compact ? (
        <div className={`mb-3 rounded-xl border p-3 ${subtlePanel}`}>
          <p className={`mb-2 text-xs font-semibold ${mutedText}`}>Existing item (optional)</p>
          <div className="mb-2">
            <p className={`mb-1 text-[11px] ${mutedText}`}>Behavior</p>
            <div className={`inline-flex rounded-lg border p-1 ${darkMode ? "border-[#7B1FFF]/35 bg-[#07030F]/70" : "border-slate-200 bg-slate-50"}`}>
              <button
                type="button"
                onClick={() => onExistingItemBehaviorChange?.("relate_or_update")}
                className={`rounded-md px-2 py-1 text-[11px] ${
                  existingItemBehavior === "relate_or_update"
                    ? darkMode ? "bg-[#7B1FFF]/28 text-[#F4EEFF]" : "bg-white text-violet-700 shadow-sm"
                    : mutedText
                }`}
                disabled={isLocked || isLoading}
              >
                Use existing as base
              </button>
              <button
                type="button"
                onClick={() => onExistingItemBehaviorChange?.("create_new")}
                className={`rounded-md px-2 py-1 text-[11px] ${
                  existingItemBehavior === "create_new"
                    ? darkMode ? "bg-[#7B1FFF]/28 text-[#F4EEFF]" : "bg-white text-violet-700 shadow-sm"
                    : mutedText
                }`}
                disabled={isLocked || isLoading}
              >
                Ignore existing, create new
              </button>
            </div>
          </div>
          <div className="flex flex-col gap-2 md:flex-row">
            <select
              value={existingSelectionKey}
              onChange={(event) => setExistingSelectionKey(event.target.value)}
              className={`flex-1 rounded-md border px-2 py-2 text-xs ${darkMode ? "border-[#7B1FFF]/40 bg-[#07030F]/80 text-slate-200" : "border-slate-200 bg-white text-slate-700"}`}
              disabled={isLocked || isLoading || existingItemOptions.length === 0}
            >
              <option value="">
                {existingItemOptions.length === 0
                  ? "No matching existing items loaded"
                  : "Select an existing item"}
              </option>
              {existingItemOptions.map((option) => (
                <option key={option.key} value={option.key}>
                  {option.label}
                </option>
              ))}
            </select>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={addExistingContextSelection}
              disabled={isLocked || isLoading || !existingSelectionKey || existingItemBehavior === "create_new"}
            >
              Add
            </Button>
          </div>
          {existingItemBehavior === "create_new" ? (
            <p className="mt-2 text-[11px] text-slate-400">
              Existing selections are ignored for generation while this mode is active.
            </p>
          ) : null}
          {articleHelpCenterHint ? (
            <p className="mt-2 text-xs text-amber-300">{articleHelpCenterHint}</p>
          ) : null}
          {selectedExistingItems.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-2">
              {selectedExistingItems.map((item) => {
                const key = `${item.object_type}:${item.id}`;
                if (!selectedExistingItemKeySet.has(key)) return null;
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => onRemoveExistingContext?.(key)}
                    className="rounded-full border border-emerald-700/80 bg-emerald-950/30 px-2 py-1 text-[11px] text-emerald-200"
                    title="Remove from selected context"
                  >
                    {item.object_type}: {item.name} <span className="text-emerald-300">x</span>
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="mt-2 text-[11px] text-slate-400">
              Pick an existing object to guide update/relationship behavior for this prompt.
            </p>
          )}
        </div>
      ) : null}

      {attachments.length > 0 ? (
        <div className={`mb-3 rounded-xl border p-3 ${subtlePanel}`}>
          <p className={`mb-2 text-xs font-semibold ${mutedText}`}>Attached context (session)</p>
          <div className="space-y-1">
            {attachments.map((item) => (
              <div key={item.id} className={`flex items-center justify-between rounded border px-2 py-1 text-xs ${darkMode ? "border-[#7B1FFF]/20 bg-[#07030F]/60 text-slate-200" : "border-slate-200 bg-slate-50 text-slate-600"}`}>
                <span>{item.filename} ({item.char_count} chars)</span>
                <button
                  type="button"
                  className="rounded p-1 text-slate-400 hover:text-slate-200"
                  onClick={() => onRemoveAttachment?.(item.id)}
                  aria-label={`Remove ${item.filename}`}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <form onSubmit={submit} className={`rounded-2xl border p-5 ${panelClass} ${embedded ? "w-full" : ""}`}>
        <div className="mb-4 flex items-center gap-3">
          <Sparkles className="h-5 w-5 text-violet-500" />
          <h3 className="text-base font-semibold">Describe what you want to build...</h3>
          {compact && selectedContextCount > 0 ? (
            <span className={`ml-auto rounded-full px-2 py-1 text-xs ${darkMode ? "bg-[#7B1FFF]/18 text-[#B9A7D9]" : "bg-violet-50 text-violet-700"}`}>
              {selectedContextCount} context
            </span>
          ) : null}
        </div>
        <textarea
          {...form.register("prompt")}
          placeholder="e.g. Create a trigger that closes tickets after 7 days of inactivity and sends a reminder email to the customer..."
          className={`min-h-[112px] w-full resize-y rounded-xl border px-4 py-3 text-sm leading-6 outline-none transition ${
            darkMode
              ? "border-[#7B1FFF]/25 bg-[#07030F]/60 text-[#F4EEFF] placeholder:text-[#B9A7D9]/70 focus:border-[#9B35FF]"
              : "border-slate-200 bg-white text-slate-800 placeholder:text-slate-500 focus:border-violet-400"
          }`}
          disabled={isLocked || isLoading}
        />
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          onChange={onAttachmentChange}
          accept=".txt,.json,.csv,.md,.log,.yaml,.yml,.pdf,.docx"
        />
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={`inline-flex h-10 items-center gap-2 rounded-lg border px-3 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-60 ${
                darkMode
                  ? "border-[#7B1FFF]/35 bg-[#07030F]/40 text-[#F4EEFF] hover:bg-[#7B1FFF]/18"
                  : "border-violet-200 bg-white text-slate-700 hover:bg-violet-50"
              }`}
              aria-label="Add attachment"
              onClick={onAttachmentClick}
              disabled={isLocked || isLoading}
            >
              <Paperclip size={16} />
              Attach File
            </button>
            <button
              type="button"
              className={`inline-flex h-10 items-center gap-2 rounded-lg border px-3 text-sm font-medium ${
                darkMode
                  ? "border-[#7B1FFF]/35 bg-[#07030F]/40 text-[#F4EEFF] hover:bg-[#7B1FFF]/18"
                  : "border-violet-200 bg-white text-slate-700 hover:bg-violet-50"
              }`}
            >
              <LayoutTemplate size={16} />
              Use Template
            </button>
          </div>
          <div className="flex items-center justify-end gap-2">
            {!compact ? (
              <span
                className={`rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-wide ${
                  darkMode ? "border-[#7B1FFF]/50 bg-[#07030F]/70 text-[#B9A7D9]" : "border-violet-200 bg-violet-50 text-violet-700"
                }`}
                title="Existing item behavior mode"
              >
                {activeModeLabel}
              </span>
            ) : null}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={`h-10 w-10 rounded-full border p-0 ${isListening ? "text-emerald-400" : darkMode ? "border-[#7B1FFF]/25 text-[#B9A7D9]" : "border-violet-100 text-violet-700"}`}
              disabled={isLocked || isLoading}
              onClick={toggleMic}
              aria-label={isListening ? "Stop dictation" : "Start dictation"}
            >
              {isListening ? <Square size={17} /> : <Mic size={17} />}
            </Button>
            <Button
              type="submit"
              size="sm"
              className="h-10 gap-2 rounded-lg bg-[#7B1FFF] px-5 text-white hover:bg-[#9B35FF]"
              disabled={isLoading || isLocked}
            >
              <WandSparkles size={16} />
              Generate
            </Button>
          </div>
        </div>
      </form>

      {isLocked ? (
        <p className="mt-2 text-center text-sm text-amber-300">
          {lockReason || "Prompt is locked until integrations are validated."}
        </p>
      ) : null}
      {form.formState.errors.prompt ? (
        <p className="mt-2 text-center text-sm text-rose-400">{form.formState.errors.prompt.message}</p>
      ) : null}
      {helperMessage ? <p className="mt-1 text-center text-xs text-slate-400">{helperMessage}</p> : null}
    </div>
  );
}
