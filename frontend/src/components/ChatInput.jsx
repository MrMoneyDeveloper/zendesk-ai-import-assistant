import { useState } from "react";
import { generateData } from "../services/api";

export default function ChatInput({ setResult, setLoading }) {
  const [prompt, setPrompt] = useState("");

  const handleGenerate = async () => {
    if (!prompt) return;
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
    <div>
      <input
        placeholder="e.g. Create billing trigger"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        style={{ width: "300px", marginRight: "10px" }}
      />
      <button onClick={handleGenerate}>Generate</button>
    </div>
  );
}
