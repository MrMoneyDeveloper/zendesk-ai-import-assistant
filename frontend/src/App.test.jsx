import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

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
  listJobs: vi.fn(async () => ({
    jobs: [
      {
        batch_id: "BATCH-TEST-001",
        status: "preview_ready",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        requester: "local-user",
        prompt_preview: "Create claims trigger flow for broker routing",
      },
    ],
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
  deployBatch: vi.fn(async () => ({
    batch_id: "BATCH-TEST-001",
    status: "deployed",
    summary: { attempted: 1, deployed: 1, failed: 0, skipped: 0 },
    message: "ok",
    results: [],
    metadata: {},
  })),
  getIntegrationsStatus: vi.fn(async () => ({
    appscript: { configured: true, health: "ok" },
    sheets_backend_mode: { service_account_mode_enabled: false },
    zendesk: { configured: false, deploy_endpoint_enabled: true },
  })),
  validateZendeskCredentials: vi.fn(async () => ({
    ok: true,
    detail: "Zendesk credentials are valid.",
    subdomain: "example",
    base_url: "https://example.zendesk.com",
    authenticated_user: "Local User",
    authenticated_user_role: "admin",
  })),
  getZendeskContext: vi.fn(async () => ({
    ok: true,
    detail: "Zendesk catalogs fetched.",
    base_url: "https://example.zendesk.com",
    fetched_at: "2026-01-01T00:00:00Z",
    warnings: [],
    catalogs: {
      groups: [{ object_type: "group", id: "1", name: "Support" }],
      ticket_forms: [{ object_type: "ticket_form", id: "2", name: "Default form" }],
      brands: [{ object_type: "brand", id: "3", name: "Main brand" }],
      help_centers: [],
      categories: [],
      sections: [],
    },
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

beforeEach(() => {
  window.sessionStorage.clear();
});

test("shows full-screen zendesk session gate by default", () => {
  renderApp();
  expect(screen.getByText("Zendesk Session Required")).toBeInTheDocument();
});

test("hides main workspace until Zendesk credentials are validated", async () => {
  renderApp();
  expect(screen.queryByText("Where should we begin?")).not.toBeInTheDocument();
});

test("runs generation flow from prompt submit", async () => {
  renderApp();
  const subdomainInput = screen.getByPlaceholderText("example: acme");
  const emailInput = screen.getByPlaceholderText("agent@acme.com");
  const tokenInput = screen.getByPlaceholderText("Zendesk API token");
  fireEvent.change(subdomainInput, { target: { value: "acme" } });
  fireEvent.change(emailInput, { target: { value: "admin@acme.com" } });
  fireEvent.change(tokenInput, { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByText("Where should we begin?")).toBeInTheDocument();
  });
  const input = screen.getByPlaceholderText("Describe the Zendesk setup you want generated...");
  await waitFor(() => {
    expect(input).not.toBeDisabled();
  });
  fireEvent.change(input, { target: { value: "Create claims trigger flow for broker routing" } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(screen.getByText("Preview Workspace")).toBeInTheDocument();
  });
});
