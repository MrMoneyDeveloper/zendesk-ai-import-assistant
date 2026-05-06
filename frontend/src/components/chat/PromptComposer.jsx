import { Plus, ArrowUp, Mic, Globe, PencilLine, Image as ImageIcon } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "../ui/button";
import { Input } from "../ui/input";

const schema = z.object({
  prompt: z.string().min(5, "Prompt must be at least 5 characters."),
});

export default function PromptComposer({ onSubmitPrompt, isLoading, defaultPrompt }) {
  const form = useForm({
    resolver: zodResolver(schema),
    defaultValues: { prompt: defaultPrompt || "" },
  });

  const submit = form.handleSubmit((values) => {
    onSubmitPrompt(values.prompt);
  });

  return (
    <div className="mx-auto w-full max-w-3xl">
      <h1 className="mb-6 text-center text-5xl font-medium text-slate-100">
        Where should we begin?
      </h1>
      <form onSubmit={submit} className="rounded-3xl border border-slate-700 bg-[#2a2d35] p-3 shadow-lg">
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="rounded-md p-2 text-slate-300 hover:bg-slate-700"
            aria-label="Add attachment"
          >
            <Plus size={20} />
          </button>
          <Input
            {...form.register("prompt")}
            placeholder="Describe the Zendesk setup you want generated..."
            className="h-12 flex-1 border-none bg-transparent text-base focus:border-none"
          />
          <Button type="button" variant="ghost" size="sm" className="rounded-full p-2 text-slate-300">
            <Mic size={18} />
          </Button>
          <Button
            type="submit"
            size="sm"
            className="h-10 w-10 rounded-full bg-white text-slate-900 hover:bg-slate-200"
            disabled={isLoading}
          >
            <ArrowUp size={18} />
          </Button>
        </div>
      </form>
      {form.formState.errors.prompt && (
        <p className="mt-2 text-center text-sm text-rose-400">{form.formState.errors.prompt.message}</p>
      )}

      <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
        <button type="button" className="rounded-full border border-slate-700 px-4 py-2 text-sm text-slate-200 hover:bg-slate-800">
          <ImageIcon size={16} className="mr-2 inline" />
          Create an image
        </button>
        <button type="button" className="rounded-full border border-slate-700 px-4 py-2 text-sm text-slate-200 hover:bg-slate-800">
          <PencilLine size={16} className="mr-2 inline" />
          Write or edit
        </button>
        <button type="button" className="rounded-full border border-slate-700 px-4 py-2 text-sm text-slate-200 hover:bg-slate-800">
          <Globe size={16} className="mr-2 inline" />
          Look something up
        </button>
      </div>
    </div>
  );
}
