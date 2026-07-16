import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import App from "./App";
import * as api from "./services/api";

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
  checkZendeskHelpCenterReadiness: vi.fn(async () => ({
    ready: true,
    state: "ready",
    detail: "Help Center is ready.",
    base_url: "https://example.zendesk.com",
    help_center_api_base_url: "https://example.zendesk.com",
    help_center_url: "https://example.zendesk.com/hc/en-us",
    locale: "en-us",
    brand: { id: "3", name: "Main brand", has_help_center: true },
    available_brands: [],
    checks: [],
    instructions: [],
    can_create_structure: true,
    can_create_articles: true,
  })),
  extractAttachment: vi.fn(async () => ({
    filename: "notes.txt",
    mime_type: "text/plain",
    char_count: 20,
    extracted_text: "hello world notes",
    truncated: false,
    warnings: [],
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
  vi.clearAllMocks();
  window.sessionStorage.clear();
  api.generateBatch.mockResolvedValue({
    batch_id: "BATCH-TEST-001",
    status: "received",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    requester: "local-user",
    target_environment: "sandbox",
    mode: "generate_validate_preview",
    status_history: [
      {
        status: "received",
        message: "Batch accepted. Preparing the department manifest, dependency order, and model lanes.",
        at: "2026-01-01T00:00:00Z",
      },
    ],
    generated_counts: {},
    validation_summary: { passed: 0, warnings: 0, blocked: 0 },
    metadata: {},
  });
  api.getJob.mockResolvedValue({
    batch_id: "BATCH-TEST-001",
    status: "preview_ready",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:05Z",
    requester: "local-user",
    target_environment: "sandbox",
    mode: "generate_validate_preview",
    status_history: [],
    generated_counts: { triggers: 1 },
    validation_summary: { passed: 1, warnings: 0, blocked: 0 },
    metadata: {},
  });
});

test("shows full-screen zendesk session gate by default", () => {
  renderApp();
  expect(screen.getByText("Zendesk Session Required")).toBeInTheDocument();
});

test("hides main workspace until Zendesk credentials are validated", async () => {
  renderApp();
  expect(screen.queryByText("What would you like to build today?")).not.toBeInTheDocument();
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
    expect(screen.getByText("What would you like to build today?")).toBeInTheDocument();
  });
  const input = screen.getByPlaceholderText(/Create a trigger that closes tickets/i);
  await waitFor(() => {
    expect(input).not.toBeDisabled();
  });
  fireEvent.change(input, { target: { value: "Create claims trigger flow for broker routing" } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(screen.getByText("Review and Confirm")).toBeInTheDocument();
  });
  await waitFor(() => {
    expect(input.value).toBe("");
  });
});

test("shows verified live wave activity while the async batch is running", async () => {
  api.getJob.mockResolvedValue({
    batch_id: "BATCH-TEST-001",
    status: "wave_execution",
    created_at: new Date(Date.now() - 15_000).toISOString(),
    updated_at: new Date().toISOString(),
    requester: "local-user",
    target_environment: "sandbox",
    mode: "generate_validate_preview",
    status_history: [
      {
        status: "wave_execution",
        message: "Wave 2/5, Claims & Incidents: building 3 trigger records in chunk 1/2 using Gemini as the primary drafting model.",
        at: new Date().toISOString(),
        source: "deterministic",
        wave: 2,
        department: "Claims & Incidents",
        object_type: "triggers",
      },
    ],
    generated_counts: { groups: 7, ticket_fields: 8 },
    validation_summary: { passed: 0, warnings: 0, blocked: 0 },
    metadata: {},
  });

  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByText("What would you like to build today?")).toBeInTheDocument();
  });
  const input = screen.getByPlaceholderText(/Create a trigger that closes tickets/i);
  fireEvent.change(input, { target: { value: "Build the claims operating model." } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(screen.getByText("Live build activity")).toBeInTheDocument();
  });
  await waitFor(() => {
    expect(screen.getAllByText(/Claims & Incidents/).length).toBeGreaterThan(0);
  });
  expect(screen.getByText("Why this matters")).toBeInTheDocument();
  expect(screen.getByText("Next checkpoint")).toBeInTheDocument();
  expect(screen.getByText(/39% \| ETA/)).toBeInTheDocument();
  expect(screen.queryByText("Review and Confirm")).not.toBeInTheDocument();
});

test("sends selected object focus in generate payload", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByText("What would you like to build today?")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: "Triggers" }));
  fireEvent.click(screen.getByRole("button", { name: "Macros" }));
  const input = screen.getByPlaceholderText(/Create a trigger that closes tickets/i);
  fireEvent.change(input, { target: { value: "Create a trigger and macro for claims." } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(api.generateBatch).toHaveBeenCalled();
  });
  const lastCallArgs = api.generateBatch.mock.calls.at(-1)?.[0] || {};
  expect(lastCallArgs.focus_object_types).toEqual(expect.arrayContaining(["triggers", "macros"]));
});

test("does not show Test APIs top action", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));
  await waitFor(() => {
    expect(screen.getByText("What would you like to build today?")).toBeInTheDocument();
  });
  expect(screen.queryByRole("button", { name: "Test APIs" })).not.toBeInTheDocument();
});

test("approve with client saves skipped decisions and does not deploy when no rows are approved", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByText("What would you like to build today?")).toBeInTheDocument();
  });

  const input = screen.getByPlaceholderText(/Create a trigger that closes tickets/i);
  fireEvent.change(input, { target: { value: "Create claims trigger flow for broker routing" } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(screen.getByText("Review and Confirm")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  fireEvent.click(screen.getByRole("button", { name: "Deploy Support Objects" }));

  await waitFor(() => {
    expect(api.approveBatch).toHaveBeenCalled();
  });
  expect(api.deployBatch).not.toHaveBeenCalled();
});

test("new chat resets active batch workspace state", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByText("What would you like to build today?")).toBeInTheDocument();
  });

  const input = screen.getByPlaceholderText(/Create a trigger that closes tickets/i);
  fireEvent.change(input, { target: { value: "Create claims trigger flow for broker routing" } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => {
    expect(screen.getByText("Review and Confirm")).toBeInTheDocument();
  });

  fireEvent.click(screen.getAllByRole("button", { name: "New Chat" })[0]);

  await waitFor(() => {
    expect(screen.queryByText("Review and Confirm")).not.toBeInTheDocument();
  });
});
