import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import App from "./App";

vi.mock("./services/api", () => ({
  testApis: vi.fn(async () => ({
    provider: "Groq (OpenAI-compatible)",
    model: "openai/gpt-oss-20b",
    status: "ok",
  })),
  generateBatch: vi.fn(async () => ({
    batch_id: "BATCH-TEST-001",
    status: "preview_ready",
    generated_counts: { triggers: 1 },
    validation_summary: { passed: 1, warnings: 0, blocked: 0 },
  })),
  getJob: vi.fn(async () => ({
    batch_id: "BATCH-TEST-001",
    status: "preview_ready",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    requester: "local-user",
    target_environment: "sandbox",
    mode: "generate_validate_preview",
    status_history: [],
    generated_counts: { triggers: 1 },
    validation_summary: { passed: 1, warnings: 0, blocked: 0 },
    metadata: {},
  })),
  getPreview: vi.fn(async () => ({
    batch_id: "BATCH-TEST-001",
    status: "preview_ready",
    planning_summary: { object_type: "triggers" },
    generated_counts: { triggers: 1 },
    validation_summary: { passed: 1, warnings: 0, blocked: 0 },
    records: [
      {
        record_id: "REC-0001",
        object_type: "triggers",
        title: "Route Claims",
        preview_summary: "test",
        validation_status: "passed",
        warnings: [],
        blocked_reason: null,
        import_decision: "pending_review",
        deployable: true,
        conditions: [],
        actions: [],
      },
    ],
  })),
  approveBatch: vi.fn(async () => ({
    batch_id: "BATCH-TEST-001",
    status: "approved",
    summary: { approved: 1, skipped: 0, edit_later: 0 },
    message: "ok",
  })),
}));

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  );
}

test("renders chat style heading", () => {
  renderApp();
  expect(screen.getByText("Where should we begin?")).toBeInTheDocument();
});

test("runs generation flow from prompt submit", async () => {
  renderApp();
  const input = screen.getByPlaceholderText("Describe the Zendesk setup you want generated...");
  fireEvent.change(input, { target: { value: "Create claims trigger flow for broker routing" } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(screen.getByText("Preview Workspace")).toBeInTheDocument();
  });
});
