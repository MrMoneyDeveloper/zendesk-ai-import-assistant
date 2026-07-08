import { useState } from "react";
import { generateData } from "../services/api";

export default function ChatInput({ setResult, setLoading }) {
  const [prompt, setPrompt] = useState("");

  const handleGenerate = async () => {
    if (!prompt.trim()) return;

    setLoading(true);

    try {
      const data = await generateData(prompt);
      setResult(data);
    } catch (err) {
      console.error(err);
      alert("Backend error - check console.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-4xl">
      <div className="rounded-3xl border border-[#7823B8]/30 bg-[#120522]/90 p-5 shadow-lg backdrop-blur">
        
        <textarea
          placeholder="Describe what you want Zendesk to create..."
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          className="
            min-h-[140px]
            w-full
            resize-none
            rounded-2xl
            border
            border-[#7823B8]/20
            bg-[#0D0618]
            p-4
            text-white
            outline-none
            transition
            focus:border-[#7823B8]
          "
        />

        <div className="mt-4 flex items-center justify-between">
          <div>
            <p className="text-sm text-slate-300">
              CX Experts Assistant
            </p>

            <p className="text-xs text-slate-500">
              Generate forms, triggers, macros and workflows
            </p>
          </div>

          <button
            onClick={handleGenerate}
            className="
              rounded-xl
              bg-[#7823B8]
              px-6
              py-3
              font-medium
              text-white
              transition
              hover:bg-[#8E3AD0]
              hover:shadow-lg
            "
          >
            ✨ Generate
          </button>
        </div>
      </div>
    </div>
  );
}