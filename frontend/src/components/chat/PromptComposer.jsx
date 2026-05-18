import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Mic, Paperclip, Square, X } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "../ui/button";
import { Input } from "../ui/input";

const schema = z.object({
  prompt: z.string().min(5, "Prompt must be at least 5 characters."),
});

const OBJECT_FOCUS_OPTIONS = [
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
      <div className="mb-3 grid gap-2 rounded-xl border border-[#7B1FFF]/30 bg-[#120522]/70 p-3 text-xs text-slate-300 md:grid-cols-3">
        <label className="flex flex-col gap-1">
          <span className="text-[#B9A7D9]">Dependency behavior</span>
          <select
            value={dependencyMode}
            onChange={(event) => onDependencyModeChange?.(event.target.value)}
            className="rounded-md border border-[#7B1FFF]/40 bg-[#07030F]/80 px-2 py-1 text-xs text-slate-200"
            disabled={isLocked}
          >
            <option value="match_existing_or_create_new">Match existing, else create new</option>
            <option value="force_existing_only">Use existing only (strict)</option>
            <option value="force_create_new">Always create new</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[#B9A7D9]">If object already exists</span>
          <select
            value={onExistingMode}
            onChange={(event) => onOnExistingModeChange?.(event.target.value)}
            className="rounded-md border border-[#7B1FFF]/40 bg-[#07030F]/80 px-2 py-1 text-xs text-slate-200"
            disabled={isLocked}
          >
            <option value="create_new">Create new anyway</option>
            <option value="overwrite_existing">Overwrite existing</option>
            <option value="skip_existing">Skip existing</option>
          </select>
        </label>
        <div className="rounded-md border border-[#7B1FFF]/30 bg-[#07030F]/70 px-2 py-2">
          <p className="text-[#B9A7D9]">Selected context</p>
          <p className="mt-1 font-semibold text-slate-200">{selectedContextCount} objects selected</p>
        </div>
      </div>

      <div className="mb-3 rounded-xl border border-[#7B1FFF]/30 bg-[#120522]/70 p-3">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-xs font-semibold text-[#B9A7D9]">Object focus</p>
          <button
            type="button"
            className={`rounded-full border px-2 py-1 text-[11px] ${
              focusObjectTypes.length === 0
                ? "border-[#7B1FFF]/60 bg-[#7B1FFF]/20 text-[#F4EEFF]"
                : "border-[#7B1FFF]/30 text-[#B9A7D9]"
            }`}
            onClick={() => onFocusObjectTypesChange?.([])}
            disabled={isLocked}
          >
            Auto
          </button>
        </div>
        <div className="flex flex-wrap gap-2">
          {OBJECT_FOCUS_OPTIONS.map((option) => {
            const active = activeFocusSet.has(option.key);
            return (
              <button
                key={option.key}
                type="button"
                onClick={() => toggleFocus(option.key)}
                disabled={isLocked}
                className={`rounded-full border px-3 py-1 text-xs ${
                  active
                    ? "border-[#7B1FFF]/70 bg-[#7B1FFF]/20 text-[#F4EEFF]"
                    : "border-[#7B1FFF]/30 text-[#B9A7D9] hover:bg-[#7B1FFF]/12"
                }`}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      {hasExplicitFocus ? (
        <div className="mb-3 rounded-xl border border-[#7B1FFF]/30 bg-[#120522]/70 p-3">
          <p className="mb-2 text-xs font-semibold text-[#B9A7D9]">Existing item (optional)</p>
          <div className="mb-2">
            <p className="mb-1 text-[11px] text-[#B9A7D9]">Behavior</p>
            <div className="inline-flex rounded-lg border border-[#7B1FFF]/35 bg-[#07030F]/70 p-1">
              <button
                type="button"
                onClick={() => onExistingItemBehaviorChange?.("relate_or_update")}
                className={`rounded-md px-2 py-1 text-[11px] ${
                  existingItemBehavior === "relate_or_update"
                    ? "bg-[#7B1FFF]/28 text-[#F4EEFF]"
                    : "text-[#B9A7D9]"
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
                    ? "bg-[#7B1FFF]/28 text-[#F4EEFF]"
                    : "text-[#B9A7D9]"
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
              className="flex-1 rounded-md border border-[#7B1FFF]/40 bg-[#07030F]/80 px-2 py-2 text-xs text-slate-200"
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
        <div className="mb-3 rounded-xl border border-[#7B1FFF]/30 bg-[#120522]/60 p-3">
          <p className="mb-2 text-xs font-semibold text-[#B9A7D9]">Attached context (session)</p>
          <div className="space-y-1">
            {attachments.map((item) => (
              <div key={item.id} className="flex items-center justify-between rounded border border-[#7B1FFF]/20 bg-[#07030F]/60 px-2 py-1 text-xs text-slate-200">
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

      <form onSubmit={submit} className={`rounded-3xl border border-[#7B1FFF]/45 bg-[#120522]/80 p-3 shadow-lg shadow-[#9B35FF]/15 ${embedded ? "ml-auto w-full max-w-4xl" : ""}`}>
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="rounded-md p-2 text-[#B9A7D9] hover:bg-[#7B1FFF]/20 disabled:cursor-not-allowed disabled:opacity-60"
            aria-label="Add attachment"
            onClick={onAttachmentClick}
            disabled={isLocked || isLoading}
          >
            <Paperclip size={20} />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={onAttachmentChange}
            accept=".txt,.json,.csv,.md,.log,.yaml,.yml,.pdf,.docx"
          />
          <Input
            {...form.register("prompt")}
            placeholder="Describe the Zendesk setup you want generated..."
            className="h-12 flex-1 border-none bg-transparent text-base text-[#F4EEFF] focus:border-none"
            disabled={isLocked || isLoading}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className={`rounded-full p-2 ${isListening ? "text-emerald-300" : "text-[#B9A7D9]"}`}
            disabled={isLocked || isLoading}
            onClick={toggleMic}
          >
            {isListening ? <Square size={18} /> : <Mic size={18} />}
          </Button>
          <span
            className="rounded-full border border-[#7B1FFF]/50 bg-[#07030F]/70 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-[#B9A7D9]"
            title="Existing item behavior mode"
          >
            {activeModeLabel}
          </span>
          <Button
            type="submit"
            size="sm"
            className="h-10 w-10 rounded-full bg-[#9B35FF] text-white hover:bg-[#C063FF]"
            disabled={isLoading || isLocked}
          >
            <ArrowUp size={18} />
          </Button>
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
