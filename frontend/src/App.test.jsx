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
  askContextQuestion: vi.fn(async (payload) => ({
    question_mode: payload.question_mode,
    answer: "The Route Claims trigger assigns matching tickets to the Support group.",
    findings: ["One synchronized trigger matched the question."],
    impact: [],
    risks: [],
    recommended_checks: ["Confirm the Support group is the intended destination."],
    cannot_verify: [],
    citations: [
      {
        source: "zendesk",
        object_type: "trigger",
        object_id: "44",
        name: "Route Claims",
        evidence: "The synchronized action uses group ID 1.",
      },
    ],
    confidence: 0.94,
    read_only: true,
    scope: { catalog_total: 4, catalog_included: 1, proposal_total: 0 },
    warnings: [],
    provider: "Gemini",
    model: "gemini-test",
    fallback_used: false,
    usage: { total_tokens: 200 },
    answered_at: "2026-01-01T00:00:03Z",
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
    sync_id: "SYNC-TEST-001",
    complete: true,
    catalog_counts: { groups: 1, ticket_forms: 1, brands: 1, triggers: 1 },
    page_counts: { groups: 1, ticket_forms: 1, brands: 1, triggers: 1 },
    warnings: [],
    catalogs: {
      groups: [{ object_type: "group", id: "1", name: "Support" }],
      ticket_forms: [{ object_type: "ticket_form", id: "2", name: "Default form" }],
      brands: [{ object_type: "brand", id: "3", name: "Main brand" }],
      triggers: [
        {
          object_type: "trigger",
          id: "44",
          name: "Route Claims",
          catalog_key: "triggers",
          editable: true,
          updated_at: "2026-01-01T00:00:00Z",
          snapshot_hash: "sha256:test-trigger",
          snapshot: {
            id: 44,
            title: "Route Claims",
            active: true,
            updated_at: "2026-01-01T00:00:00Z",
            conditions: {
              all: [{ field: "status", operator: "is", value: "new" }],
              any: [{ field: "tags", operator: "includes", value: "claim" }],
            },
            actions: [{ field: "group_id", value: "1" }],
          },
        },
      ],
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
  window.localStorage.clear();
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
  expect(screen.queryByText("What would you like to do today?")).not.toBeInTheDocument();
});

test("applies light and dark theme state to the document root", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => expect(screen.getByText("What would you like to do today?")).toBeInTheDocument());
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  expect(document.documentElement).not.toHaveClass("dark");

  fireEvent.click(screen.getByRole("button", { name: "Switch to purple theme" }));
  expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  expect(document.documentElement).toHaveClass("dark");

  fireEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  expect(document.documentElement).not.toHaveClass("dark");
});

test("keeps the prompt locked until an operation mode is selected", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByText("Choose Create, Update, Create or update, or Ask first.")).toBeInTheDocument();
  });
  const input = screen.getByPlaceholderText(/Create a trigger that closes tickets/i);
  expect(input).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: /Create new/i }));
  await waitFor(() => {
    expect(screen.getByPlaceholderText(/Create a trigger that closes tickets/i)).not.toBeDisabled();
  });
});

test("offers four modes and answers a read-only instance question without generation", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByRole("button", { name: /Create new/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Update existing/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create or update/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ask or verify/i })).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: /Ask or verify/i }));
  const input = await screen.findByPlaceholderText(/Which triggers route claims tickets/i);
  await waitFor(() => expect(input).not.toBeDisabled());
  fireEvent.change(input, { target: { value: "Which trigger routes claims tickets?" } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => expect(api.askContextQuestion).toHaveBeenCalledOnce());
  expect(api.generateBatch).not.toHaveBeenCalled();
  expect(api.askContextQuestion.mock.calls[0][0]).toMatchObject({
    question_mode: "instance",
    instance_sync_id: "SYNC-TEST-001",
  });
  expect(await screen.findByText(/assigns matching tickets to the Support group/i)).toBeInTheDocument();
  expect(screen.getByText("Read-only")).toBeInTheDocument();
});

test("submits an exact synchronized target for update mode", async () => {
  renderApp();
  fireEvent.change(screen.getByPlaceholderText("example: acme"), { target: { value: "acme" } });
  fireEvent.change(screen.getByPlaceholderText("agent@acme.com"), { target: { value: "admin@acme.com" } });
  fireEvent.change(screen.getByPlaceholderText("Zendesk API token"), { target: { value: "tok_test" } });
  fireEvent.click(screen.getByRole("button", { name: "Validate and Unlock" }));

  await waitFor(() => {
    expect(screen.getByRole("button", { name: /Update existing/i })).toBeInTheDocument();
  });
  fireEvent.click(screen.getByRole("button", { name: /Update existing/i }));

  const typeSelect = await screen.findByRole("combobox", { name: "Zendesk object type" });
  await waitFor(() => {
    expect(screen.getByRole("option", { name: "Triggers (1)" })).toBeInTheDocument();
  });
  fireEvent.change(typeSelect, { target: { value: "triggers" } });

  const targetSelect = await screen.findByRole("combobox", { name: "Existing Zendesk object" });
  await waitFor(() => {
    expect(screen.getByRole("option", { name: "Route Claims (ID 44)" })).toBeInTheDocument();
  });
  fireEvent.change(targetSelect, { target: { value: "trigger:44" } });

  const input = screen.getByPlaceholderText(/Keep the current routing logic/i);
  await waitFor(() => expect(input).not.toBeDisabled());
  expect(screen.getByRole("combobox", { name: "Dependency Behavior" })).toHaveValue("force_existing_only");
  expect(screen.getByRole("combobox", { name: "Dependency Behavior" })).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "Existing Object Behavior" })).toHaveValue("overwrite_existing");
  expect(screen.getByRole("combobox", { name: "Existing Object Behavior" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "Add the VIP tag while preserving all current routing logic." } });
  fireEvent.submit(input.closest("form"));

  await waitFor(() => expect(api.generateBatch).toHaveBeenCalled());
  const payload = api.generateBatch.mock.calls.at(-1)?.[0] || {};
  expect(payload.operation_mode).toBe("update");
  expect(payload.instance_sync_id).toBe("SYNC-TEST-001");
  expect(payload.dependency_mode).toBe("force_existing_only");
  expect(payload.focus_object_types).toEqual(["triggers"]);
  expect(payload.update_target).toMatchObject({
    object_type: "trigger",
    id: "44",
    name: "Route Claims",
    snapshot_hash: "sha256:test-trigger",
  });
  expect(payload.update_target.snapshot.conditions.any).toHaveLength(1);
  expect(payload.related_objects).toHaveLength(1);
  expect(payload.related_objects[0].id).toBe("44");
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
    expect(screen.getByText("What would you like to do today?")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByRole("button", { name: /Create new/i }));
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
    expect(screen.getByText("What would you like to do today?")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByRole("button", { name: /Create new/i }));
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
    expect(screen.getByText("What would you like to do today?")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: /Create new/i }));
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
    expect(screen.getByText("What would you like to do today?")).toBeInTheDocument();
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
    expect(screen.getByText("What would you like to do today?")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: /Create new/i }));
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
    expect(screen.getByText("What would you like to do today?")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: /Create new/i }));
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
