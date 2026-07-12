import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
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
  LogOut,
  MessageSquare,
  Moon,
  PanelRightOpen,
  Plus,
  Settings,
  Sparkles,
  Sun,
  Workflow,
  Zap,
} from "lucide-react";

import PromptComposer from "./components/chat/PromptComposer";
import PreviewWorkspace from "./components/chat/PreviewWorkspace";
import StatusRibbon from "./components/chat/StatusRibbon";
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
    const waveMatch = message.match(/wave\s+(\d+)\/(\d+)\s+([a-z_]+)\s+chunk\s+(\d+)\/(\d+)/i);
    if (waveMatch) {
      return {
        badge: `Wave ${waveMatch[1]} • ${objectTypeLabel(waveMatch[3])} ${waveMatch[4]}/${waveMatch[5]}`,
        chunkIndex: Number(waveMatch[4]),
        chunkTotal: Number(waveMatch[5]),
      };
    }
    const chunkMatch = message.match(/chunk\s+(\d+)\/(\d+)/i);
    if (chunkMatch) {
      return {
        badge: `Chunk ${chunkMatch[1]}/${chunkMatch[2]}`,
        chunkIndex: Number(chunkMatch[1]),
        chunkTotal: Number(chunkMatch[2]),
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
    };
  }
  return {
    badge: "Preparing run",
    chunkIndex: 0,
    chunkTotal: 0,
  };
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
  const [processingClockMs, setProcessingClockMs] = useState(() => Date.now());
  const lastContextSyncRef = useRef("");
  const lastContextErrorRef = useRef("");
  const conversationEndRef = useRef(null);
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

  const generateMutation = useMutation({
    mutationFn: generateBatch,
    onMutate: () => {
      setActiveRunStartedAtMs(Date.now());
      setLastFailureDetail(null);
      appendActivity("info", "Started generate -> stage -> validate pipeline.");
      appendTimeline("assistant", "Processing your request...");
    },
    onSuccess: (data) => {
      setBatchId(data.batch_id);
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
        `assistant: batch ${data.batch_id} ready (passed=${data.validation_summary?.passed || 0}, warnings=${data.validation_summary?.warnings || 0}, blocked=${data.validation_summary?.blocked || 0})`,
      ]);
      appendActivity(
        "success",
        `Batch ${data.batch_id} ready for review. Validation summary: passed=${data.validation_summary?.passed || 0}, warnings=${data.validation_summary?.warnings || 0}, blocked=${data.validation_summary?.blocked || 0}.`
      );
      if (generationSafety?.blocked) {
        appendTimeline(
          "assistant",
          `Batch ${data.batch_id} generated, but deployment is blocked by safety checks. ${generationSafety.reasons?.join(" | ") || ""}`
        );
      } else {
        appendTimeline(
          "assistant",
          `Batch ${data.batch_id} is ready for review (${data.validation_summary?.passed || 0} passed, ${data.validation_summary?.warnings || 0} warnings, ${data.validation_summary?.blocked || 0} blocked).`
        );
      }
      if ((focusDiagnostics?.mismatch_count || 0) > 0) {
        appendActivity(
          "warning",
          `Object focus mismatch: ${focusDiagnostics.mismatch_count} generated records are outside selected focus (${(focusDiagnostics.generated_types || []).join(", ")}).`
        );
      }
      if (assumptionMessages.length > 0) {
        appendActivity(
          "warning",
          `Assumptions applied: ${assumptionMessages.join(" | ")}`
        );
        appendTimeline(
          "assistant",
          `Assumptions applied: ${assumptionMessages.join(" | ")}`
        );
      }
      queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
      queryClient.invalidateQueries({ queryKey: ["job", data.batch_id] });
      queryClient.invalidateQueries({ queryKey: ["checkpoints", data.batch_id] });
      queryClient.invalidateQueries({ queryKey: ["preview", data.batch_id] });
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
      if (!status) return 2500;
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
      return 2500;
    },
  });

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
    onMutate: () => {
      appendActivity("info", "Deploying approved records to live Zendesk instance.");
      appendTimeline("assistant", "Deploying approved records to Zendesk...");
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
    enabled: Boolean(batchId),
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
    if (dependencyMode === "force_existing_only" && existingItemBehavior === "create_new") {
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
    if (dependencyMode === "force_existing_only" && selectedRelatedObjects.length === 0) {
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

    const referenceCatalog = contextCatalog || {};
    const recentBatchContext = localHistory.slice(-8);
    const promptForModel = buildPromptWithAttachments(promptText);
    const selectedRelatedObjectsForRequest = existingItemBehavior === "create_new"
      ? []
      : selectedRelatedObjects;
    const attachmentNote = attachments.length
      ? `Attachment context included from ${attachments.length} file(s): ${attachments.map((item) => item.filename).join(", ")}.`
      : "";

    generateMutation.mutate({
      prompt: promptForModel,
      target_environment: "sandbox",
      mode: "generate_validate_preview",
      requester: "local-user",
      dependency_mode: dependencyMode,
      focus_object_types: focusObjectTypes,
      related_objects: selectedRelatedObjectsForRequest,
      reference_catalog: referenceCatalog,
      recent_batch_context: recentBatchContext,
      context_notes: [
        `Existing item behavior: ${existingItemBehavior === "create_new" ? "ignore_selected_existing_and_create_new" : "use_selected_existing_as_base_or_reference"}.`,
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

  const generatedData = generateMutation.data;
  const effectiveGenerateMetadata = generatedData?.metadata || jobQuery.data?.metadata || {};
  const previewData = previewQuery.data;
  const historyItems = jobsQuery.data?.jobs || [];
  const contextCatalog = zendeskContextQuery.data?.catalogs || null;
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
    ? "No help centers were returned from Zendesk context sync. Refresh context in Existing Context."
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
    setDependencyMode("match_existing_or_create_new");
    setOnExistingMode("create_new");
    setExistingItemBehavior("relate_or_update");
    setFocusObjectTypes([]);
    setAttachments([]);
    setShowAdvancedCatalog(false);
    setActiveRunStartedAtMs(null);
    lastContextSyncRef.current = "";
    lastContextErrorRef.current = "";
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
    setExistingItemBehavior("relate_or_update");
    setOnExistingMode("create_new");
    setFocusObjectTypes([]);
    setAttachments([]);
    setShowAdvancedCatalog(false);
    setShowProcessingDetails(false);
    setShowExistingContext(false);
    setActiveRunStartedAtMs(null);
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
    appendActivity("info", `Loaded batch ${selectedBatchId} from history.`);
    queryClient.invalidateQueries({ queryKey: ["job", selectedBatchId] });
    queryClient.invalidateQueries({ queryKey: ["preview", selectedBatchId] });
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
        on_existing: onExistingMode,
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
        on_existing: onExistingMode,
      });
    } catch (error) {
      appendActivity(
        "error",
        parseFailureDetail(error) || "Could not complete approve-and-deploy action."
      );
    }
  };

  const isWorking =
    generateMutation.isPending
    || approveMutation.isPending
    || deployMutation.isPending
    || jobQuery.isFetching
    || attachmentExtractMutation.isPending;

  const currentPhaseLabel = (() => {
    if (attachmentExtractMutation.isPending) return "Extracting attachment context...";
    if (deployMutation.isPending) return "Deploying approved records to Zendesk...";
    if (approveMutation.isPending) return "Writing approval decisions to Apps Script...";
    if (generateMutation.isPending) return "Generating and staging records...";
    const status = currentStatus || "";
    const phaseMap = {
      request_validated: "Validating request...",
      planning: "Planning configuration...",
      planned: "Plan ready.",
      clarification_required: "Waiting for clarification before generation...",
      schemas_selected: "Selecting schemas...",
      generating: "Generating records...",
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
    const runtimeMetadata = generatedData?.metadata || jobQuery.data?.metadata || {};
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
        lines.unshift({
          at: new Date().toISOString(),
          stage: "wave_execution",
          text: waveProgress.slice(0, 2).join(" | "),
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
      lines.unshift({
        at: new Date().toISOString(),
        stage: "supervisor_review",
        text: supervisorText,
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
    const runtimeMetadata = generatedData?.metadata || jobQuery.data?.metadata || {};
    const progress = extractWaveChunkProgress(jobQuery.data?.status_history || [], runtimeMetadata);
    const fallbackStart = jobQuery.data?.created_at ? Date.parse(jobQuery.data.created_at) : null;
    const startAtMs = activeRunStartedAtMs || fallbackStart || null;
    const elapsedMs = startAtMs ? Math.max(0, processingClockMs - startAtMs) : 0;
    let etaMs = null;
    if (progress.chunkTotal > 0 && progress.chunkIndex > 0 && progress.chunkTotal > progress.chunkIndex) {
      const chunkAverage = elapsedMs / progress.chunkIndex;
      etaMs = Math.max(0, Math.round(chunkAverage * (progress.chunkTotal - progress.chunkIndex)));
    }
    const currentLine = processingLines[0];
    return {
      badge: progress.badge || "Preparing run",
      elapsedLabel: formatDuration(elapsedMs),
      etaLabel: etaMs === null ? "estimating" : formatDuration(etaMs),
      currentDoing: currentLine?.text || currentPhaseLabel,
      lines: processingLines.slice(0, 8),
    };
  }, [
    activeRunStartedAtMs,
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
    ? "rounded-lg border border-[#7B1FFF]/24 bg-[#120522]/60 p-3"
    : "rounded-lg border border-slate-200 bg-slate-100 p-3";

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
      onClick={() => {
        if (item.label === "New Chat") startNewChat();
        else if (item.label === "Dashboard") appendTimeline("assistant", "Dashboard view coming soon.");
        else if (item.label === "All Chats") setShowExistingContext(true);
        else if (item.label === "Integrations") setShowExistingContext(true);
        else if (item.label === "Settings") appendTimeline("assistant", "Settings panel coming soon.");
        // Templates and Knowledge Base are future features
      }}
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
          <div className="flex items-center gap-3 xl:hidden">
            <img src={cxIcon} alt="CX" className="h-10 w-10 rounded-lg" />
            <div>
              <p className="font-semibold">CX Experts</p>
              <p className={`text-xs ${textSoft}`}>AI Assistant</p>
            </div>
          </div>
          <div className="hidden xl:block" />
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              className={`${darkMode ? "" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"} gap-2`}
              onClick={() => setShowExistingContext((prev) => !prev)}
            >
              <PanelRightOpen size={16} />
              Context
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
              className="gap-2 bg-[#7B1FFF] text-white hover:bg-[#9B35FF]"
              onClick={startNewChat}
            >
              <CirclePlus size={16} />
              New Chat
            </Button>
          </div>
        </header>

        <main className="mx-auto grid max-w-[1480px] gap-6 px-4 pb-10 sm:px-6 lg:grid-cols-[minmax(0,1fr)_330px] lg:px-8">
          <section className="min-w-0">
            <div className={`relative overflow-hidden rounded-2xl border p-7 ${workspaceCard}`}>
              <div className="relative z-10 max-w-xl pr-8">
                <h1 className="text-3xl font-bold tracking-normal text-violet-600">Zendesk AI Import</h1>
                <p className={`mt-3 text-xl font-semibold ${sectionTitle}`}>What would you like to build today?</p>
                <p className={`mt-3 max-w-xl text-sm leading-6 ${textSoft}`}>
                  Generate Zendesk configurations, automate workflows, and create help center content with AI.
                </p>
              </div>
              <div className="pointer-events-none absolute inset-y-0 right-0 w-2/3 opacity-70">
                <img src={darkMode ? cxHeroBanner : cxHeroLight} alt="" className={`h-full w-full ${
      darkMode
        ? "object-cover object-right opacity-80"
        : "object-cover object-center opacity-100"
    }`} />
              </div>
            </div>

            <div className="mt-6">
              <h2 className={`mb-3 text-sm font-semibold ${sectionTitle}`}>Quick Actions</h2>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                {QUICK_ACTIONS.map((action) => {
  const Icon = action.icon;
  return (
    <button
      key={action.label}
      type="button"
      onClick={() => setExternalPrompt(action.prompt)}
      className={`rounded-lg border p-4 text-left transition ${hoverCard}`}
    >
      <Icon className={`h-8 w-8 ${action.color}`} />
      <p className={`mt-3 text-sm font-semibold ${sectionTitle}`}>{action.label}</p>
      <p className={`mt-1 text-xs leading-5 ${textSoft}`}>{action.detail}</p>
    </button>
  );
})}
              </div>
            </div>

            <div className="mt-4">
              <PromptComposer
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
                isLocked={!zendeskValidated}
                lockReason="Validate Zendesk credentials first in Integration Status."
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
                 externalPrompt={externalPrompt}                        // ← new
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
      className={`flex items-center gap-3 rounded-lg border p-3 text-left text-sm transition ${hoverCard}`}
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

            <Card className={`mt-5 ${workspaceCard}`}>
              <CardContent className="p-4">
                <div className="mb-3 flex items-center justify-between">
                  <h2 className={`text-sm font-semibold ${sectionTitle}`}>Recent Activity</h2>
                  <span className={`text-xs ${textSoft}`}>{currentPhaseLabel}</span>
                </div>
                <div className="space-y-2">
                  {timeline.length > 0 ? (
                    timeline.slice(-4).map((entry) => (
                      <div key={entry.id} className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm ${darkMode ? "border-[#7B1FFF]/18 bg-[#07030F]/40" : "border-slate-200 bg-white"}`}>
                        <div>
                          <p className={sectionTitle}>{entry.text}</p>
                          <p className={`mt-1 text-xs ${textSoft}`}>{entry.role} - {new Date(entry.at).toLocaleTimeString()}</p>
                        </div>
                        <ChevronRight className={textSoft} size={18} />
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

            <div className={`mt-5 ${processingPanelBg}`}>
              <button
                type="button"
                onClick={() => setShowProcessingDetails((prev) => !prev)}
                className="flex w-full items-center justify-between text-left"
              >
                <div>
                  <p className={`text-sm font-semibold ${processingHeading}`}>
                    {isWorking ? "Processing..." : "Processing details"}
                  </p>
                  <p className={`text-xs ${processingSubtext}`}>{currentPhaseLabel}</p>
                  <div className={`mt-2 flex flex-wrap items-center gap-2 text-[11px] ${processingBody}`}>
                    <span className={badgePill}>{processingSnapshot.badge}</span>
                    <span>Elapsed: {processingSnapshot.elapsedLabel}</span>
                    <span>ETA: {processingSnapshot.etaLabel}</span>
                    {pendingCheckpointCount > 0 ? (
                      <span>Checkpoints: {pendingCheckpointCount}</span>
                    ) : null}
                  </div>
                </div>
                <span className={`text-xs ${textSoft}`}>{showProcessingDetails ? "Hide" : "Show"}</span>
              </button>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => sendRunControl("pause")}
                  disabled={!canControlRun || runControlMutation.isPending || runControlState.pause_requested}
                >
                  Visual Pause
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => sendRunControl("resume")}
                  disabled={!canControlRun || runControlMutation.isPending || !runControlState.pause_requested}
                >
                  Resume
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => sendRunControl("cancel")}
                  disabled={!canControlRun || runControlMutation.isPending || runControlState.cancel_requested}
                >
                  Cancel
                </Button>
              </div>
              {showProcessingDetails ? (
                <div className={processingDetailLines}>
                  {processingSnapshot.lines.map((line, idx) => (
                    <p key={`${idx}-${line.at}-${line.text}`}>
                      [{new Date(line.at).toLocaleTimeString()}] {line.stage}: {line.text}
                    </p>
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

            <StatusRibbon
              darkMode={darkMode}
              batchId={batchId}
              status={currentStatus}
              testResult={null}
              jobData={jobQuery.data}
              generateMetadata={effectiveGenerateMetadata}
              approvalResult={approveMutation.data}
              deployEnabled={Boolean(integrationsQuery.data?.zendesk?.deploy_endpoint_enabled)}
              deployTarget={zendeskValidationResult?.base_url || ""}
              onExistingMode={onExistingMode}
            />
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
                  onClick={() => setShowExistingContext((prev) => !prev)}
                >
                  Add Context
                  <CirclePlus size={16} />
                </Button>
              </CardContent>
            </Card>

            <Card className={workspaceCard}>
              <CardContent className="space-y-4 p-4">
                <h2 className={`text-sm font-semibold ${sectionTitle}`}>Advanced Options</h2>
                <label className="block">
                  <span className={`text-xs ${textSoft}`}>Dependency Behavior</span>
                  <select value={dependencyMode} onChange={(event) => setDependencyMode(event.target.value)} className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/60 text-slate-100" : "border-slate-200 bg-white text-slate-700"}`}>
                    <option value="match_existing_or_create_new">Match existing or create new</option>
                    <option value="force_existing_only">Existing only</option>
                    <option value="force_create_new">Always create new</option>
                  </select>
                </label>
                <label className="block">
                  <span className={`text-xs ${textSoft}`}>Existing Object Behavior</span>
                  <select value={onExistingMode} onChange={(event) => setOnExistingMode(event.target.value)} className={`mt-2 w-full rounded-lg border px-3 py-2 text-sm ${darkMode ? "border-[#7B1FFF]/25 bg-[#07030F]/60 text-slate-100" : "border-slate-200 bg-white text-slate-700"}`}>
                    <option value="create_new_anyway">Create new anyway</option>
                    <option value="skip_existing">Skip existing</option>
                    <option value="update_existing">Update existing</option>
                  </select>
                </label>
                <div>
                  <div className="mb-3 flex items-center justify-between">
                    <span className={`text-xs ${textSoft}`}>Focus Objects</span>
                    <button type="button" onClick={() => setFocusObjectTypes([])} className={`rounded-full px-3 py-1 text-xs font-medium ${focusObjectTypes.length === 0 ? "bg-violet-100 text-violet-700" : textSoft}`}>
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
                          className={`rounded-lg border px-3 py-1.5 text-xs ${
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
              <div className="mb-6">
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
                <Card className={`mt-4 ${workspaceCard}`}>
                  <CardContent className="grid gap-4 p-4 lg:grid-cols-2">
                    <div>
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
            />
          </div>
        </main>
      </div>
    </div>
  );

}

export default App;
