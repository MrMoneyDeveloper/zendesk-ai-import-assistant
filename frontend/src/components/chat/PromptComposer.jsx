import { useEffect, useRef, useState } from "react";
import { ArrowUp, Globe, Image as ImageIcon, Mic, Paperclip, PencilLine, Square } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "../ui/button";
import { Input } from "../ui/input";
import cxHeroBanner from "../../assets/cx-hero-banner.png";

const schema = z.object({
  prompt: z.string().min(5, "Prompt must be at least 5 characters."),
});

const MAX_ATTACHMENT_SIZE_BYTES = 1024 * 1024;

export default function PromptComposer({
  onSubmitPrompt,
  isLoading,
  defaultPrompt,
  isLocked = false,
  lockReason = "",
  dependencyMode = "match_existing_or_create_new",
  onDependencyModeChange,
  onExistingMode = "create_new",
  onOnExistingModeChange,
  selectedContextCount = 0,
}) {
  const fileInputRef = useRef(null);
  const recognitionRef = useRef(null);
  const [isListening, setIsListening] = useState(false);
  const [attachmentName, setAttachmentName] = useState("");
  const [helperMessage, setHelperMessage] = useState("");
  const form = useForm({
    resolver: zodResolver(schema),
    defaultValues: { prompt: defaultPrompt || "" },
  });

  useEffect(() => {
    form.reset({ prompt: defaultPrompt || "" });
  }, [defaultPrompt, form]);

  useEffect(
    () => () => {
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
    },
    []
  );

  const submit = form.handleSubmit((values) => {
    if (isLocked) return;
    onSubmitPrompt(values.prompt);
  });

  const appendToPrompt = (text) => {
    if (!text) return;
    const current = form.getValues("prompt") || "";
    const next = current ? `${current}\n${text}` : text;
    form.setValue("prompt", next, { shouldValidate: true, shouldDirty: true });
  };

  const onAttachmentClick = () => {
    if (isLocked) return;
    fileInputRef.current?.click();
  };

  const onAttachmentChange = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    setHelperMessage("");
    if (file.size > MAX_ATTACHMENT_SIZE_BYTES) {
      setHelperMessage("Attachment too large. Keep it under 1MB for inline prompt context.");
      event.target.value = "";
      return;
    }

    try {
      const text = await file.text();
      setAttachmentName(file.name);
      appendToPrompt(`Attachment: ${file.name}\n${text.slice(0, 6000)}`);
      setHelperMessage("Attachment text added to prompt.");
    } catch {
      setHelperMessage("Could not read this file as text. Use TXT, CSV, or JSON.");
    } finally {
      event.target.value = "";
    }
  };

  const toggleMic = () => {
    if (isLocked) return;
    setHelperMessage("");

    if (isListening && recognitionRef.current) {
      recognitionRef.current.stop();
      setIsListening(false);
      return;
    }

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setHelperMessage("Microphone transcription is not available in this browser.");
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
      setHelperMessage((prev) => (prev === "Listening..." ? "Mic capture finished." : prev));
    };
    recognition.onerror = () => {
      setIsListening(false);
      setHelperMessage("Could not capture microphone input.");
    };
    recognition.onresult = (event) => {
      const transcript = event.results?.[0]?.[0]?.transcript || "";
      appendToPrompt(transcript);
      setHelperMessage("Mic input added to prompt.");
    };

    recognitionRef.current = recognition;
    recognition.start();
  };

  return (
    <div className="mx-auto mb-8 w-full max-w-5xl">
      <div className="mb-3 overflow-hidden rounded-2xl border border-[#7B1FFF]/38 bg-[#120522]/60 shadow-[0_0_24px_rgba(91,53,255,0.12)]">
        <img src={cxHeroBanner} alt="CX banner" className="h-20 w-full object-cover opacity-80" />
      </div>
      <h1 className="mb-4 text-center text-5xl font-medium text-slate-100">
        Where should we begin?
      </h1>
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
      <form onSubmit={submit} className="rounded-3xl border border-[#7B1FFF]/45 bg-[#120522]/80 p-3 shadow-lg shadow-[#9B35FF]/15">
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="rounded-md p-2 text-[#B9A7D9] hover:bg-[#7B1FFF]/20 disabled:cursor-not-allowed disabled:opacity-60"
            aria-label="Add attachment"
            onClick={onAttachmentClick}
            disabled={isLocked}
          >
            <Paperclip size={20} />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={onAttachmentChange}
            accept=".txt,.json,.csv,.md,.log,.yaml,.yml"
          />
          <Input
            {...form.register("prompt")}
            placeholder="Describe the Zendesk setup you want generated..."
            className="h-12 flex-1 border-none bg-transparent text-base text-[#F4EEFF] focus:border-none"
            disabled={isLocked}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className={`rounded-full p-2 ${isListening ? "text-emerald-300" : "text-[#B9A7D9]"}`}
            disabled={isLocked}
            onClick={toggleMic}
          >
            {isListening ? <Square size={18} /> : <Mic size={18} />}
          </Button>
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
      {attachmentName ? (
        <p className="mt-2 text-center text-xs text-slate-400">Attached: {attachmentName}</p>
      ) : null}
      {helperMessage ? <p className="mt-1 text-center text-xs text-slate-400">{helperMessage}</p> : null}

      <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
        <button type="button" className="cx-chip rounded-full px-4 py-2 text-sm hover:bg-[#7B1FFF]/20">
          <ImageIcon size={16} className="mr-2 inline" />
          Create an image
        </button>
        <button type="button" className="cx-chip rounded-full px-4 py-2 text-sm hover:bg-[#7B1FFF]/20">
          <PencilLine size={16} className="mr-2 inline" />
          Write or edit
        </button>
        <button type="button" className="cx-chip rounded-full px-4 py-2 text-sm hover:bg-[#7B1FFF]/20">
          <Globe size={16} className="mr-2 inline" />
          Look something up
        </button>
      </div>
    </div>
  );
}
