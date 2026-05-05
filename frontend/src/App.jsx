import { useState } from "react";
import ChatInput from "./components/ChatInput";
import Preview from "./components/Preview";

function App() {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  return (
    <div style={{ padding: "20px" }}>
      <h1>AI Zendesk Import Assistant</h1>
      <ChatInput setResult={setResult} setLoading={setLoading} />
      {loading && <p>Generating...</p>}
      <Preview result={result} />
    </div>
  );
}

export default App;