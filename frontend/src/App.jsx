import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  BookOpen,
  Boxes,
  CheckCircle2,
  ChevronRight,
  CirclePlus,
  FileText,
  Gauge,
  GitBranch,
  Grid2X2,
  Library,
  LoaderCircle,
  LogOut,
  MessageSquare,
  Moon,
  PanelRightOpen,
  Plus,
  Settings,
  ShieldCheck,
  Sparkles,
  Sun,
  Workflow,
  Zap,
} from "lucide-react";

import PromptComposer from "./components/chat/PromptComposer";
import OperationModeSelector from "./components/chat/OperationModeSelector";
import PreviewWorkspace from "./components/chat/PreviewWorkspace";
import IntegrationPanel from "./components/chat/IntegrationPanel";
import ZendeskSessionGate from "./components/chat/ZendeskSessionGate";
import { Button } from "./components/ui/button";
import { Card, CardContent } from "./components/ui/card";
import cxHeroBanner from "./assets/cx-hero-banner.png";
import cxHeroLight from "./assets/cx-hero-light.png";
import cxIcon from "./assets/cx-icon.png";
import cxLogo from "./assets/cx-logo.png";
import {
  approveBatch,
  checkZendeskHelpCenterReadiness,
  deployBatch,
  extractAttachment,
  generateBatch,
  controlBatchJob,
  decideCheckpoint,
  getCheckpoints,
  getZendeskContext,
  getIntegrationsStatus,
  getJob,
  listJobs,
  getPreview,
  validateZendeskCredentials,
} from "./services/api";
import { useImportAssistantStore } from "./store/importAssistantStore";

const ZENDESK_SESSION_STORAGE_KEY = "zendesk_session_credentials_v1";

function extractFailurePayload(error) {
  const detail = error?.response?.data?.detail;
  if (detail && typeof detail === "object") {
    const validationErrors = Array.isArray(detail.validation_errors)
      ? detail.validation_errors
      : [];
    const compaction = detail.compaction && typeof detail.compaction === "object"
      ? detail.compaction
      : null;
    return {
      stage: detail.failure_stage || null,
      code: detail.failure_code || null,
      reason: detail.failure_reason || "Request failed.",
      nextStep: detail.next_step || null,
      validationErrors,
      batchId: detail.batch_id || null,
      compaction,
    };
  }
  return null;
}

function parseFailureDetail(error) {
  const structured = extractFailurePayload(error);
  if (structured) {
    const friendlyByCode = {
      request_validation_failed: "Some request inputs are invalid.",
      run_cancelled: "The run was cancelled.",
      rate_limited: "The AI provider is temporarily rate-limited.",
      unsupported_response_format: "The model returned a format we could not use for this step.",
      generator_json_validation_failed: "The model response could not be validated safely.",
      generate_staging_failed: "We generated data, but staging to Apps Script/Sheets failed.",
      generate_validation_failed: "We staged data, but validation failed.",
      generate_preview_failed: "We generated data, but preview assembly failed.",
      generate_runtime_error: "Generation hit an unexpected runtime issue.",
      wave_generation_failed: "A generation wave could not complete.",
      wave_dependency_resolution_failed: "A wave dependency could not be resolved.",
      checkpoint_rejected: "A checkpoint was rejected and the run was cancelled.",
      rollback_partial: "Rollback completed partially.",
      rollback_failed: "Rollback could not complete cleanly.",
    };
    if (
      structured.code === "request_validation_failed"
      && Array.isArray(structured.validationErrors)
      && structured.validationErrors.length > 0
    ) {
      const first = structured.validationErrors[0];
      const path = String(first?.path || "request");
      const message = String(first?.message || "Invalid value.");
      const next = structured.nextStep ? ` Next: ${structured.nextStep}` : "";
      return `[request] ${path}: ${message}.${next}`;
    }
    const friendlyPrefix = friendlyByCode[structured.code] || null;
    if (structured.code === "rate_limited") {
      const reason = structured.reason || "Request was rate-limited by the provider.";
      const next = structured.nextStep
        || "Wait for the rate-limit window to reset, then retry.";
      const stage = structured.stage ? `[${structured.stage}] ` : "";
      const prefix = friendlyPrefix ? `${friendlyPrefix} ` : "";
      return `${stage}${prefix}${reason} Next: ${next}`;
    }
    const stage = structured.stage ? `[${structured.stage}] ` : "";
    const next = structured.nextStep ? ` Next: ${structured.nextStep}` : "";
    const reason = structured.reason || "Request failed.";
    const prefix = friendlyPrefix ? `${friendlyPrefix} ` : "";
    return `${stage}${prefix}${reason}${next}`;
  }
  const detail = error?.response?.data?.detail;
  return detail || error?.message || "Request failed.";
}

const STATUS_STEP_LABELS = {
  received: "Request received.",
  request_validated: "Validating request payload and context.",
  planning: "Planning generation strategy.",
  planned: "Plan created.",
  business_blueprinting: "Compiling business blueprint from your prompt.",
  backlog_building: "Building deterministic dependency backlog.",
  wave_execution: "Executing orchestration waves.",
  schemas_selected: "Selecting schema constraints.",
  generating: "Generating structured records.",
  supervisor_review: "Gemini supervisor reviewing and patching output.",
  generated: "Generation completed.",
  staging: "Writing staged records to Apps Script/Sheets.",
  staged: "Staging completed.",
  validating: "Running safety and validation checks.",
  validated_passed: "Validation passed.",
  validated_warning: "Validation passed with warnings.",
  validated_failed: "Validation failed.",
  preview_ready: "Preview ready for review.",
  checkpoint_pending: "Wave checkpoint is pending review.",
  checkpoint_accepted: "Wave checkpoint accepted.",
  checkpoint_rejected: "Wave checkpoint rejected.",
  approved: "Approval decisions saved.",
  partially_approved: "Partial approval saved.",
  deploying: "Deploying approved records to Zendesk.",
  deployed: "Deployment completed.",
  deployed_partial: "Deployment completed with partial failures.",
  deploy_failed: "Deployment failed.",
  failed: "Run failed.",
};

function humanizeStatusStep(status, message) {
  const base = STATUS_STEP_LABELS[String(status || "").trim()] || status || "processing";
  const detail = String(message || "").trim();
  if (!detail) return base;
  if (String(status || "").trim() === "wave_execution") return detail;
  if (detail.toLowerCase() === base.toLowerCase()) return base;
  return `${base} ${detail}`;
}

function formatDuration(ms) {
  const totalSeconds = Math.max(0, Math.floor((ms || 0) / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes <= 0) return `${seconds}s`;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function objectTypeLabel(value) {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return "Object";
  const mapping = {
    brands: "Brand",
    categories: "Category",
    sections: "Section",
    triggers: "Trigger",
    automations: "Automation",
    macros: "Macro",
    views: "View",
    groups: "Group",
    ticket_fields: "Field",
    ticket_forms: "Form",
    articles: "Article",
  };
  return mapping[text] || text.replace(/_/g, " ");
}

function extractWaveChunkProgress(statusHistory = [], metadata = {}) {
  const history = Array.isArray(statusHistory) ? statusHistory : [];
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const row = history[index] || {};
    const message = String(row.message || "");
    const waveMatch = message.match(/wave\s+(\d+)\/(\d+)/i);
    const chunkMatch = message.match(/chunk\s+(\d+)\/(\d+)/i);
    if (waveMatch) {
      if (String(row.status || "") !== "wave_execution" && !chunkMatch) {
        continue;
      }
      const waveIndex = Number(waveMatch[1]);
      const waveTotal = Number(waveMatch[2]);
      const chunkIndex = Number(chunkMatch?.[1] || 0);
      const chunkTotal = Number(chunkMatch?.[2] || 0);
      const departmentMatch = message.match(/wave\s+\d+\/\d+\s*,\s*([^:]+):/i);
      const objectMatch = message.match(/building\s+\d+\s+([a-z_ ]+?)\s+record/i);
      const department = String(row.department || departmentMatch?.[1] || "").trim();
      const objectType = String(row.object_type || objectMatch?.[1] || "").trim().replace(/\s+/g, "_");
      const completed = /\bcompleted\b/i.test(message);
      const withinWave = completed
        ? 1
        : (chunkTotal > 0 ? Math.min(chunkIndex / chunkTotal, 0.95) : 0.15);
      const progressFraction = waveTotal > 0
        ? Math.min(Math.max((waveIndex - 1 + withinWave) / waveTotal, 0), 1)
        : 0;
      const details = [
        department,
        objectType ? objectTypeLabel(objectType) : "",
        chunkTotal > 0 ? `${chunkIndex}/${chunkTotal}` : "",
      ].filter(Boolean);
      return {
        badge: `Wave ${waveIndex}/${waveTotal}${details.length ? ` | ${details.join(" | ")}` : ""}`,
        chunkIndex,
        chunkTotal,
        waveIndex,
        waveTotal,
        progressFraction,
        progressPercent: Math.round(progressFraction * 100),
      };
    }
    if (chunkMatch) {
      const chunkIndex = Number(chunkMatch[1]);
      const chunkTotal = Number(chunkMatch[2]);
      const progressFraction = chunkTotal > 0 ? chunkIndex / chunkTotal : 0;
      return {
        badge: `Chunk ${chunkMatch[1]}/${chunkMatch[2]}`,
        chunkIndex,
        chunkTotal,
        waveIndex: 0,
        waveTotal: 0,
        progressFraction,
        progressPercent: Math.round(progressFraction * 100),
      };
    }
  }

  const chunking = metadata?.chunking || {};
  const totalChunks = Number(chunking?.total_chunks || 0);
  const chunkRows = Array.isArray(chunking?.chunks) ? chunking.chunks : [];
  if (totalChunks > 0 && chunkRows.length > 0) {
    return {
      badge: `Chunk ${chunkRows.length}/${totalChunks}`,
      chunkIndex: chunkRows.length,
      chunkTotal: totalChunks,
      waveIndex: 0,
      waveTotal: 0,
      progressFraction: chunkRows.length / totalChunks,
      progressPercent: Math.round((chunkRows.length / totalChunks) * 100),
    };
  }
  return {
    badge: "Preparing run",
    chunkIndex: 0,
    chunkTotal: 0,
    waveIndex: 0,
    waveTotal: 0,
    progressFraction: 0,
    progressPercent: 0,
  };
}

const OPERATIONAL_NARRATIVES = {
  received: [
    {
      why: "The run is being given a stable identity before any model work starts, so every later event can be observed and audited.",
      next: "Validate the request, map named departments, and choose the dependency-safe generation path.",
    },
  ],
  request_validated: [
    {
      why: "Prompt constraints and selected Zendesk context must be fixed before the operating model is expanded.",
      next: "Build the department coverage manifest and object targets.",
    },
  ],
  planning: [
    {
      why: "A complete manifest prevents a multi-department design from becoming broad but shallow.",
      next: "Turn departments, forms, fields, topics, and rules into an ordered backlog.",
    },
    {
      why: "Dependencies are planned by name now so Zendesk IDs are resolved only when deployment is approved.",
      next: "Open the first wave only after its required targets are explicit.",
    },
  ],
  business_blueprinting: [
    {
      why: "The blueprint converts business language into measurable coverage for every named department.",
      next: "Expand the blueprint into categories, groups, fields, forms, rules, macros, views, and articles.",
    },
  ],
  backlog_building: [
    {
      why: "Dependency ordering keeps later objects from referencing records that have not been created or verified yet.",
      next: "Start independent work in parallel inside the first available wave.",
    },
  ],
  wave_execution: [
    {
      why: "Stable names let forms, views, and routing rules connect without inventing Zendesk IDs.",
      next: "Finish this bundle, run supervisor and deterministic gates, then commit only verified records.",
    },
    {
      why: "Department-by-department coverage keeps a large operating model detailed instead of merely large.",
      next: "Compare completed records with the remaining manifest before the next dependency wave opens.",
    },
    {
      why: "Each accepted chunk becomes real context for dependent objects, preserving the moving parts across waves.",
      next: "Apply safe targeted patches and retry only a source chunk that fails its gate.",
    },
  ],
  generating: [
    {
      why: "Independent chunks can run in parallel while shared dependencies remain fixed and traceable.",
      next: "Validate the structured result and send its department bundle to supervision.",
    },
    {
      why: "The generator is drafting only the current target, which avoids rewriting records that already passed review.",
      next: "Merge the result into the current wave after structural checks pass.",
    },
  ],
  supervisor_review: [
    {
      why: "Gemini can suggest cleanup, but backend rules decide which patches are safe and whether approval is effective.",
      next: "Merge safe patches, reject unsafe operations, and revalidate the affected records.",
    },
    {
      why: "Only records that exist after patching are allowed into verified memory for later waves.",
      next: "Retry only a rejected source chunk or close the bundle as approved.",
    },
  ],
  staging: [
    {
      why: "Staging creates a reviewable audit trail without writing anything to the Zendesk instance.",
      next: "Persist records and metadata, then run validation over the staged batch.",
    },
  ],
  validating: [
    {
      why: "Validation checks deployability, dependency references, coverage warnings, and blocked decisions before review.",
      next: "Assemble the preview with clear passed, warning, and blocked counts.",
    },
  ],
  preview_ready: [
    {
      why: "The generated operating model is complete enough to inspect while deployment remains under human control.",
      next: "Review warnings and record decisions before any Zendesk write is allowed.",
    },
  ],
};

function buildOperationalNarrative(status, beat = 0) {
  const normalized = String(status || "received").trim().toLowerCase();
  const alias = {
    planned: "planning",
    schemas_selected: "planning",
    generated: "staging",
    staged: "validating",
  }[normalized];
  const rows = OPERATIONAL_NARRATIVES[normalized]
    || (alias ? OPERATIONAL_NARRATIVES[alias] : null)
    || (normalized.startsWith("validated") ? OPERATIONAL_NARRATIVES.validating : null)
    || OPERATIONAL_NARRATIVES.wave_execution;
  return rows[Math.abs(Number(beat || 0)) % rows.length];
}

function sectionLabel(key) {
  const labels = {
    groups: "Groups",
    ticket_forms: "Forms",
    brands: "Brands",
    triggers: "Triggers",
    automations: "Automations",
    macros: "Macros",
    views: "Views",
    ticket_fields: "Ticket Fields",
    articles: "Articles",
    help_centers: "Help Centers",
    categories: "Categories",
    sections: "Sections",
  };
  return labels[key] || key;
}

const TERMINAL_BATCH_STATUSES = new Set([
  "preview_ready",
  "clarification_required",
  "failed",
  "approved",
  "partially_approved",
  "deployed",
  "deployed_partial",
  "deploy_failed",
]);

const PREVIEW_POLL_ACTIVE_STATUSES = new Set([
  "staging",
  "staged",
  "validating",
  "validated_passed",
  "validated_warning",
  "validated_failed",
  "preview_ready",
]);

const FOCUS_TO_CATALOG_KEYS = {
  brands: ["brands"],
  categories: ["categories", "help_centers", "brands"],
  sections: ["sections", "categories", "help_centers", "brands"],
  triggers: ["triggers", "groups", "ticket_forms", "brands"],
  automations: ["automations", "groups", "ticket_forms", "brands"],
  macros: ["macros", "groups", "ticket_forms", "ticket_fields"],
  views: ["views", "groups", "ticket_forms", "ticket_fields"],
  groups: ["groups", "brands"],
  ticket_forms: ["ticket_forms", "ticket_fields", "groups", "brands"],
  ticket_fields: ["ticket_fields", "ticket_forms"],
  articles: ["articles", "help_centers", "categories", "sections"],
};

const UPDATE_FOCUS_BY_OBJECT_TYPE = {
  brand: "brands",
  category: "categories",
  section: "sections",
  trigger: "triggers",
  automation: "automations",
  macro: "macros",
  view: "views",
  group: "groups",
  ticket_form: "ticket_forms",
  ticket_field: "ticket_fields",
  article: "articles",
};

function compactContextReference(entry, { includeSnapshot = false } = {}) {
  if (!entry) return null;
  return {
    object_type: entry.object_type,
    id: String(entry.id),
    name: entry.name,
    description: entry.description || null,
    catalog_key: entry.catalog_key || null,
    updated_at: entry.updated_at || null,
    snapshot_hash: entry.snapshot_hash || null,
    editable: entry.editable !== false,
    ...(includeSnapshot ? { snapshot: entry.snapshot || null } : {}),
  };
}

function compactReferenceCatalog(catalog) {
  return Object.fromEntries(
    Object.entries(catalog || {}).map(([key, entries]) => [
      key,
      (entries || []).map((entry) => compactContextReference(entry)).filter(Boolean),
    ])
  );
}

const NAV_ITEMS = [
  { label: "New Chat", icon: Plus, active: true },
  { label: "Dashboard", icon: Gauge },
  { label: "All Chats", icon: MessageSquare },
  { label: "Templates", icon: Grid2X2 },
  { label: "Knowledge Base", icon: Boxes },
  { label: "Integrations", icon: GitBranch },
  { label: "Settings", icon: Settings },
];

const QUICK_ACTIONS = [
  { label: "Trigger", detail: "Create automation triggers", icon: CirclePlus, color: "text-violet-500", prompt: "Create an automation trigger" },
  { label: "Form", detail: "Build ticket forms", icon: FileText, color: "text-blue-500", prompt: "Build a ticket form" },
  { label: "Macro", detail: "Create agent macros", icon: Zap, color: "text-amber-500", prompt: "Create an agent macro" },
  { label: "Workflow", detail: "Build business workflows", icon: Workflow, color: "text-emerald-500", prompt: "Build a business workflow" },
  { label: "Article", detail: "Create help center articles", icon: BookOpen, color: "text-pink-500", prompt: "Create a help center article" },
];

const EXAMPLE_PROMPTS = [
  { label: "Create SLA policy for VIP customers", icon: Sparkles, color: "text-violet-500", prompt: "Create an SLA policy for VIP customers with 1 hour first response time and priority escalation" },
  { label: "Auto-assign tickets based on priority", icon: Gauge, color: "text-blue-500", prompt: "Create a trigger to auto-assign tickets to the appropriate group based on ticket priority" },
  { label: "Send follow-up email after resolution", icon: MessageSquare, color: "text-amber-500", prompt: "Create an automation that sends a follow-up email to the customer 24 hours after a ticket is resolved" },
  { label: "Create help center article for refunds", icon: Library, color: "text-pink-500", prompt: "Create a help center article explaining the refund process, eligibility criteria, and how to submit a refund request" },
];

const FOCUS_CHIPS = [
  { key: "brands", label: "Brands" },
  { key: "categories", label: "Categories" },
  { key: "sections", label: "Sections" },
  { key: "triggers", label: "Triggers" },
  { key: "automations", label: "Automations" },
  { key: "macros", label: "Macros" },
  { key: "views", label: "Views" },
  { key: "groups", label: "Groups" },
  { key: "ticket_forms", label: "Forms" },
  { key: "ticket_fields", label: "Fields" },
  { key: "articles", label: "Articles" },
];

function readZendeskSessionCredentials() {
  try {
    const raw = sessionStorage.getItem(ZENDESK_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed?.subdomain || !parsed?.email || !parsed?.api_token) return null;
    return {
      subdomain: String(parsed.subdomain),
      email: String(parsed.email),
      api_token: String(parsed.api_token),
    };
  } catch {
    return null;
  }
}

function writeZendeskSessionCredentials(credentials) {
  sessionStorage.setItem(ZENDESK_SESSION_STORAGE_KEY, JSON.stringify(credentials));
}

function clearZendeskSessionCredentials() {
  sessionStorage.removeItem(ZENDESK_SESSION_STORAGE_KEY);
}

function App() {
  const queryClient = useQueryClient();

  const [darkMode, setDarkMode] = useState(() => {
    return localStorage.getItem("theme") === "dark";
  });

  useEffect(() => {
    document.documentElement.setAttribute(
      "data-theme",
      darkMode ? "dark" : "light"
    );
    localStorage.setItem("theme", darkMode ? "dark" : "light");
  }, [darkMode]);

  const [decisions, setDecisions] = useState({});
  const [activityLogs, setActivityLogs] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [historySearch, setHistorySearch] = useState("");
  const [chatHistory, setChatHistory] = useState([]);
  const [externalPrompt, setExternalPrompt] = useState("");
  const [selectedContext, setSelectedContext] = useState({});
  const [operationMode, setOperationMode] = useState(null);
  const [updateTarget, setUpdateTarget] = useState(null);
  const [dependencyMode, setDependencyMode] = useState("match_existing_or_create_new");
  const [onExistingMode, setOnExistingMode] = useState("create_new");
  const [existingItemBehavior, setExistingItemBehavior] = useState("relate_or_update");
  const [showExistingContext, setShowExistingContext] = useState(false);
  const [showAdvancedCatalog, setShowAdvancedCatalog] = useState(false);
  const [showProcessingDetails, setShowProcessingDetails] = useState(false);
  const [inferenceAssumptionMessages, setInferenceAssumptionMessages] = useState([]);
  const [lastFailureDetail, setLastFailureDetail] = useState(null);
  const [focusObjectTypes, setFocusObjectTypes] = useState([]);
  const [attachments, setAttachments] = useState([]);
  const [activeRunStartedAtMs, setActiveRunStartedAtMs] = useState(null);
  const [helpCenterUrl, setHelpCenterUrl] = useState("");
  const [helpCenterArticleMode, setHelpCenterArticleMode] = useState("draft");
  const [helpCenterReadiness, setHelpCenterReadiness] = useState(null);
  const [processingClockMs, setProcessingClockMs] = useState(() => Date.now());
  const [chatSessionId, setChatSessionId] = useState(0);
  const lastContextSyncRef = useRef("");
  const lastContextErrorRef = useRef("");
  const conversationEndRef = useRef(null);
  const terminalBatchNotifiedRef = useRef(new Set());
  const [zendeskValidated, setZendeskValidated] = useState(false);
  const [bootZendeskSession] = useState(() => readZendeskSessionCredentials());
  const [zendeskCredentials, setZendeskCredentials] = useState(() =>
    bootZendeskSession || {
      subdomain: "",
      email: "",
      api_token: "",
    }
  );

  const { batchId, setBatchId, resetFlow } = useImportAssistantStore();

  useEffect(() => {
    if (typeof conversationEndRef.current?.scrollIntoView === "function") {
      conversationEndRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [timeline.length]);

  const appendActivity = useCallback((level, message) => {
    setActivityLogs((prev) => [
      {
        at: new Date().toISOString(),
        level,
        message,
      },
      ...prev,
    ]);
  }, []);

  const appendTimeline = useCallback((role, text) => {
    setTimeline((prev) => [
      ...prev,
      {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        role,
        text,
        at: new Date().toISOString(),
      },
    ]);
  }, []);

  const notifyGenerationTerminal = useCallback((data) => {
    const resolvedBatchId = String(data?.batch_id || "").trim();
    const status = String(data?.status || "").trim();
    if (!resolvedBatchId || !["preview_ready", "failed", "clarification_required"].includes(status)) {
      return;
    }
    if (terminalBatchNotifiedRef.current.has(resolvedBatchId)) return;
    terminalBatchNotifiedRef.current.add(resolvedBatchId);
    setBatchId(resolvedBatchId);

    if (status === "failed") {
      const failure = data?.metadata?.failure || {};
      const reason = String(failure.failure_reason || "Generation failed in the background.");
      const nextStep = String(failure.next_step || "Review the failed stage and retry the prompt.");
      setLastFailureDetail({
        stage: failure.failure_stage || "generate",
        code: failure.failure_code || "generate_runtime_error",
        reason,
        nextStep,
      });
      appendActivity("error", `${reason} Next: ${nextStep}`);
      appendTimeline("assistant", `Generation stopped: ${reason} Next: ${nextStep}`);
      queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
      return;
    }

    if (status === "clarification_required") {
      appendActivity("warning", `Batch ${resolvedBatchId} needs clarification before generation can continue.`);
      appendTimeline("assistant", "I need a little more detail before opening the generation waves.");
      queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
      return;
    }

    const generationSafety = data?.metadata?.generation_safety;
    const focusDiagnostics = data?.metadata?.focus_diagnostics;
    const assumptions = Array.isArray(data?.metadata?.inference_assumptions)
      ? data.metadata.inference_assumptions
      : [];
    const assumptionMessages = assumptions
      .map((item) => String(item?.message || "").trim())
      .filter(Boolean);
    setInferenceAssumptionMessages(assumptionMessages);
    setChatHistory((prev) => [
      ...prev,
      `assistant: batch ${resolvedBatchId} ready (passed=${data.validation_summary?.passed || 0}, warnings=${data.validation_summary?.warnings || 0}, blocked=${data.validation_summary?.blocked || 0})`,
    ]);
    appendActivity(
      "success",
      `Batch ${resolvedBatchId} ready for review. Validation summary: passed=${data.validation_summary?.passed || 0}, warnings=${data.validation_summary?.warnings || 0}, blocked=${data.validation_summary?.blocked || 0}.`
    );
    if (generationSafety?.blocked) {
      appendTimeline(
        "assistant",
        `Batch ${resolvedBatchId} generated, but deployment is blocked by safety checks. ${generationSafety.reasons?.join(" | ") || ""}`
      );
    } else {
      appendTimeline(
        "assistant",
        `Batch ${resolvedBatchId} is ready for review (${data.validation_summary?.passed || 0} passed, ${data.validation_summary?.warnings || 0} warnings, ${data.validation_summary?.blocked || 0} blocked).`
      );
    }
    if ((focusDiagnostics?.mismatch_count || 0) > 0) {
      appendActivity(
        "warning",
        `Object focus mismatch: ${focusDiagnostics.mismatch_count} generated records are outside selected focus (${(focusDiagnostics.generated_types || []).join(", ")}).`
      );
    }
    if (assumptionMessages.length > 0) {
      appendActivity("warning", `Assumptions applied: ${assumptionMessages.join(" | ")}`);
      appendTimeline("assistant", `Assumptions applied: ${assumptionMessages.join(" | ")}`);
    }
    queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
    queryClient.invalidateQueries({ queryKey: ["job", resolvedBatchId] });
    queryClient.invalidateQueries({ queryKey: ["checkpoints", resolvedBatchId] });
    queryClient.invalidateQueries({ queryKey: ["preview", resolvedBatchId] });
  }, [appendActivity, appendTimeline, queryClient, setBatchId]);

  const generateMutation = useMutation({
    mutationFn: generateBatch,
    onMutate: () => {
      setBatchId(null);
      setHelpCenterReadiness(null);
      setHelpCenterArticleMode("draft");
      setActiveRunStartedAtMs(Date.now());
      setLastFailureDetail(null);
      setShowProcessingDetails(true);
      appendActivity("info", "Started generate -> stage -> validate pipeline.");
      appendTimeline("assistant", "I am preparing the operating-model run and will show each verified checkpoint here.");
    },
    onSuccess: (data) => {
      setBatchId(data.batch_id);
      queryClient.setQueryData(["job", data.batch_id], data);
      if (["preview_ready", "failed", "clarification_required"].includes(String(data?.status || ""))) {
        notifyGenerationTerminal(data);
      } else {
        appendActivity("info", `Batch ${data.batch_id} is live. Progress polling is attached.`);
        appendTimeline(
          "assistant",
          `Batch ${data.batch_id} is live. I am mapping the request before the first dependency wave opens.`
        );
      }
      queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      const failurePayload = extractFailurePayload(error);
      setLastFailureDetail(failurePayload);
      if (failurePayload?.batchId) {
        setBatchId(failurePayload.batchId);
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
        queryClient.invalidateQueries({ queryKey: ["job", failurePayload.batchId] });
      }
      appendActivity("error", detail);
      appendTimeline("assistant", `Generation failed: ${detail}`);
    },
  });

  const attachmentExtractMutation = useMutation({
    mutationFn: extractAttachment,
  });

  const integrationsQuery = useQuery({
    queryKey: ["integrations-status"],
    queryFn: getIntegrationsStatus,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const data = query.state.data;
      const appscriptHealth = data?.appscript?.health;
      if (generateMutation.isPending) {
        return 15_000;
      }
      if (appscriptHealth === "ok") {
        return false;
      }
      return 20_000;
    },
  });

  const jobsQuery = useQuery({
    queryKey: ["jobs-list"],
    queryFn: () => listJobs(40),
    refetchInterval: 45_000,
    refetchOnWindowFocus: false,
  });

  const zendeskContextQuery = useQuery({
    queryKey: ["zendesk-context", zendeskCredentials?.subdomain, zendeskCredentials?.email],
    queryFn: () => getZendeskContext(zendeskCredentials),
    enabled: Boolean(
      zendeskValidated
      && zendeskCredentials?.subdomain
      && zendeskCredentials?.email
      && zendeskCredentials?.api_token
    ),
    staleTime: 60000,
    refetchInterval: false,
  });

  const jobQuery = useQuery({
    queryKey: ["job", batchId],
    queryFn: () => getJob(batchId),
    enabled: Boolean(batchId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status) return 1_500;
      if (
        [
          "preview_ready",
          "failed",
          "approved",
          "partially_approved",
          "deployed",
          "deployed_partial",
          "deploy_failed",
        ].includes(status)
      ) {
        return false;
      }
      return 1_500;
    },
  });

  useEffect(() => {
    const status = String(jobQuery.data?.status || "");
    if (["preview_ready", "failed", "clarification_required"].includes(status)) {
      const timer = window.setTimeout(() => notifyGenerationTerminal(jobQuery.data), 0);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [jobQuery.data, notifyGenerationTerminal]);

  const approveMutation = useMutation({
    mutationFn: approveBatch,
    onMutate: () => {
      appendActivity("info", "Saving approval decisions to Apps Script.");
      appendTimeline("assistant", "Saving approval decisions...");
    },
    onSuccess: (data) => {
      appendActivity(
        "success",
        `Approval saved. approved=${data?.summary?.approved || 0}, skipped=${data?.summary?.skipped || 0}, edit_later=${data?.summary?.edit_later || 0}.`
      );
      appendTimeline("assistant", "Approval decisions saved.");
      if (batchId) {
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
        queryClient.invalidateQueries({ queryKey: ["job", batchId] });
        queryClient.invalidateQueries({ queryKey: ["checkpoints", batchId] });
        queryClient.invalidateQueries({ queryKey: ["preview", batchId] });
      }
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      appendActivity("error", detail);
      appendTimeline("assistant", `Approval failed: ${detail}`);
    },
  });

  const deployMutation = useMutation({
    mutationFn: deployBatch,
    onMutate: (variables) => {
      const scope = variables?.deployment_scope === "help_center" ? "Help Center" : "Support";
      appendActivity("info", `Deploying approved ${scope} records to the live Zendesk instance.`);
      appendTimeline("assistant", `Deploying approved ${scope} records to Zendesk...`);
    },
    onSuccess: (data) => {
      const firstFailure = (data?.results || []).find((item) => item?.deployment_status === "failed");
      const failureSuffix = firstFailure?.execution_message ? ` First failure: ${firstFailure.execution_message}` : "";
      appendActivity(
        data?.status === "deployed" ? "success" : "error",
        `Deploy finished. deployed=${data?.summary?.deployed || 0}, failed=${data?.summary?.failed || 0}, skipped=${data?.summary?.skipped || 0}.${failureSuffix}`
      );
      appendTimeline(
        "assistant",
        `Deploy complete. deployed=${data?.summary?.deployed || 0}, failed=${data?.summary?.failed || 0}, skipped=${data?.summary?.skipped || 0}.${failureSuffix}`
      );
      if (batchId) {
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
        queryClient.invalidateQueries({ queryKey: ["job", batchId] });
        queryClient.invalidateQueries({ queryKey: ["checkpoints", batchId] });
        queryClient.invalidateQueries({ queryKey: ["preview", batchId] });
      }
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      appendActivity("error", detail);
      appendTimeline("assistant", `Deploy failed: ${detail}`);
    },
  });

  const helpCenterReadinessMutation = useMutation({
    mutationFn: checkZendeskHelpCenterReadiness,
    onMutate: () => {
      appendActivity("info", "Verifying the target brand and Help Center with read-only Zendesk checks.");
    },
    onSuccess: (data) => {
      setHelpCenterReadiness(data);
      appendActivity(data?.ready ? "success" : "warning", data?.detail || "Help Center check completed.");
      appendTimeline("assistant", data?.detail || "Help Center check completed.");
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      setHelpCenterReadiness(null);
      appendActivity("error", detail);
      appendTimeline("assistant", `Help Center verification failed: ${detail}`);
    },
  });

  const runControlMutation = useMutation({
    mutationFn: ({ targetBatchId, action }) =>
      controlBatchJob(targetBatchId, {
        action,
        requested_by: "local-user",
      }),
    onSuccess: (data, variables) => {
      const action = String(variables?.action || "").trim();
      const actionLabel = {
        pause: "Visual pause enabled. Generation continues in background.",
        resume: "Visual pause cleared.",
        cancel: "Cancel requested. Run will stop at the next safe checkpoint.",
        pause_at_next_wave: "Visual pause will enable at the next wave checkpoint.",
        clear_pause_after_wave: "Next-wave visual pause cleared.",
      }[action] || "Run control updated.";
      appendActivity("info", actionLabel);
      appendTimeline("assistant", actionLabel);
      if (data?.batch_id) {
        queryClient.invalidateQueries({ queryKey: ["job", data.batch_id] });
        queryClient.invalidateQueries({ queryKey: ["checkpoints", data.batch_id] });
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
      }
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      appendActivity("error", detail);
      appendTimeline("assistant", `Run control failed: ${detail}`);
    },
  });

  const checkpointsQuery = useQuery({
    queryKey: ["checkpoints", batchId],
    queryFn: () => getCheckpoints(batchId),
    enabled: Boolean(batchId),
    refetchInterval: () => {
      if (!batchId) return false;
      const current = String(jobQuery.data?.status || "");
      if (current && TERMINAL_BATCH_STATUSES.has(current)) {
        return false;
      }
      return 2500;
    },
    refetchOnWindowFocus: false,
  });

  const checkpointDecisionMutation = useMutation({
    mutationFn: ({ targetBatchId, checkpointId, decision, note }) =>
      decideCheckpoint(targetBatchId, checkpointId, {
        decision,
        requested_by: "local-user",
        note: String(note || ""),
      }),
    onSuccess: (data, variables) => {
      const decision = String(variables?.decision || "").trim();
      const checkpointId = String(variables?.checkpointId || "").trim();
      const rollbackStatus = String(data?.rollback?.status || "").trim();
      if (decision === "accept") {
        appendActivity("success", `Checkpoint ${checkpointId} accepted.`);
        appendTimeline("assistant", `Checkpoint ${checkpointId} accepted. Generation continues.`);
      } else {
        appendActivity(
          rollbackStatus === "rollback_completed" ? "warning" : "error",
          rollbackStatus === "rollback_completed"
            ? `Checkpoint ${checkpointId} rejected. Rollback completed and run stopped.`
            : `Checkpoint ${checkpointId} rejected. Rollback is partial or failed; review rollback details.`
        );
        appendTimeline(
          "assistant",
          rollbackStatus === "rollback_completed"
            ? `Checkpoint ${checkpointId} rejected. Run cancelled and rollback completed.`
            : `Checkpoint ${checkpointId} rejected. Run cancelled with rollback status: ${rollbackStatus || "unknown"}.`
        );
      }
      if (data?.batch_id) {
        queryClient.invalidateQueries({ queryKey: ["job", data.batch_id] });
        queryClient.invalidateQueries({ queryKey: ["checkpoints", data.batch_id] });
        queryClient.invalidateQueries({ queryKey: ["preview", data.batch_id] });
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
      }
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      appendActivity("error", detail);
      appendTimeline("assistant", `Checkpoint decision failed: ${detail}`);
    },
  });

  const previewQuery = useQuery({
    queryKey: ["preview", batchId],
    queryFn: () => getPreview(batchId),
    enabled: Boolean(
      batchId
      && (
        PREVIEW_POLL_ACTIVE_STATUSES.has(String(jobQuery.data?.status || ""))
        || ["approved", "partially_approved", "deployed", "deployed_partial", "deploy_failed"].includes(
          String(jobQuery.data?.status || "")
        )
      )
    ),
    refetchInterval: () => {
      if (!batchId) return false;
      const current = String(jobQuery.data?.status || "");
      if (current && TERMINAL_BATCH_STATUSES.has(current)) {
        return false;
      }
      if (approveMutation.isPending || deployMutation.isPending) {
        return 2_500;
      }
      if (PREVIEW_POLL_ACTIVE_STATUSES.has(current)) {
        return generateMutation.isPending ? 2_500 : 5_000;
      }
      return false;
    },
    refetchOnWindowFocus: false,
  });

  const zendeskValidateMutation = useMutation({
    mutationFn: validateZendeskCredentials,
    onSuccess: (data, variables) => {
      const valid = Boolean(data?.ok);
      setZendeskValidated(valid);
      appendActivity(
        valid ? "success" : "error",
        valid ? "Zendesk session credentials validated." : (data?.detail || "Zendesk credential validation failed.")
      );
      if (valid) {
        const persisted = {
          subdomain: variables.subdomain,
          email: variables.email,
          api_token: variables.api_token,
        };
        setZendeskCredentials(persisted);
        writeZendeskSessionCredentials(persisted);
        queryClient.invalidateQueries({ queryKey: ["zendesk-context"] });
      } else {
        clearZendeskSessionCredentials();
        setSelectedContext({});
      }
    },
    onError: () => {
      setZendeskValidated(false);
      clearZendeskSessionCredentials();
      setSelectedContext({});
      appendActivity("error", "Zendesk credential validation request failed.");
    },
  });

  const {
    mutate: mutateZendeskValidation,
    reset: resetZendeskValidation,
    isPending: zendeskValidationPending,
    data: zendeskValidationResult,
    error: zendeskValidationError,
  } = zendeskValidateMutation;

  useEffect(() => {
    if (!bootZendeskSession) return;
    mutateZendeskValidation(bootZendeskSession);
  }, [bootZendeskSession, mutateZendeskValidation]);

  useEffect(() => {
    const syncedAt = zendeskContextQuery.data?.fetched_at;
    if (zendeskContextQuery.data?.ok && syncedAt && syncedAt !== lastContextSyncRef.current) {
      lastContextSyncRef.current = syncedAt;
      appendActivity("info", "Zendesk context synced (brands, groups, forms, help center).");
    }
  }, [zendeskContextQuery.data, appendActivity]);

  useEffect(() => {
    const errorText =
      zendeskContextQuery.error?.response?.data?.detail
      || zendeskContextQuery.error?.message
      || "";
    if (errorText && errorText !== lastContextErrorRef.current) {
      lastContextErrorRef.current = errorText;
      appendActivity("error", `Zendesk context sync failed: ${errorText}`);
    }
  }, [zendeskContextQuery.error, appendActivity]);

  const currentStatus = jobQuery.data?.status || generateMutation.data?.status || null;
  const runControlState = useMemo(() => {
    const metadata = jobQuery.data?.metadata;
    const source = metadata?.run_control || {};
    return {
      pause_requested: Boolean(source?.pause_requested),
      pause_after_wave: Boolean(source?.pause_after_wave),
      cancel_requested: Boolean(source?.cancel_requested),
      cancel_reason: source?.cancel_reason || "",
      updated_at: source?.updated_at || "",
      updated_by: source?.updated_by || "",
    };
  }, [jobQuery.data?.metadata]);

  const checkpoints = useMemo(() => {
    const fromEndpoint = checkpointsQuery.data?.checkpoints;
    if (Array.isArray(fromEndpoint) && fromEndpoint.length > 0) {
      return fromEndpoint;
    }
    const fromMetadata = jobQuery.data?.metadata?.checkpoints;
    return Array.isArray(fromMetadata) ? fromMetadata : [];
  }, [checkpointsQuery.data?.checkpoints, jobQuery.data?.metadata?.checkpoints]);

  const pendingCheckpointCount = checkpoints.filter((item) => item?.status === "pending").length;
  const selectedRelatedObjects = Object.values(selectedContext);
  const previewRecords = previewQuery.data?.records || [];
  const hasActiveBatchRun = Boolean(
    batchId
    && currentStatus
    && !TERMINAL_BATCH_STATUSES.has(String(currentStatus))
  );
  const canControlRun = Boolean(batchId && hasActiveBatchRun);
  const shouldTickProcessingClock = Boolean(
    generateMutation.isPending
    || approveMutation.isPending
    || deployMutation.isPending
    || hasActiveBatchRun
  );

  useEffect(() => {
    if (!shouldTickProcessingClock) return undefined;
    const interval = setInterval(() => {
      setProcessingClockMs(Date.now());
    }, 1000);
    return () => clearInterval(interval);
  }, [shouldTickProcessingClock]);

  const sendRunControl = (action) => {
    if (!batchId) {
      appendActivity("error", "No active batch selected for run control.");
      return;
    }
    runControlMutation.mutate({
      targetBatchId: batchId,
      action,
    });
  };

  const submitCheckpointDecision = (checkpointId, decision) => {
    if (!batchId || !checkpointId) return;
    checkpointDecisionMutation.mutate({
      targetBatchId: batchId,
      checkpointId,
      decision,
      note: "",
    });
  };

  const buildDecisionPayload = () => {
    return Object.entries(decisions)
      .filter(([, importDecision]) => ["approved", "skipped", "edit_later"].includes(importDecision))
      .map(([record_id, import_decision]) => ({ record_id, import_decision }));
  };

  const buildReviewDecisionPayload = () => {
    return (previewRecords || []).map((row) => {
      const local = decisions[row.record_id];
      const resolved = local || row.import_decision || "pending_review";
      return {
        record_id: row.record_id,
        import_decision: resolved,
      };
    })
      .filter((item) => ["approved", "skipped", "edit_later"].includes(item.import_decision));
  };

  const saveApproval = () => {
    if (!batchId) return;
    const records = buildDecisionPayload();
    if (!records.length) {
      appendActivity(
        "info",
        "No local approval changes to save. Set one or more decisions to approved/skipped/edit_later first."
      );
      return;
    }
    approveMutation.mutate({
      batch_id: batchId,
      approved_by: "local-user",
      records,
    });
  };

  const handleExtractAttachment = async (file) => {
    const result = await attachmentExtractMutation.mutateAsync(file);
    const payload = {
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      ...result,
    };
    setAttachments((prev) => [...prev, payload]);
    if (result?.warnings?.length) {
      appendActivity("warning", `Attachment ${result.filename}: ${result.warnings.join(" | ")}`);
    } else {
      appendActivity("success", `Attachment extracted: ${result.filename}`);
    }
    return payload;
  };

  const removeAttachment = (attachmentId) => {
    setAttachments((prev) => prev.filter((item) => item.id !== attachmentId));
  };

  const buildPromptWithAttachments = (promptValue) => {
    const cleanedPrompt = String(promptValue || "").trim();
    if (!attachments.length) return cleanedPrompt;
    const context = attachments
      .map((item, index) => `[Attachment ${index + 1}: ${item.filename}]\n${item.extracted_text}`)
      .join("\n\n");
    return `${cleanedPrompt}\n\nAttachment context:\n${context}`;
  };

  const submitPrompt = async (value) => {
    if (!zendeskValidated) return false;
    if (!operationMode) {
      appendActivity("error", "Choose Create new or Update existing before entering a request.");
      return false;
    }
    if (operationMode === "update" && (!resolvedUpdateTarget || !zendeskContextQuery.data?.sync_id)) {
      appendActivity("error", "Select one synchronized Zendesk object before requesting an update.");
      appendTimeline("assistant", "Choose the exact existing object that this update may change.");
      return false;
    }
    if (operationMode === "create" && dependencyMode === "force_existing_only" && existingItemBehavior === "create_new") {
      appendActivity(
        "error",
        "Switch Existing item behavior to 'Use existing as base' when dependency mode is 'Use existing only (strict)'."
      );
      appendTimeline(
        "assistant",
        "Existing-only dependency mode requires using existing context. Switch the Existing item behavior toggle."
      );
      return false;
    }
    if (operationMode === "create" && dependencyMode === "force_existing_only" && selectedRelatedObjects.length === 0) {
      appendActivity(
        "error",
        "Dependency mode requires existing context. Select one or more groups/forms/brands/sections first."
      );
      appendTimeline("assistant", "Select existing context first when strict existing-only mode is enabled.");
      return false;
    }

    const promptText = String(value || "").trim();
    if (!promptText) return false;

    setInferenceAssumptionMessages([]);
    setLastFailureDetail(null);
    appendTimeline("user", promptText);
    const localHistory = [...chatHistory.slice(-10), `user: ${promptText}`];
    setChatHistory(localHistory);

    const referenceCatalog = compactReferenceCatalog(contextCatalog || {});
    const recentBatchContext = localHistory.slice(-8);
    const promptForModel = buildPromptWithAttachments(promptText);
    const selectedRelatedObjectsForRequest = operationMode === "update"
      ? [compactContextReference(resolvedUpdateTarget, { includeSnapshot: true })]
      : existingItemBehavior === "create_new"
        ? []
        : selectedRelatedObjects.map((item) => compactContextReference(item));
    const effectiveFocusObjectTypes = operationMode === "update"
      ? [UPDATE_FOCUS_BY_OBJECT_TYPE[resolvedUpdateTarget.object_type]]
      : focusObjectTypes;
    const attachmentNote = attachments.length
      ? `Attachment context included from ${attachments.length} file(s): ${attachments.map((item) => item.filename).join(", ")}.`
      : "";

    generateMutation.mutate({
      prompt: promptForModel,
      target_environment: "sandbox",
      mode: "generate_validate_preview",
      requester: "local-user",
      operation_mode: operationMode,
      update_target: operationMode === "update"
        ? compactContextReference(resolvedUpdateTarget, { includeSnapshot: true })
        : null,
      instance_sync_id: operationMode === "update" ? zendeskContextQuery.data?.sync_id : null,
      dependency_mode: operationMode === "update" ? "force_existing_only" : dependencyMode,
      focus_object_types: effectiveFocusObjectTypes,
      related_objects: selectedRelatedObjectsForRequest,
      reference_catalog: referenceCatalog,
      recent_batch_context: recentBatchContext,
      context_notes: [
        `Operation mode: ${operationMode}.`,
        operationMode === "update"
          ? `Only update ${resolvedUpdateTarget.object_type}:${resolvedUpdateTarget.name} (Zendesk ID ${resolvedUpdateTarget.id}).`
          : `Existing item behavior: ${existingItemBehavior === "create_new" ? "ignore_selected_existing_and_create_new" : "use_selected_existing_as_base_or_reference"}.`,
        selectedRelatedObjectsForRequest.length
          ? `Selected context objects: ${selectedRelatedObjectsForRequest.map((obj) => `${obj.object_type}:${obj.name}`).join(", ")}`
          : "No explicit object selections were included in generation context.",
        attachmentNote,
      ].filter(Boolean).join(" "),
    });
    return true;
  };

  const errors = [
    generateMutation.error,
    jobQuery.error,
    checkpointsQuery.error,
    previewQuery.error,
    approveMutation.error,
    deployMutation.error,
    integrationsQuery.error,
    jobsQuery.error,
    zendeskContextQuery.error,
    zendeskValidationError,
    attachmentExtractMutation.error,
  ]
    .filter(Boolean)
    .map((err) => parseFailureDetail(err));

  const previewData = previewQuery.data;
  const activeOperationMode = previewData?.planning_summary?.operation_mode || operationMode || "create";
  const deploymentExistingMode = activeOperationMode === "update"
    ? "overwrite_existing"
    : onExistingMode;
  const generatedData = jobQuery.data
    ? {
        ...(generateMutation.data || {}),
        ...jobQuery.data,
        planning_summary: previewData?.planning_summary || {},
        preview_url: batchId ? `/api/import-assistant/preview/${batchId}` : null,
      }
    : generateMutation.data;
  const effectiveGenerateMetadata = jobQuery.data?.metadata || generatedData?.metadata || {};
  const historyItems = jobsQuery.data?.jobs || [];
  const contextCatalog = zendeskContextQuery.data?.catalogs || null;
  const resolvedUpdateTarget = operationMode === "update" && updateTarget && contextCatalog
    ? Object.values(contextCatalog)
      .flatMap((entries) => entries || [])
      .find((entry) => (
        entry.object_type === updateTarget.object_type
        && String(entry.id) === String(updateTarget.id)
      )) || null
    : updateTarget;

  const handleOperationModeChange = (nextMode) => {
    if (nextMode === operationMode) return;
    setOperationMode(nextMode);
    setUpdateTarget(null);
    setSelectedContext({});
    setFocusObjectTypes([]);
    setExternalPrompt("");
    setDecisions({});
    setChatSessionId((value) => value + 1);
    generateMutation.reset();
    resetFlow();
    if (nextMode === "update") {
      setDependencyMode("force_existing_only");
      setExistingItemBehavior("relate_or_update");
      setOnExistingMode("overwrite_existing");
      zendeskContextQuery.refetch();
    } else {
      setDependencyMode("match_existing_or_create_new");
      setExistingItemBehavior("create_new");
      setOnExistingMode("create_new");
    }
  };

  const handleUpdateTargetChange = (target) => {
    setUpdateTarget(target);
    if (!target) {
      setSelectedContext({});
      setFocusObjectTypes([]);
      return;
    }
    const key = `${target.object_type}:${target.id}`;
    setSelectedContext({ [key]: target });
    const focus = UPDATE_FOCUS_BY_OBJECT_TYPE[target.object_type];
    setFocusObjectTypes(focus ? [focus] : []);
  };

  const promptLocked = Boolean(
    !zendeskValidated
    || !operationMode
    || (operationMode === "update" && (
      zendeskContextQuery.isFetching
      || !zendeskContextQuery.data?.sync_id
      || !resolvedUpdateTarget
    ))
  );
  const promptLockReason = !zendeskValidated
    ? "Validate Zendesk credentials first in Integration Status."
    : !operationMode
      ? "Choose Create new or Update existing first."
      : operationMode === "update" && zendeskContextQuery.isFetching
        ? "Wait for the Zendesk configuration snapshot to finish synchronizing."
        : operationMode === "update" && !resolvedUpdateTarget
          ? "Choose the exact existing Zendesk object to update."
          : "";
  const selectedExistingItems = selectedRelatedObjects;
  const selectedExistingItemKeySet = new Set(
    selectedExistingItems.map((item) => `${item.object_type}:${item.id}`)
  );
  const allowedCatalogKeys = new Set(
    focusObjectTypes.flatMap((focus) => FOCUS_TO_CATALOG_KEYS[focus] || [])
  );
  const existingItemOptions = [];
  if (contextCatalog && focusObjectTypes.length) {
    for (const [catalogKey, entries] of Object.entries(contextCatalog)) {
      if (!allowedCatalogKeys.has(catalogKey)) continue;
      (entries || []).forEach((entry) => {
        const key = `${entry.object_type}:${entry.id}`;
        existingItemOptions.push({
          key,
          label: `${sectionLabel(catalogKey)}: ${entry.name}`,
          catalogKey,
          entry,
        });
      });
    }
    existingItemOptions.sort((a, b) => a.label.localeCompare(b.label));
  }

  const articleFocusSelected = focusObjectTypes.includes("articles");
  const helpCentersLoaded = (contextCatalog?.help_centers || []).length > 0;
  const articleHelpCenterHint = articleFocusSelected && !helpCentersLoaded
    ? "No Help Center was confirmed for this brand. Content can still be generated, but deployment will require brand verification and may require manual Guide enablement."
    : "";
  const normalizedHistorySearch = historySearch.trim().toLowerCase();
  const filteredHistory = historyItems.filter((item) => {
    if (!normalizedHistorySearch) return true;
    return (
      String(item.batch_id || "").toLowerCase().includes(normalizedHistorySearch)
      || String(item.prompt_preview || "").toLowerCase().includes(normalizedHistorySearch)
      || String(item.status || "").toLowerCase().includes(normalizedHistorySearch)
    );
  });

  const validateZendeskSession = (credentials) => {
    setZendeskCredentials(credentials);
    mutateZendeskValidation(credentials);
  };

  const signOutZendeskSession = () => {
    clearZendeskSessionCredentials();
    setZendeskValidated(false);
    setZendeskCredentials({ subdomain: "", email: "", api_token: "" });
    setDecisions({});
    setActivityLogs([]);
    setTimeline([]);
    setInferenceAssumptionMessages([]);
    setLastFailureDetail(null);
    setChatHistory([]);
    setHistorySearch("");
    setSelectedContext({});
    setOperationMode(null);
    setUpdateTarget(null);
    setDependencyMode("match_existing_or_create_new");
    setOnExistingMode("create_new");
    setExistingItemBehavior("relate_or_update");
    setFocusObjectTypes([]);
    setAttachments([]);
    setHelpCenterUrl("");
    setHelpCenterArticleMode("draft");
    setHelpCenterReadiness(null);
    setShowAdvancedCatalog(false);
    setActiveRunStartedAtMs(null);
    setChatSessionId((value) => value + 1);
    lastContextSyncRef.current = "";
    lastContextErrorRef.current = "";
    generateMutation.reset();
    resetFlow();
    resetZendeskValidation();
  };

  const startNewChat = () => {
    const previousBatchId = batchId;
    setDecisions({});
    setInferenceAssumptionMessages([]);
    setLastFailureDetail(null);
    setActivityLogs([]);
    setTimeline([]);
    setHistorySearch("");
    setChatHistory([]);
    setOperationMode(null);
    setUpdateTarget(null);
    setSelectedContext({});
    setDependencyMode("match_existing_or_create_new");
    setExistingItemBehavior("relate_or_update");
    setOnExistingMode("create_new");
    setFocusObjectTypes([]);
    setAttachments([]);
    setHelpCenterUrl("");
    setHelpCenterArticleMode("draft");
    setHelpCenterReadiness(null);
    setShowAdvancedCatalog(false);
    setShowProcessingDetails(false);
    setShowExistingContext(false);
    setActiveRunStartedAtMs(null);
    setChatSessionId((value) => value + 1);
    generateMutation.reset();
    resetFlow();
    if (previousBatchId) {
      queryClient.removeQueries({ queryKey: ["job", previousBatchId] });
      queryClient.removeQueries({ queryKey: ["preview", previousBatchId] });
      queryClient.removeQueries({ queryKey: ["checkpoints", previousBatchId] });
    }
    appendActivity("info", "Started a new chat. Zendesk context remains loaded for this session.");
  };

  const selectHistoryBatch = (selectedBatchId) => {
    setBatchId(selectedBatchId);
    setHelpCenterReadiness(null);
    setHelpCenterArticleMode("draft");
    appendActivity("info", `Loaded batch ${selectedBatchId} from history.`);
    queryClient.invalidateQueries({ queryKey: ["job", selectedBatchId] });
    queryClient.invalidateQueries({ queryKey: ["preview", selectedBatchId] });
  };

  const scrollToSection = (selector) => {
    window.setTimeout(() => {
      document
        .querySelector(selector)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  };

  const handleSidebarNavigation = (label) => {
    if (label === "New Chat") {
      startNewChat();
      return;
    }

    if (label === "Dashboard") {
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }

    if (label === "All Chats") {
      setShowExistingContext(true);
      scrollToSection("#recent-requests");
      return;
    }

    if (label === "Templates") {
      scrollToSection("#zendesk-templates");
      return;
    }

    if (label === "Knowledge Base") {
      setFocusObjectTypes(["articles"]);
      setExternalPrompt("Create a help center article");
      scrollToSection("#knowledge-base");
      return;
    }

    if (label === "Integrations") {
      setShowExistingContext(true);
      scrollToSection("#integration-diagnostics");
      return;
    }

    if (label === "Settings") {
      scrollToSection("#settings");
    }
  };

  const openContextSelection = () => {
    setShowExistingContext(true);
    setShowAdvancedCatalog(true);
    scrollToSection("#context-selection");
  };

  const toggleContextSelection = (entry) => {
    const key = `${entry.object_type}:${entry.id}`;
    setSelectedContext((prev) => {
      if (prev[key]) {
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: entry };
    });
  };

  const toggleFocusObjectType = (key) => {
    setFocusObjectTypes((prev) => {
      const next = new Set(prev || []);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return Array.from(next);
    });
  };

  const addExistingContextSelection = (selectionKey) => {
    if (!selectionKey) return;
    const option = existingItemOptions.find((item) => item.key === selectionKey);
    if (!option?.entry) return;
    setSelectedContext((prev) => ({ ...prev, [selectionKey]: option.entry }));
  };

  const removeExistingContextSelection = (selectionKey) => {
    if (!selectionKey) return;
    setSelectedContext((prev) => {
      if (!prev[selectionKey]) return prev;
      const next = { ...prev };
      delete next[selectionKey];
      return next;
    });
  };

  const deployToZendesk = async () => {
    const performDeploy = () => {
      deployMutation.mutate({
        batch_id: batchId,
        subdomain: zendeskCredentials.subdomain,
        email: zendeskCredentials.email,
        api_token: zendeskCredentials.api_token,
        dry_run: false,
        on_existing: deploymentExistingMode,
        deployment_scope: "support",
      });
    };

    if (!batchId || !zendeskCredentials?.subdomain || !zendeskCredentials?.email || !zendeskCredentials?.api_token) {
      appendActivity("error", "Cannot deploy: missing validated Zendesk session credentials.");
      return;
    }

    try {
      const localDecisions = buildDecisionPayload();
      if (localDecisions.length > 0) {
        appendActivity("info", "Auto-saving local approval changes before deploy.");
        await approveMutation.mutateAsync({
          batch_id: batchId,
          approved_by: "local-user",
          records: localDecisions,
        });
      }

      const hasApprovedRecords = previewRecords.some((row) => {
        const local = decisions[row.record_id];
        const effectiveDecision = local || row.import_decision;
        return effectiveDecision === "approved" && row.deployable;
      });

      if (!hasApprovedRecords) {
        appendActivity(
          "error",
          "No deployable approved records found. Approve at least one record, save, then deploy."
        );
        return;
      }

      performDeploy();
    } catch (error) {
      appendActivity(
        "error",
        parseFailureDetail(error) || "Could not save approval decisions before deploy."
      );
    }
  };

  const approveAndDeploy = async () => {
    if (!batchId) {
      appendActivity("error", "No batch selected.");
      return;
    }
    if (!zendeskCredentials?.subdomain || !zendeskCredentials?.email || !zendeskCredentials?.api_token) {
      appendActivity("error", "Cannot deploy: missing validated Zendesk session credentials.");
      return;
    }

    const reviewPayload = buildReviewDecisionPayload();
    if (!reviewPayload.length) {
      appendActivity("error", "No review records available to approve.");
      return;
    }

    try {
      appendActivity("info", "Saving approval and starting deployment...");
      await approveMutation.mutateAsync({
        batch_id: batchId,
        approved_by: "local-user",
        records: reviewPayload,
      });
      setDecisions((prev) => {
        const next = { ...prev };
        reviewPayload.forEach((item) => {
          next[item.record_id] = item.import_decision;
        });
        return next;
      });
      const hasApprovedRecords = reviewPayload.some((item) => item.import_decision === "approved");
      if (!hasApprovedRecords) {
        appendActivity("info", "Decisions saved. Nothing to deploy because no rows are approved.");
        appendTimeline("assistant", "Decisions saved. No approved rows selected, so deployment was skipped.");
        return;
      }
      deployMutation.mutate({
        batch_id: batchId,
        subdomain: zendeskCredentials.subdomain,
        email: zendeskCredentials.email,
        api_token: zendeskCredentials.api_token,
        dry_run: false,
        on_existing: deploymentExistingMode,
        deployment_scope: "support",
      });
    } catch (error) {
      appendActivity(
        "error",
        parseFailureDetail(error) || "Could not complete approve-and-deploy action."
      );
    }
  };

  const updateHelpCenterUrl = (value) => {
    setHelpCenterUrl(value);
    setHelpCenterReadiness(null);
  };

  const verifyHelpCenter = () => {
    if (!zendeskValidated) {
      appendActivity("error", "Validate the Zendesk session before checking Help Center readiness.");
      return;
    }
    const targetUrl = helpCenterUrl.trim()
      || `https://${zendeskCredentials.subdomain}.zendesk.com/hc/en-us`;
    if (!helpCenterUrl.trim()) {
      setHelpCenterUrl(targetUrl);
    }
    helpCenterReadinessMutation.mutate({
      ...zendeskCredentials,
      help_center_url: targetUrl,
      brand_id: helpCenterReadiness?.brand?.id || null,
    });
  };

  const deployHelpCenter = () => {
    if (!batchId || !helpCenterReadiness?.ready) {
      appendActivity("error", "Verify a ready Help Center before deploying knowledge content.");
      return;
    }
    deployMutation.mutate({
      batch_id: batchId,
      subdomain: zendeskCredentials.subdomain,
      email: zendeskCredentials.email,
      api_token: zendeskCredentials.api_token,
      dry_run: false,
      on_existing: activeOperationMode === "update"
        ? "overwrite_existing"
        : onExistingMode === "create_new" ? "skip_existing" : onExistingMode,
      deployment_scope: "help_center",
      help_center_url: helpCenterReadiness.help_center_url || helpCenterUrl,
      brand_id: helpCenterReadiness.brand?.id || null,
      locale: helpCenterReadiness.locale || "en-us",
      article_mode: helpCenterArticleMode,
      confirm_help_center_deploy: true,
      confirm_article_publish: helpCenterArticleMode === "publish",
    });
  };

  const isWorking =
    generateMutation.isPending
    || approveMutation.isPending
    || deployMutation.isPending
    || helpCenterReadinessMutation.isPending
    || hasActiveBatchRun
    || attachmentExtractMutation.isPending;

  const currentPhaseLabel = (() => {
    if (attachmentExtractMutation.isPending) return "Extracting attachment context...";
    if (helpCenterReadinessMutation.isPending) return "Verifying Help Center readiness...";
    if (deployMutation.isPending) return "Deploying approved records to Zendesk...";
    if (approveMutation.isPending) return "Writing approval decisions to Apps Script...";
    if (generateMutation.isPending) return "Generating and staging records...";
    const status = currentStatus || "";
    const phaseMap = {
      received: "Preparing the run...",
      request_validated: "Validating request...",
      planning: "Planning configuration...",
      planned: "Plan ready.",
      business_blueprinting: "Building department coverage blueprint...",
      backlog_building: "Ordering dependency-safe work...",
      wave_execution: "Running department generation waves...",
      clarification_required: "Waiting for clarification before generation...",
      schemas_selected: "Selecting schemas...",
      generating: "Generating records...",
      supervisor_review: "Reviewing quality and applying safe patches...",
      generated: "Generation complete.",
      staging: "Writing to Apps Script/Sheets...",
      staged: "Rows staged.",
      validating: "Running validation...",
      validated_passed: "Validation passed.",
      validated_warning: "Validation complete with warnings.",
      validated_failed: "Validation failed for some records.",
      preview_ready: "Preview ready for your decisions.",
      deploying: "Deploying to Zendesk...",
      deployed: "Deployment completed.",
      deployed_partial: "Deployment completed with partial failures.",
      deploy_failed: "Deployment failed.",
      approved: "Approval decisions saved.",
      partially_approved: "Partial approval saved.",
    };
    return phaseMap[status] || "Idle";
  })();

  const attachmentSummaryText = useMemo(
    () => attachments.map((item) => `${item.filename} (${item.char_count})`).join(", "),
    [attachments]
  );

  const processingLines = useMemo(() => {
    const runtimeMetadata = jobQuery.data?.metadata || generatedData?.metadata || {};
    const orchestration = runtimeMetadata?.orchestration || {};
    const supervisor = runtimeMetadata?.supervisor || {};
    const waveRows = Array.isArray(orchestration?.waves) ? orchestration.waves : [];
    const history = [...(jobQuery.data?.status_history || [])];
    const lines = history
      .filter((item) => item?.status && item?.message)
      .slice(-10)
      .reverse()
      .map((item) => ({
        at: item.at || new Date().toISOString(),
        stage: item.status,
        text: humanizeStatusStep(item.status, item.message),
        source: item.source || "pipeline",
        provider: item.provider || null,
        model: item.model || null,
        wave: item.wave || null,
        department: item.department || null,
        objectType: item.object_type || null,
      }));

    if (waveRows.length > 0) {
      const waveProgress = waveRows
        .map((wave) => {
          const waveId = Number(wave?.wave || 0);
          const status = String(wave?.status || "pending");
          const created = Number(wave?.created || 0);
          const reused = Number(wave?.reused || 0);
          const updated = Number(wave?.updated || 0);
          const blocked = Number(wave?.blocked || 0);
          const chunks = Number(wave?.chunks || 0);
          return `Wave ${waveId}: ${status} (chunks=${chunks}, create=${created}, reuse=${reused}, update=${updated}, blocked=${blocked})`;
        })
        .filter(Boolean);
      if (waveProgress.length > 0) {
        lines.push({
          at: new Date().toISOString(),
          stage: "wave_execution",
          text: waveProgress.slice(0, 2).join(" | "),
          source: "orchestration_summary",
        });
      }
    }

    if (supervisor?.enabled) {
      const reviews = Array.isArray(supervisor?.reviews) ? supervisor.reviews : [];
      const patchCounts = supervisor?.patch_counts || {};
      const latestReview = reviews.length > 0 ? reviews[reviews.length - 1] : null;
      let supervisorText;
      if (supervisor?.availability_reason === "missing_api_key") {
        supervisorText = "Gemini supervisor is enabled and waiting for GEMINI_API_KEY.";
      } else if (latestReview) {
        const summary = String(
          latestReview.public_reasoning_summary
          || latestReview.reason
          || latestReview.status
          || "review completed"
        ).trim();
        const effectiveApproved = latestReview.effective_approved;
        const rawScore = Number(latestReview.raw_quality_score ?? latestReview.quality_score ?? 0).toFixed(2);
        const effectiveScore = Number(latestReview.effective_quality_score ?? latestReview.quality_score ?? 0).toFixed(2);
        const regen = latestReview.requires_regeneration || effectiveApproved === false
          ? " Targeted recovery is required for the affected chunk."
          : "";
        supervisorText = `Gemini supervisor: ${summary} (raw=${rawScore}, effective=${effectiveScore}, gate=${effectiveApproved === false ? "rejected" : "approved"}, patches applied=${Number(patchCounts.applied || 0)}, rejected=${Number(patchCounts.rejected || 0)}).${regen}`;
      } else if (supervisor?.available) {
        supervisorText = "Gemini supervisor lane is ready for review and safe patching.";
      } else {
        supervisorText = `Gemini supervisor unavailable: ${supervisor?.availability_reason || "not configured"}.`;
      }
      lines.push({
        at: new Date().toISOString(),
        stage: "supervisor_review",
        text: supervisorText,
        source: "supervisor_summary",
      });
    }

    if (attachmentExtractMutation.isPending) {
      lines.unshift({
        at: new Date().toISOString(),
        stage: "attachments",
        text: "Extracting attachment text for prompt context.",
      });
    }

    if (generateMutation.isPending && !lines.some((item) => item.stage === "generating")) {
      lines.unshift({
        at: new Date().toISOString(),
        stage: "generating",
        text: "Generating records and synchronizing blueprint waves.",
      });
    }

    if (!lines.length && activityLogs.length > 0) {
      for (const item of activityLogs.slice(0, 3)) {
        lines.push({
          at: item.at,
          stage: item.level,
          text: item.message,
        });
      }
    }

    return lines.slice(0, 8);
  }, [
    activityLogs,
    attachmentExtractMutation.isPending,
    generateMutation.isPending,
    generatedData?.metadata,
    jobQuery.data?.metadata,
    jobQuery.data?.status_history,
  ]);

  const processingSnapshot = useMemo(() => {
    const runtimeMetadata = jobQuery.data?.metadata || generatedData?.metadata || {};
    const progress = extractWaveChunkProgress(jobQuery.data?.status_history || [], runtimeMetadata);
    const fallbackStart = jobQuery.data?.created_at ? Date.parse(jobQuery.data.created_at) : null;
    const startAtMs = activeRunStartedAtMs || fallbackStart || null;
    const elapsedMs = startAtMs ? Math.max(0, processingClockMs - startAtMs) : 0;
    let etaMs = null;
    if (progress.progressFraction > 0.05 && progress.progressFraction < 1) {
      etaMs = Math.max(
        0,
        Math.round((elapsedMs / progress.progressFraction) * (1 - progress.progressFraction))
      );
    }
    const currentLine = processingLines[0];
    const beat = Math.floor(elapsedMs / 7_000);
    const activityStage = String(currentLine?.stage || currentStatus || "received");
    const narrative = buildOperationalNarrative(activityStage, beat);
    const lastUpdateAt = currentLine?.at ? Date.parse(currentLine.at) : null;
    const lastUpdateSeconds = lastUpdateAt
      ? Math.max(0, Math.floor((processingClockMs - lastUpdateAt) / 1000))
      : null;
    const completedRun = [
      "preview_ready",
      "approved",
      "partially_approved",
      "deployed",
      "deployed_partial",
    ].includes(String(currentStatus || ""));
    const stoppedRun = ["failed", "clarification_required", "deploy_failed"].includes(
      String(currentStatus || "")
    );
    const generationProgress = Math.min(Math.max(progress.progressPercent || 0, 0), 100);
    const milestoneProgressByStage = {
      received: 2,
      request_validated: 5,
      planning: 10,
      planned: 15,
      business_blueprinting: 18,
      backlog_building: 22,
      schemas_selected: 25,
      generated: 76,
      staging: 82,
      staged: 89,
      validating: 94,
      validated_passed: 98,
      validated_warning: 98,
      validated_failed: 98,
    };
    let milestoneProgress = milestoneProgressByStage[activityStage];
    if (["generating", "wave_execution"].includes(activityStage)) {
      milestoneProgress = 25 + (generationProgress * 0.45);
    } else if (activityStage === "supervisor_review") {
      milestoneProgress = 30 + (generationProgress * 0.45);
    }
    if (!Number.isFinite(milestoneProgress)) {
      milestoneProgress = generationProgress;
    }
    return {
      badge: progress.badge || "Preparing run",
      elapsedLabel: formatDuration(elapsedMs),
      etaLabel: completedRun ? "complete" : (stoppedRun ? "stopped" : (etaMs === null ? "estimating" : formatDuration(etaMs))),
      currentDoing: currentLine?.text || currentPhaseLabel,
      why: narrative.why,
      next: narrative.next,
      progressPercent: completedRun ? 100 : Math.min(Math.round(milestoneProgress), 98),
      lastUpdateLabel: lastUpdateSeconds === null
        ? "connecting"
        : (lastUpdateSeconds < 2 ? "just now" : `${lastUpdateSeconds}s ago`),
      lines: processingLines.slice(0, 8),
    };
  }, [
    activeRunStartedAtMs,
    currentStatus,
    currentPhaseLabel,
    generatedData?.metadata,
    jobQuery.data,
    processingClockMs,
    processingLines,
  ]);

  if (!zendeskValidated) {
    return (
      <ZendeskSessionGate
        credentials={zendeskCredentials}
        onCredentialsChange={setZendeskCredentials}
        onValidate={validateZendeskSession}
        isValidating={zendeskValidationPending}
        validationResult={zendeskValidationResult}
        validationError={zendeskValidationError}
        integrationsStatus={integrationsQuery.data}
        integrationsLoading={integrationsQuery.isLoading}
        isRestoringSession={Boolean(bootZendeskSession) && zendeskValidationPending && !zendeskValidated}
      />
    );
  }

  const processingPanelBg = darkMode
    ? "min-w-0 overflow-hidden rounded-lg border border-[#7B1FFF]/24 bg-[#120522]/60 p-3"
    : "min-w-0 overflow-hidden rounded-lg border border-slate-200 bg-slate-100 p-3";

  const processingHeading = darkMode ? "text-slate-200" : "text-slate-800";
  const processingSubtext = darkMode ? "text-slate-400" : "text-slate-500";
  const processingBody = darkMode ? "text-[#B9A7D9]" : "text-slate-600";

  const badgePill = darkMode
    ? "rounded-full border border-[#7B1FFF]/40 bg-[#07030F]/70 px-2 py-0.5"
    : "rounded-full border border-slate-300 bg-white px-2 py-0.5 text-slate-600";

  const processingDetailLines = darkMode
    ? "mt-3 space-y-1 border-t border-[#7B1FFF]/20 pt-2 text-xs text-slate-300"
    : "mt-3 space-y-1 border-t border-slate-200 pt-2 text-xs text-slate-600";

  const appSurface = darkMode
    ? "bg-[#07030F] text-slate-100"
    : "bg-white text-slate-950";
  const railSurface = darkMode
    ? "border-[#7B1FFF]/20 bg-[#07030F]/86"
    : "border-slate-200 bg-white";
  const workspaceCard = darkMode
    ? "border-[#7B1FFF]/24 bg-[#120522]/70 shadow-[0_18px_60px_rgba(0,0,0,0.32)]"
    : "border-slate-200 bg-white shadow-[0_16px_40px_rgba(15,23,42,0.06)]";
  const hoverCard = darkMode
    ? "border-[#7B1FFF]/22 bg-[#151027]/78 hover:border-[#9B35FF]/55 hover:bg-[#1B1230]/88"
    : "border-slate-200 bg-white hover:border-violet-300 hover:bg-violet-50/30";
  const textSoft = darkMode ? "text-[#B9A7D9]" : "text-slate-500";
  const sectionTitle = darkMode ? "text-slate-100" : "text-slate-950";
  const activeFocusSet = new Set(focusObjectTypes || []);

  return (
    <div className={`relative min-h-screen overflow-hidden ${appSurface}`}>
      {darkMode ? (
        <div className="cx-hero-watermark" aria-hidden="true">
          <img src={cxHeroBanner} alt="" />
        </div>
      ) : null}

      <aside className={`fixed inset-y-0 left-0 z-20 hidden w-[260px] border-r px-5 py-7 xl:block ${railSurface}`}>
        <div className="flex items-center justify-center py-4">
  <img
    src={cxLogo}
    alt="CX Experts"
    className="max-h-24 w-auto object-contain"
  />
</div>
        <nav className="mt-9 space-y-2">
          {NAV_ITEMS.map((item) => {
  const Icon = item.icon;
  return (
    <button
      key={item.label}
      type="button"
      onClick={() => handleSidebarNavigation(item.label)}
      className={`flex h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm transition ${
        item.active
          ? darkMode
            ? "bg-[#7B1FFF]/24 text-white"
            : "bg-violet-50 text-violet-700"
          : darkMode
            ? "text-slate-300 hover:bg-[#7B1FFF]/12"
            : "text-slate-700 hover:bg-slate-50"
      }`}
    >
      <Icon size={18} />
      {item.label}
    </button>
  );
})}
        </nav>
        <div className={`mt-9 border-t pt-6 ${darkMode ? "border-[#7B1FFF]/18" : "border-slate-200"}`}>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#6D28D9] text-sm font-semibold text-white">
              A
            </div>
            <div>
              <p className="text-sm font-semibold">Admin User</p>
              <p className={`text-xs ${textSoft}`}>admin@cxexperts.com</p>
            </div>
          </div>
          <button
            type="button"
            onClick={signOutZendeskSession}
            className={`mt-6 flex h-10 w-full items-center gap-3 rounded-lg px-3 text-sm ${darkMode ? "text-slate-300 hover:bg-[#7B1FFF]/12" : "text-slate-700 hover:bg-slate-50"}`}
          >
            <LogOut size={17} />
            Sign Out
          </button>
        </div>
      
      </aside>

      <div className="relative z-10 xl:pl-[260px]">
        <header className="mx-auto flex max-w-[1480px] items-center justify-between gap-3 px-4 py-5 sm:px-6 lg:px-8">
          <div className="flex shrink-0 items-center gap-3 xl:hidden">
            <img src={cxIcon} alt="CX" className="h-10 w-10 rounded-lg" />
            <div className="hidden sm:block">
              <p className="font-semibold">CX Experts</p>
              <p className={`text-xs ${textSoft}`}>AI Assistant</p>
            </div>
          </div>
          <div className="hidden xl:block" />
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              className={`${darkMode ? "" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"} h-10 w-10 gap-2 p-0 sm:w-auto sm:px-4`}
              onClick={() => setShowExistingContext((prev) => !prev)}
              aria-label="Context"
            >
              <PanelRightOpen size={16} />
              <span className="hidden sm:inline">Context</span>
            </Button>
            <Button
              variant="outline"
              className={`${darkMode ? "" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"} h-10 w-10 p-0`}
              onClick={() => setDarkMode(!darkMode)}
              aria-label={darkMode ? "Switch to light theme" : "Switch to purple theme"}
            >
              {darkMode ? <Sun size={17} /> : <Moon size={17} />}
            </Button>
            <Button
              variant="primary"
              className="h-10 w-10 gap-2 bg-[#7B1FFF] p-0 text-white hover:bg-[#9B35FF] sm:w-auto sm:px-4"
              onClick={startNewChat}
              aria-label="New Chat"
            >
              <CirclePlus size={16} />
              <span className="hidden sm:inline">New Chat</span>
            </Button>
          </div>
        </header>

        <main className="mx-auto grid max-w-[1480px] gap-6 px-4 pb-10 sm:px-6 lg:grid-cols-[minmax(0,1fr)_330px] lg:px-8">
          <section className="min-w-0">
            <div id="dashboard" className={`relative overflow-hidden rounded-2xl border p-5 sm:p-7 ${workspaceCard}`}>
              <div className="relative z-10 max-w-xl pr-0 sm:pr-8">
                <h1 className="text-2xl font-bold tracking-normal text-violet-600 sm:text-3xl">Zendesk AI Import</h1>
                <p className={`mt-3 text-xl font-semibold ${sectionTitle}`}>What would you like to build today?</p>
                <p className={`mt-3 max-w-xl text-sm leading-6 ${textSoft}`}>
                  Generate Zendesk configurations, automate workflows, and create help center content with AI.
                </p>
              </div>
              <div className="pointer-events-none absolute inset-y-0 right-0 w-full opacity-20 sm:w-2/3 sm:opacity-70">
                <img src={darkMode ? cxHeroBanner : cxHeroLight} alt="" className={`h-full w-full ${
      darkMode
        ? "object-cover object-right opacity-40 sm:opacity-80"
        : "object-cover object-center opacity-30 sm:opacity-100"
    }`} />
              </div>
            </div>

            <div className="mt-6 overflow-hidden rounded-lg">
              <OperationModeSelector
                darkMode={darkMode}
                operationMode={operationMode}
                onOperationModeChange={handleOperationModeChange}
                contextStatus={zendeskContextQuery.data}
                contextLoading={zendeskContextQuery.isFetching}
                onRefreshContext={() => zendeskContextQuery.refetch()}
                updateTarget={resolvedUpdateTarget}
                onUpdateTargetChange={handleUpdateTargetChange}
              />
            </div>

            <div id="zendesk-templates" className="mt-6">
              <h2 className={`mb-3 text-sm font-semibold ${sectionTitle}`}>Quick Actions</h2>
              <div className="grid grid-cols-2 gap-3 xl:grid-cols-5">
                {QUICK_ACTIONS.map((action) => {
  const Icon = action.icon;
  return (
    <button
      key={action.label}
      type="button"
      onClick={() => setExternalPrompt(action.prompt)}
      disabled={operationMode !== "create"}
      className={`min-h-[142px] rounded-lg border p-4 text-left transition disabled:cursor-not-allowed disabled:opacity-45 ${hoverCard}`}
    >
      <Icon className={`h-8 w-8 ${action.color}`} />
      <p className={`mt-3 text-sm font-semibold ${sectionTitle}`}>{action.label}</p>
      <p className={`mt-1 text-xs leading-5 ${textSoft}`}>{action.detail}</p>
    </button>
  );
})}
              </div>
            </div>

            <div id="knowledge-base" className="mt-4">
              <PromptComposer
                key={chatSessionId}
                embedded
                compact
                darkMode={darkMode}
                onSubmitPrompt={submitPrompt}
                onExtractAttachment={handleExtractAttachment}
                onRemoveAttachment={removeAttachment}
                onAddExistingContext={addExistingContextSelection}
                onRemoveExistingContext={removeExistingContextSelection}
                attachments={attachments}
                isLoading={generateMutation.isPending || attachmentExtractMutation.isPending}
                isLocked={promptLocked}
                lockReason={promptLockReason}
                operationMode={operationMode}
                dependencyMode={dependencyMode}
                onDependencyModeChange={setDependencyMode}
                onExistingMode={onExistingMode}
                onOnExistingModeChange={setOnExistingMode}
                existingItemBehavior={existingItemBehavior}
                onExistingItemBehaviorChange={setExistingItemBehavior}
                selectedContextCount={selectedRelatedObjects.length}
                focusObjectTypes={focusObjectTypes}
                onFocusObjectTypesChange={setFocusObjectTypes}
                existingItemOptions={existingItemOptions}
                selectedExistingItems={selectedExistingItems}
                selectedExistingItemKeySet={selectedExistingItemKeySet}
                articleHelpCenterHint={articleHelpCenterHint}
                externalPrompt={externalPrompt}
                onExternalPromptConsumed={() => setExternalPrompt("")}
              />
            </div>

            <div className="mt-5">
              <div className="mb-3 flex items-center justify-between">
                <h2 className={`text-sm font-semibold ${sectionTitle}`}>Try these examples</h2>
                <button type="button" className="text-xs font-medium text-violet-600">View all</button>
              </div>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {EXAMPLE_PROMPTS.map((item) => {
  const Icon = item.icon;
  return (
    <button
      key={item.label}
      type="button"
      onClick={() => setExternalPrompt(item.prompt)}
      disabled={operationMode !== "create"}
      className={`flex items-center gap-3 rounded-lg border p-3 text-left text-sm transition disabled:cursor-not-allowed disabled:opacity-45 ${hoverCard}`}
    >
      <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${darkMode ? "bg-[#7B1FFF]/18" : "bg-violet-50"}`}>
        <Icon className={`h-5 w-5 ${item.color}`} />
      </span>
      <span className={sectionTitle}>{item.label}</span>
    </button>
  );
})}
              </div>
            </div>

            <div className={`mt-5 ${processingPanelBg}`}>
              <button
                type="button"
                onClick={() => setShowProcessingDetails((prev) => !prev)}
                className="flex w-full flex-col items-start justify-between gap-3 text-left sm:flex-row sm:gap-4"
                aria-expanded={showProcessingDetails}
                aria-label={showProcessingDetails ? "Hide activity history" : "Show activity history"}
              >
                <div className="flex min-w-0 items-start gap-3">
                  <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${darkMode ? "bg-[#7B1FFF]/18 text-[#B994FF]" : "bg-violet-100 text-violet-700"}`}>
                    {isWorking ? (
                      <LoaderCircle className="animate-spin" size={17} aria-hidden="true" />
                    ) : (
                      <Activity size={17} aria-hidden="true" />
                    )}
                  </span>
                  <div className="min-w-0">
                    <p className={`text-sm font-semibold ${processingHeading}`}>
                      {isWorking ? "Live build activity" : "Run activity"}
                    </p>
                    <p className={`mt-0.5 text-xs ${processingSubtext}`}>{currentPhaseLabel}</p>
                  </div>
                </div>
                <div className="flex w-full min-w-0 items-center justify-between gap-2 sm:w-auto sm:shrink-0 sm:justify-end">
                  <div className={`mt-2 flex flex-wrap items-center gap-2 text-[11px] ${processingBody}`}>
                    <span className={`${badgePill} max-w-full break-words`}>{processingSnapshot.badge}</span>
                  </div>
                  <ChevronRight
                    className={`mt-2 transition-transform ${showProcessingDetails ? "rotate-90" : ""} ${textSoft}`}
                    size={17}
                    aria-hidden="true"
                  />
                </div>
              </button>

              {(isWorking || processingSnapshot.progressPercent > 0) ? (
                <div className="mt-3">
                  <div className={`flex items-center justify-between text-[11px] ${processingBody}`}>
                    <span>Elapsed {processingSnapshot.elapsedLabel}</span>
                    <span>{processingSnapshot.progressPercent}% | ETA {processingSnapshot.etaLabel}</span>
                  </div>
                  <div className={`mt-1.5 h-1.5 w-full overflow-hidden rounded-full ${darkMode ? "bg-white/10" : "bg-slate-200"}`}>
                    <div
                      className="h-full rounded-full bg-emerald-500 transition-[width] duration-700 ease-out"
                      style={{ width: `${Math.max(processingSnapshot.progressPercent, isWorking ? 2 : 0)}%` }}
                    />
                  </div>
                </div>
              ) : null}

              {(batchId || isWorking) ? (
                <div
                  className={`mt-3 border-t pt-3 ${darkMode ? "border-[#7B1FFF]/20" : "border-slate-200"}`}
                  aria-live="polite"
                >
                <div className="flex flex-col items-start justify-between gap-2 sm:flex-row sm:gap-3">
                  <div className="min-w-0">
                    <p className={`text-[11px] font-semibold uppercase tracking-normal ${processingSubtext}`}>
                      Working on now
                    </p>
                    <p className={`mt-1 break-words text-sm leading-5 ${processingHeading}`}>
                      {processingSnapshot.currentDoing}
                    </p>
                  </div>
                  <span className={`shrink-0 text-[11px] ${processingSubtext}`}>
                    Updated {processingSnapshot.lastUpdateLabel}
                  </span>
                </div>

                <div className={`mt-3 grid gap-3 border-t pt-3 sm:grid-cols-2 ${darkMode ? "border-white/8" : "border-slate-200"}`}>
                  <div className="flex min-w-0 items-start gap-2">
                    <ShieldCheck className="mt-0.5 shrink-0 text-emerald-500" size={15} aria-hidden="true" />
                    <div className="min-w-0">
                      <p className={`text-[11px] font-semibold ${processingHeading}`}>Why this matters</p>
                      <p className={`mt-1 break-words text-xs leading-5 ${processingBody}`}>{processingSnapshot.why}</p>
                    </div>
                  </div>
                  <div className="flex min-w-0 items-start gap-2">
                    <ArrowRight className="mt-0.5 shrink-0 text-blue-500" size={15} aria-hidden="true" />
                    <div className="min-w-0">
                      <p className={`text-[11px] font-semibold ${processingHeading}`}>Next checkpoint</p>
                      <p className={`mt-1 break-words text-xs leading-5 ${processingBody}`}>{processingSnapshot.next}</p>
                    </div>
                  </div>
                </div>
                </div>
              ) : null}

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => sendRunControl("pause")}
                  disabled={!canControlRun || runControlMutation.isPending || runControlState.pause_requested}
                  className={darkMode ? "" : "text-slate-700 disabled:text-slate-500"}
                >
                  Visual Pause
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => sendRunControl("resume")}
                  disabled={!canControlRun || runControlMutation.isPending || !runControlState.pause_requested}
                  className={darkMode ? "" : "text-slate-700 disabled:text-slate-500"}
                >
                  Resume
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => sendRunControl("cancel")}
                  disabled={!canControlRun || runControlMutation.isPending || runControlState.cancel_requested}
                  className={darkMode ? "" : "text-slate-700 disabled:text-slate-500"}
                >
                  Cancel
                </Button>
              </div>
              {showProcessingDetails ? (
                <div className={processingDetailLines}>
                  <p className={`pb-1 text-[11px] font-semibold uppercase tracking-normal ${processingSubtext}`}>
                    Verified event history
                  </p>
                  {processingSnapshot.lines.map((line, idx) => (
                    <div key={`${idx}-${line.at}-${line.text}`} className="grid min-w-0 grid-cols-[4.25rem_minmax(0,1fr)] gap-2 py-1">
                      <span className={processingSubtext}>{new Date(line.at).toLocaleTimeString()}</span>
                      <p className="min-w-0 break-words">
                        <span className="font-medium">{line.stage}</span>
                        {line.provider ? ` | ${line.provider}` : ""}: {line.text}
                      </p>
                    </div>
                  ))}
                  {attachments.length > 0 ? (
                    <p className={`text-[11px] ${textSoft}`}>Attachment context: {attachmentSummaryText}</p>
                  ) : null}
                </div>
              ) : null}
            </div>

            {checkpoints.length > 0 ? (
              <Card className={`mt-5 ${workspaceCard}`}>
                <CardContent className="space-y-3 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <h2 className={`text-sm font-semibold ${sectionTitle}`}>Wave Checkpoints</h2>
                    <span className={`text-xs ${textSoft}`}>{pendingCheckpointCount} pending</span>
                  </div>
                  {checkpoints.slice(0, 4).map((checkpoint) => {
                    const checkpointId = String(checkpoint?.checkpoint_id || "");
                    const checkpointStatus = String(checkpoint?.status || "pending");
                    const isPending = checkpointStatus === "pending";
                    return (
                      <div key={checkpointId} className={`rounded-lg border p-3 ${darkMode ? "border-[#7B1FFF]/18 bg-[#07030F]/40" : "border-slate-200 bg-slate-50"}`}>
                        <div className="flex items-center justify-between gap-2">
                          <p className={`text-sm ${sectionTitle}`}>Wave {Number(checkpoint?.wave || 0)} checkpoint</p>
                          <span className={`rounded-full px-2 py-1 text-xs ${isPending ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}`}>
                            {checkpointStatus}
                          </span>
                        </div>
                        <div className="mt-2 flex gap-2">
                          <Button size="sm" variant="outline" disabled={!isPending || checkpointDecisionMutation.isPending} onClick={() => submitCheckpointDecision(checkpointId, "accept")}>
                            Accept
                          </Button>
                          <Button size="sm" variant="outline" disabled={!isPending || checkpointDecisionMutation.isPending} onClick={() => submitCheckpointDecision(checkpointId, "reject")}>
                            Reject
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </CardContent>
              </Card>
            ) : null}

            {inferenceAssumptionMessages.length > 0 || lastFailureDetail || errors.length > 0 ? (
              <Card className="mt-5 border-amber-300 bg-amber-50">
                <CardContent className="space-y-2 p-4 text-sm text-amber-900">
                  {inferenceAssumptionMessages.slice(0, 3).map((message, idx) => (
                    <p key={`${message}-${idx}`}>{message}</p>
                  ))}
                  {lastFailureDetail ? <p>{lastFailureDetail.reason}</p> : null}
                  {errors.map((err, idx) => (
                    <p key={`${err}-${idx}`}>{err}</p>
                  ))}
                </CardContent>
              </Card>
            ) : null}

          </section>

          <aside className="space-y-4">
            <Card className={workspaceCard}>
              <CardContent className="p-4">
                <div className="mb-5 flex items-center justify-between">
                  <h2 className={`text-sm font-semibold ${sectionTitle}`}>Integration Status</h2>
                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-600">
                    <CheckCircle2 size={13} />
                    Connected
                  </span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <span className="h-3 w-3 rounded-full bg-emerald-500" />
                    <div>
                      <p className="text-sm font-semibold">Zendesk</p>
                      <p className={`text-xs ${textSoft}`}>{zendeskCredentials.subdomain || "workspace"}.zendesk.com</p>
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    className={darkMode ? "" : "border-violet-200 bg-white text-violet-700 hover:bg-violet-50"}
                    onClick={() => mutateZendeskValidation(zendeskCredentials)}
                    disabled={zendeskValidationPending}
                  >
                    Validate
                  </Button>
                </div>
              </CardContent>
            </Card>

            <Card className={workspaceCard}>
              <CardContent className="p-4">
                <h2 className={`text-sm font-semibold ${sectionTitle}`}>Selected Context</h2>
                <div className="mt-5 flex items-center gap-3">
                  <span className={`flex h-9 w-9 items-center justify-center rounded-lg ${darkMode ? "bg-[#7B1FFF]/20 text-violet-200" : "bg-violet-50 text-violet-600"}`}>
                    <Library size={18} />
                  </span>
                  <p className="text-sm font-semibold">{selectedRelatedObjects.length} objects selected</p>
                </div>
                <p className={`mt-3 text-sm leading-6 ${textSoft}`}>Add relevant objects to improve AI suggestions</p>
                <Button
                  variant="outline"
                  className={`${darkMode ? "" : "border-violet-200 bg-white text-violet-700 hover:bg-violet-50"} mt-4 w-full gap-2`}
                  onClick={openContextSelection}
                >
                  Add Context
                  <CirclePlus size={16} />
                </Button>
              </CardContent>
            </Card>

            <Card id="settings" className={workspaceCard}>
              <CardContent className="space-y-4 p-4">
                <h2 className={`text-sm font-semibold ${sectionTitle}`}>Advanced Options</h2>
                <label className="block">
                  <span className={`text-xs ${textSoft}`}>Dependency Behavior</span>
                  <select
                    value={dependencyMode}
                    onChange={(event) => setDependencyMode(event.target.value)}
                    disabled={operationMode === "update"}
                    className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-60 ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/60 text-slate-100" : "border-slate-200 bg-white text-slate-700"}`}
                  >
                    <option value="match_existing_or_create_new">Match existing or create new</option>
                    <option value="force_existing_only">Existing only</option>
                    <option value="force_create_new">Always create new</option>
                  </select>
                </label>
                <label className="block">
                  <span className={`text-xs ${textSoft}`}>Existing Object Behavior</span>
                  <select
                    value={onExistingMode}
                    onChange={(event) => setOnExistingMode(event.target.value)}
                    disabled={operationMode === "update"}
                    className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-60 ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/60 text-slate-100" : "border-slate-200 bg-white text-slate-700"}`}
                  >
                    <option value="create_new">Create new anyway</option>
                    <option value="skip_existing">Skip existing</option>
                    <option value="overwrite_existing">Update existing</option>
                  </select>
                </label>
                <div>
                  <div className="mb-3 flex items-center justify-between">
                    <span className={`text-xs ${textSoft}`}>Focus Objects</span>
                    <button
                      type="button"
                      onClick={() => setFocusObjectTypes([])}
                      disabled={operationMode === "update"}
                      className={`rounded-full px-3 py-1 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60 ${focusObjectTypes.length === 0 ? "bg-violet-100 text-violet-700" : textSoft}`}
                    >
                      Auto
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {FOCUS_CHIPS.map((option) => {
                      const active = activeFocusSet.has(option.key);
                      return (
                        <button
                          key={option.key}
                          type="button"
                          onClick={() => toggleFocusObjectType(option.key)}
                          disabled={operationMode === "update"}
                          className={`rounded-lg border px-3 py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-60 ${
                            active
                              ? "border-violet-300 bg-violet-50 text-violet-700"
                              : darkMode
                                ? "border-[#7B1FFF]/22 text-[#B9A7D9] hover:bg-[#7B1FFF]/12"
                                : "border-violet-200 text-violet-700 hover:bg-violet-50"
                          }`}
                        >
                          {option.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </CardContent>
            </Card>
          </aside>

          <div className="lg:col-span-2">
            {showExistingContext ? (
              <div id="integration-diagnostics" className="mb-6">
                <IntegrationPanel
                  darkMode={darkMode}  
                  integrationsStatus={integrationsQuery.data}
                  integrationsLoading={integrationsQuery.isLoading}
                  onRefreshIntegrations={() => integrationsQuery.refetch()}
                  generateMetadata={effectiveGenerateMetadata}
                  approvalMetadata={approveMutation.data?.metadata}
                  onValidateZendesk={(payload) => mutateZendeskValidation(payload)}
                  isValidatingZendesk={zendeskValidationPending}
                  zendeskValidationResult={zendeskValidationResult}
                  zendeskValidated={zendeskValidated}
                  contextStatus={zendeskContextQuery.data}
                  contextLoading={zendeskContextQuery.isFetching}
                  selectedContextCount={selectedRelatedObjects.length}
                />
                <Card id="recent-requests" className={`mt-4 ${workspaceCard}`}>
                  <CardContent className="grid gap-4 p-4 lg:grid-cols-2">
                    <div id="context-selection">
                      <h2 className={`mb-2 text-sm font-semibold ${sectionTitle}`}>Recent Requests</h2>
                      <input
                        value={historySearch}
                        onChange={(event) => setHistorySearch(event.target.value)}
                        placeholder="Search by prompt or batch id..."
                        className={`mb-2 w-full rounded-lg border px-3 py-2 text-sm ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/60 text-slate-100" : "border-slate-200 bg-white text-slate-700"}`}
                      />
                      <div className="space-y-2">
                        {filteredHistory.slice(0, 5).map((item) => (
                          <button
                            key={item.batch_id}
                            type="button"
                            onClick={() => selectHistoryBatch(item.batch_id)}
                            className={`w-full rounded-lg border px-3 py-2 text-left text-xs ${hoverCard}`}
                          >
                            <span className="font-mono">{item.batch_id}</span>
                            <p className={`mt-1 ${textSoft}`}>{item.prompt_preview || item.status}</p>
                          </button>
                        ))}
                        {filteredHistory.length === 0 ? (
                          <p className={`text-xs ${textSoft}`}>No matching requests.</p>
                        ) : null}
                      </div>
                    </div>
                    <div>
                      <div className="mb-2 flex items-center justify-between">
                        <h2 className={`text-sm font-semibold ${sectionTitle}`}>Context Selection</h2>
                        <Button size="sm" variant="outline" onClick={() => setShowAdvancedCatalog((prev) => !prev)}>
                          {showAdvancedCatalog ? "Hide" : "Show"}
                        </Button>
                      </div>
                      {!contextCatalog ? (
                        <p className={`text-xs ${textSoft}`}>Context will appear after Zendesk session validation.</p>
                      ) : showAdvancedCatalog ? (
                        <div className="grid gap-3 sm:grid-cols-2">
                          {Object.entries(contextCatalog).slice(0, 6).map(([catalogKey, entries]) => (
                            <div key={catalogKey} className={`rounded-lg border p-2 ${darkMode ? "border-[#7B1FFF]/18 bg-[#07030F]/40" : "border-slate-200 bg-slate-50"}`}>
                              <p className={`mb-2 text-xs font-semibold ${textSoft}`}>{sectionLabel(catalogKey)}</p>
                              <div className="space-y-1">
                                {(entries || []).slice(0, 4).map((entry) => {
                                  const selectedKey = `${entry.object_type}:${entry.id}`;
                                  const selected = Boolean(selectedContext[selectedKey]);
                                  return (
                                    <button
                                      key={selectedKey}
                                      type="button"
                                      onClick={() => toggleContextSelection(entry)}
                                      className={`w-full rounded-md border px-2 py-1 text-left text-xs ${
                                        selected
                                          ? "border-emerald-500 bg-emerald-50 text-emerald-700"
                                          : darkMode
                                            ? "border-[#7B1FFF]/18 bg-[#120522]/40 text-slate-300"
                                            : "border-slate-200 bg-white text-slate-600"
                                      }`}
                                    >
                                      {entry.name}
                                    </button>
                                  );
                                })}
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className={`text-xs ${textSoft}`}>Open the advanced list to select existing Zendesk objects for context.</p>
                      )}
                    </div>
                  </CardContent>
                </Card>
                <Card className={`mt-4 ${workspaceCard}`}>
                  <CardContent className="p-4">
                    <div className="mb-3 flex items-center justify-between">
                      <h2 className={`text-sm font-semibold ${sectionTitle}`}>Recent Activity</h2>
                      <span className={`text-xs ${textSoft}`}>{currentPhaseLabel}</span>
                    </div>
                    <div className="space-y-2">
                      {timeline.length > 0 ? (
                        timeline.slice(-4).map((entry) => (
                          <div key={entry.id} className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm ${darkMode ? "border-[#7B1FFF]/18 bg-[#07030F]/40" : "border-slate-200 bg-white"}`}>
                            <div className="min-w-0 flex-1">
                              <p className={`${sectionTitle} break-words`}>{entry.text}</p>
                              <p className={`mt-1 text-xs ${textSoft}`}>{entry.role} - {new Date(entry.at).toLocaleTimeString()}</p>
                            </div>
                            <ChevronRight className={`${textSoft} shrink-0`} size={18} />
                          </div>
                        ))
                      ) : (
                        <div className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm ${darkMode ? "border-[#7B1FFF]/18 bg-[#07030F]/40" : "border-slate-200 bg-white"}`}>
                          <div className="flex items-center gap-3">
                            <span className="flex h-9 w-9 items-center justify-center rounded-full bg-violet-100 text-violet-600">
                              <CirclePlus size={18} />
                            </span>
                            <div>
                              <p className={sectionTitle}>Create trigger for inactive tickets</p>
                              <p className={`text-xs ${textSoft}`}>Trigger - ready to generate</p>
                            </div>
                          </div>
                          <span className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-600">Ready</span>
                        </div>
                      )}
                      <div ref={conversationEndRef} />
                    </div>
                  </CardContent>
                </Card>
                {activityLogs.length > 0 ? (
                  <Card className={`mt-4 ${workspaceCard}`}>
                    <CardContent className={`space-y-2 p-4 text-xs ${textSoft}`}>
                      <h2 className={`text-sm font-semibold ${sectionTitle}`}>Action Log</h2>
                      {activityLogs.slice(0, 8).map((entry, index) => (
                        <p key={`${entry.at}-${index}`}>
                          {entry.level}: {entry.message}
                        </p>
                      ))}
                    </CardContent>
                  </Card>
                ) : null}
              </div>
            ) : null}
            <PreviewWorkspace
              previewData={previewData}
              generatedData={generatedData}
              decisions={decisions}
              onDecisionChange={(recordId, decision) =>
                setDecisions((prev) => ({ ...prev, [recordId]: decision }))
              }
              onApproveDecisions={saveApproval}
              onApproveAndDeploy={approveAndDeploy}
              isApproving={approveMutation.isPending}
              onDeployToZendesk={deployToZendesk}
              isDeploying={deployMutation.isPending}
              deployResult={deployMutation.data}
              deploymentMetadata={jobQuery.data?.metadata?.zendesk_deploy || null}
              helpCenterUrl={helpCenterUrl}
              onHelpCenterUrlChange={updateHelpCenterUrl}
              helpCenterReadiness={helpCenterReadiness}
              onVerifyHelpCenter={verifyHelpCenter}
              isVerifyingHelpCenter={helpCenterReadinessMutation.isPending}
              helpCenterArticleMode={helpCenterArticleMode}
              onHelpCenterArticleModeChange={setHelpCenterArticleMode}
              onDeployHelpCenter={deployHelpCenter}
            />
          </div>
        </main>
      </div>
    </div>
  );

}

export default App;
