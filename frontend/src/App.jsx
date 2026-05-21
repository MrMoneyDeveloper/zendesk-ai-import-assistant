import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import PromptComposer from "./components/chat/PromptComposer";
import PreviewWorkspace from "./components/chat/PreviewWorkspace";
import StatusRibbon from "./components/chat/StatusRibbon";
import IntegrationPanel from "./components/chat/IntegrationPanel";
import ZendeskSessionGate from "./components/chat/ZendeskSessionGate";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { Card, CardContent } from "./components/ui/card";
import cxHeroBanner from "./assets/cx-hero-banner.png";
import cxIcon from "./assets/cx-icon.png";
import cxLogo from "./assets/cx-logo.png";
import {
  approveBatch,
  deployBatch,
  extractAttachment,
  generateBatch,
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
    return {
      stage: detail.failure_stage || null,
      code: detail.failure_code || null,
      reason: detail.failure_reason || "Request failed.",
      nextStep: detail.next_step || null,
    };
  }
  return null;
}

function parseFailureDetail(error) {
  const structured = extractFailurePayload(error);
  if (structured) {
    if (structured.code === "rate_limited") {
      const reason = structured.reason || "Request was rate-limited by the provider.";
      const next = structured.nextStep
        || "Wait for the rate-limit window to reset, then retry.";
      const stage = structured.stage ? `[${structured.stage}] ` : "";
      return `${stage}${reason} Next: ${next}`;
    }
    const stage = structured.stage ? `[${structured.stage}] ` : "";
    const next = structured.nextStep ? ` Next: ${structured.nextStep}` : "";
    const reason = structured.reason || "Request failed.";
    return `${stage}${reason}${next}`;
  }
  const detail = error?.response?.data?.detail;
  return detail || error?.message || "Request failed.";
}

function statusBadgeVariant(status) {
  if (["deployed", "approved", "preview_ready"].includes(status)) return "success";
  if (["deploy_failed", "failed", "validated_failed"].includes(status)) return "danger";
  if (["deployed_partial", "validated_warning"].includes(status)) return "warning";
  return "neutral";
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
  generated: "Generation completed.",
  staging: "Writing staged records to Apps Script/Sheets.",
  staged: "Staging completed.",
  validating: "Running safety and validation checks.",
  validated_passed: "Validation passed.",
  validated_warning: "Validation passed with warnings.",
  validated_failed: "Validation failed.",
  preview_ready: "Preview ready for review.",
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
  const [decisions, setDecisions] = useState({});
  const [activityLogs, setActivityLogs] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [historySearch, setHistorySearch] = useState("");
  const [chatHistory, setChatHistory] = useState([]);
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
  const [processingClockMs, setProcessingClockMs] = useState(Date.now());
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
      queryClient.invalidateQueries({ queryKey: ["preview", data.batch_id] });
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      setLastFailureDetail(extractFailurePayload(error));
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
        queryClient.invalidateQueries({ queryKey: ["preview", batchId] });
      }
    },
    onError: (error) => {
      const detail = parseFailureDetail(error);
      appendActivity("error", detail);
      appendTimeline("assistant", `Deploy failed: ${detail}`);
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
  const selectedRelatedObjects = Object.values(selectedContext);
  const previewRecords = previewQuery.data?.records || [];
  const hasActiveBatchRun = Boolean(
    batchId
    && currentStatus
    && !TERMINAL_BATCH_STATUSES.has(String(currentStatus))
  );
  const shouldTickProcessingClock = Boolean(
    generateMutation.isPending
    || approveMutation.isPending
    || deployMutation.isPending
    || hasActiveBatchRun
  );

  useEffect(() => {
    if (!shouldTickProcessingClock) return undefined;
    setProcessingClockMs(Date.now());
    const interval = setInterval(() => {
      setProcessingClockMs(Date.now());
    }, 1000);
    return () => clearInterval(interval);
  }, [shouldTickProcessingClock]);

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
  const selectedExistingItemKeySet = useMemo(
    () => new Set(selectedExistingItems.map((item) => `${item.object_type}:${item.id}`)),
    [selectedExistingItems]
  );
  const existingItemOptions = useMemo(() => {
    if (!contextCatalog || !focusObjectTypes.length) return [];
    const allowedCatalogKeys = new Set(
      focusObjectTypes.flatMap((focus) => FOCUS_TO_CATALOG_KEYS[focus] || [])
    );
    const options = [];
    for (const [catalogKey, entries] of Object.entries(contextCatalog)) {
      if (!allowedCatalogKeys.has(catalogKey)) continue;
      (entries || []).forEach((entry) => {
        const key = `${entry.object_type}:${entry.id}`;
        options.push({
          key,
          label: `${sectionLabel(catalogKey)}: ${entry.name}`,
          catalogKey,
          entry,
        });
      });
    }
    options.sort((a, b) => a.label.localeCompare(b.label));
    return options;
  }, [contextCatalog, focusObjectTypes]);
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
      return {
        ...prev,
        [key]: entry,
      };
    });
  };

  const addExistingContextSelection = (selectionKey) => {
    if (!selectionKey) return;
    const option = existingItemOptions.find((item) => item.key === selectionKey);
    if (!option?.entry) return;
    setSelectedContext((prev) => ({
      ...prev,
      [selectionKey]: option.entry,
    }));
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
    jobQuery.data?.created_at,
    jobQuery.data?.metadata,
    jobQuery.data?.status_history,
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

  return (
    <div className="relative min-h-screen text-slate-100">
      <div className="cx-hero-watermark" aria-hidden="true">
        <img src={cxHeroBanner} alt="" />
      </div>
      <main className="relative z-10 mx-auto max-w-6xl px-4 py-5 lg:px-10">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <img src={cxIcon} alt="CX Icon" className="h-6 w-6 rounded-md border border-[#7B1FFF]/50" />
            <img src={cxLogo} alt="CX Experts Assistant" className="h-6 w-auto opacity-95" />
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowExistingContext((prev) => !prev)}
            >
              {showExistingContext ? "Hide Context" : "Existing Context"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={startNewChat}
            >
              New Chat
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={signOutZendeskSession}
            >
              Sign Out Session
            </Button>
          </div>
        </div>
        <div className="mb-4 cx-brand-divider" />

        <Card className="mb-4 border-[#7B1FFF]/30 bg-[#120522]/72">
          <CardContent className="space-y-4 p-4">
            <div className="max-h-[22rem] overflow-y-auto rounded-xl border border-[#7B1FFF]/20 bg-[#07030F]/45 p-3">
              {timeline.length > 0 ? (
                <div className="space-y-2">
                  {timeline.map((entry) => (
                    <div
                      key={entry.id}
                      className={`max-w-[92%] rounded-xl border px-3 py-2 text-sm ${
                        entry.role === "user"
                          ? "ml-auto border-[#7B1FFF]/45 bg-[#7B1FFF]/16 text-[#F4EEFF]"
                          : "border-[#7B1FFF]/25 bg-[#07030F]/60 text-[#B9A7D9]"
                      }`}
                    >
                      <p>{entry.text}</p>
                      <p className="mt-1 text-[10px] text-slate-500">{entry.at}</p>
                    </div>
                  ))}
                  <div ref={conversationEndRef} />
                </div>
              ) : (
                <div className="flex min-h-[10rem] items-center justify-center">
                  <p className="text-center text-4xl font-medium text-slate-200">
                    Where should we begin?
                  </p>
                </div>
              )}
            </div>

            <PromptComposer
              embedded
              onSubmitPrompt={submitPrompt}
              onExtractAttachment={handleExtractAttachment}
              onRemoveAttachment={removeAttachment}
              onAddExistingContext={addExistingContextSelection}
              onRemoveExistingContext={removeExistingContextSelection}
              attachments={attachments}
              isLoading={generateMutation.isPending || attachmentExtractMutation.isPending}
              isLocked={!zendeskValidated}
              lockReason="Validate Zendesk credentials first in Integration Diagnostics."
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
            />

            <div className="rounded-lg border border-[#7B1FFF]/24 bg-[#120522]/60 p-3">
              <button
                type="button"
                onClick={() => setShowProcessingDetails((prev) => !prev)}
                className="flex w-full items-center justify-between text-left"
              >
                <div>
                  <p className="text-sm font-semibold text-slate-200">
                    {isWorking ? "Processing..." : "Processing details"}
                  </p>
                  <p className="text-xs text-slate-400">{currentPhaseLabel}</p>
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-[#B9A7D9]">
                    <span className="rounded-full border border-[#7B1FFF]/40 bg-[#07030F]/70 px-2 py-0.5">
                      {processingSnapshot.badge}
                    </span>
                    <span>Elapsed: {processingSnapshot.elapsedLabel}</span>
                    <span>ETA: {processingSnapshot.etaLabel}</span>
                  </div>
                  <p className="mt-1 text-[11px] text-slate-300">
                    Currently doing: {processingSnapshot.currentDoing}
                  </p>
                </div>
                <span className="text-xs text-[#B9A7D9]">
                  {showProcessingDetails ? "Hide" : "Show"}
                </span>
              </button>
              {showProcessingDetails ? (
                <div className="mt-3 space-y-1 border-t border-[#7B1FFF]/20 pt-2 text-xs text-slate-300">
                  {processingSnapshot.lines.map((line, idx) => (
                    <p key={`${idx}-${line.at}-${line.text}`}>
                      [{new Date(line.at).toLocaleTimeString()}] {line.stage}: {line.text}
                    </p>
                  ))}
                  {attachments.length > 0 ? (
                    <p className="text-[11px] text-slate-400">
                      Attachment context: {attachmentSummaryText}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <StatusRibbon
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

        {showExistingContext ? (
          <div className="mb-6 space-y-4">
            <IntegrationPanel
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

            <Card className="border-[#7B1FFF]/30 bg-[#120522]/70">
              <CardContent className="space-y-4 p-4">
                <div>
                  <p className="mb-2 text-sm font-semibold text-slate-200">Recent Requests</p>
                  <input
                    value={historySearch}
                    onChange={(event) => setHistorySearch(event.target.value)}
                    placeholder="Search by prompt or batch id..."
                    className="mb-2 w-full rounded-md border border-[#7B1FFF]/35 bg-[#07030F]/70 px-2 py-1 text-sm text-[#F4EEFF]"
                  />
                  <div className="space-y-1">
                    {filteredHistory.slice(0, 10).map((item) => (
                      <button
                        key={item.batch_id}
                        type="button"
                        onClick={() => selectHistoryBatch(item.batch_id)}
                        className={`w-full rounded border px-2 py-2 text-left text-xs ${
                          batchId === item.batch_id
                            ? "border-[#7B1FFF]/60 bg-[#5B35FF]/18"
                            : "border-[#7B1FFF]/25 bg-[#07030F]/45 hover:bg-[#7B1FFF]/12"
                        }`}
                      >
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <span className="font-mono text-slate-300">{item.batch_id}</span>
                          <Badge variant={statusBadgeVariant(item.status)}>{item.status}</Badge>
                        </div>
                        <p className="text-slate-400">{item.prompt_preview || "No prompt preview."}</p>
                      </button>
                    ))}
                    {filteredHistory.length === 0 ? (
                      <p className="text-xs text-slate-500">No matching requests.</p>
                    ) : null}
                  </div>
                </div>

                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <p className="text-sm font-semibold text-slate-200">Context Selection</p>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => zendeskContextQuery.refetch()}
                        disabled={zendeskContextQuery.isFetching}
                      >
                        {zendeskContextQuery.isFetching ? "Refreshing..." : "Refresh"}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setShowAdvancedCatalog((prev) => !prev)}
                      >
                        {showAdvancedCatalog ? "Hide Advanced List" : "Advanced Catalog List"}
                      </Button>
                    </div>
                  </div>
                  {!contextCatalog ? (
                    <p className="text-xs text-slate-500">
                      Context will appear here after Zendesk session validation.
                    </p>
                  ) : showAdvancedCatalog ? (
                    <div className="grid gap-3 md:grid-cols-2">
                      {Object.entries(contextCatalog).map(([catalogKey, entries]) => (
                        <div key={catalogKey} className="rounded border border-[#7B1FFF]/25 bg-[#07030F]/45 p-2">
                          <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">
                            {sectionLabel(catalogKey)}
                          </p>
                          <div className="space-y-1">
                            {(entries || []).slice(0, 8).map((entry) => {
                              const selectedKey = `${entry.object_type}:${entry.id}`;
                              const selected = Boolean(selectedContext[selectedKey]);
                              return (
                                <button
                                  key={selectedKey}
                                  type="button"
                                  onClick={() => toggleContextSelection(entry)}
                                  className={`w-full rounded border px-2 py-1 text-left text-xs ${
                                    selected
                                      ? "border-emerald-700 bg-emerald-950/20 text-emerald-200"
                                      : "border-[#7B1FFF]/25 bg-[#120522]/40 text-slate-300 hover:bg-[#7B1FFF]/12"
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
                    <p className="text-xs text-slate-400">
                      Use the prompt-level <span className="font-semibold text-slate-200">Existing item</span> picker
                      for quick selection. Open <span className="font-semibold text-slate-200">Advanced Catalog List</span> if you need the full catalog.
                    </p>
                  )}
                </div>
              </CardContent>
            </Card>

            {activityLogs.length > 0 ? (
              <Card className="border-[#7B1FFF]/30 bg-[#120522]/70">
                <CardContent className="space-y-2 p-4 text-xs text-slate-300">
                  <p className="font-semibold text-slate-200">Action Log</p>
                  {activityLogs.slice(0, 10).map((entry, index) => (
                    <p key={`${entry.at}-${index}`}>
                      <span className="font-mono text-slate-400">{entry.at}</span> |{" "}
                      <span className="uppercase">{entry.level}</span> | {entry.message}
                    </p>
                  ))}
                </CardContent>
              </Card>
            ) : null}
          </div>
        ) : null}

        {inferenceAssumptionMessages.length > 0 ? (
          <Card className="mb-6 border-amber-700/50 bg-amber-950/20">
            <CardContent className="space-y-2 p-4 text-sm text-amber-100">
              <p className="font-semibold">Assumptions Applied</p>
              {inferenceAssumptionMessages.slice(0, 4).map((message, idx) => (
                <p key={`${message}-${idx}`} className="text-xs text-amber-200">
                  {message}
                </p>
              ))}
            </CardContent>
          </Card>
        ) : null}

        {lastFailureDetail ? (
          <Card className="mb-6 border-rose-800 bg-rose-950/30">
            <CardContent className="space-y-2 p-4 text-sm text-rose-200">
              <p className="font-semibold">
                {lastFailureDetail.stage ? `[${lastFailureDetail.stage}] ` : ""}
                {lastFailureDetail.reason}
              </p>
              {lastFailureDetail.nextStep ? (
                <p className="text-xs text-rose-300">Next step: {lastFailureDetail.nextStep}</p>
              ) : null}
            </CardContent>
          </Card>
        ) : null}

        {errors.length > 0 && (
          <Card className="mt-6 border-rose-800 bg-rose-950/30">
            <CardContent className="space-y-2 p-4 text-sm text-rose-200">
              {errors.map((err, idx) => (
                <p key={`${err}-${idx}`}>{err}</p>
              ))}
            </CardContent>
          </Card>
        )}

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
      </main>
    </div>
  );
}

export default App;
