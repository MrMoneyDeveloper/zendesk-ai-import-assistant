import { useCallback, useEffect, useRef, useState } from "react";
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
  generateBatch,
  getZendeskContext,
  getIntegrationsStatus,
  getJob,
  listJobs,
  getPreview,
  testApis,
  validateZendeskCredentials,
} from "./services/api";
import { useImportAssistantStore } from "./store/importAssistantStore";

const ZENDESK_SESSION_STORAGE_KEY = "zendesk_session_credentials_v1";

function statusBadgeVariant(status) {
  if (["deployed", "approved", "preview_ready"].includes(status)) return "success";
  if (["deploy_failed", "failed", "validated_failed"].includes(status)) return "danger";
  if (["deployed_partial", "validated_warning"].includes(status)) return "warning";
  return "neutral";
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
  const [testResult, setTestResult] = useState(null);
  const [decisions, setDecisions] = useState({});
  const [activityLogs, setActivityLogs] = useState([]);
  const [historySearch, setHistorySearch] = useState("");
  const [chatHistory, setChatHistory] = useState([]);
  const [selectedContext, setSelectedContext] = useState({});
  const [dependencyMode, setDependencyMode] = useState("match_existing_or_create_new");
  const [onExistingMode, setOnExistingMode] = useState("create_new");
  const [showSettings, setShowSettings] = useState(false);
  const [clarificationQuestions, setClarificationQuestions] = useState([]);
  const lastContextSyncRef = useRef("");
  const lastContextErrorRef = useRef("");
  const [zendeskValidated, setZendeskValidated] = useState(false);
  const [bootZendeskSession] = useState(() => readZendeskSessionCredentials());
  const [zendeskCredentials, setZendeskCredentials] = useState(() =>
    bootZendeskSession || {
      subdomain: "",
      email: "",
      api_token: "",
    }
  );

  const { batchId, setBatchId, prompt, setPrompt, resetFlow } =
    useImportAssistantStore();

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

  const testMutation = useMutation({
    mutationFn: testApis,
    onSuccess: (data) => {
      setTestResult(data);
      appendActivity("success", `API test completed: ${data.provider} | ${data.model} | ${data.status}.`);
    },
    onError: (err) => {
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail || err?.message || "API test failed.";
      setTestResult({ status: "error", detail: status ? `HTTP ${status}: ${detail}` : detail });
      appendActivity("error", `API test failed: ${status ? `HTTP ${status}` : detail}`);
    },
  });

  const generateMutation = useMutation({
    mutationFn: generateBatch,
    onMutate: () => {
      appendActivity("info", "Started generate -> stage -> validate pipeline.");
    },
    onSuccess: (data) => {
      setBatchId(data.batch_id);
      if (data.status === "clarification_required" || data.needs_clarification) {
        const questions = data.clarification_questions || [];
        setClarificationQuestions(questions);
        setChatHistory((prev) => [
          ...prev,
          `assistant: clarification required (${questions[0]?.question || "additional detail needed"})`,
        ]);
        appendActivity(
          "warning",
          `Clarification required before generation. Questions: ${questions.map((q) => q.question).join(" | ")}`
        );
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
        queryClient.invalidateQueries({ queryKey: ["job", data.batch_id] });
        return;
      }
      setClarificationQuestions([]);
      setChatHistory((prev) => [
        ...prev,
        `assistant: batch ${data.batch_id} ready (passed=${data.validation_summary?.passed || 0}, warnings=${data.validation_summary?.warnings || 0}, blocked=${data.validation_summary?.blocked || 0})`,
      ]);
      appendActivity(
        "success",
        `Batch ${data.batch_id} ready for review. Validation summary: passed=${data.validation_summary?.passed || 0}, warnings=${data.validation_summary?.warnings || 0}, blocked=${data.validation_summary?.blocked || 0}.`
      );
      queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
      queryClient.invalidateQueries({ queryKey: ["job", data.batch_id] });
      queryClient.invalidateQueries({ queryKey: ["preview", data.batch_id] });
    },
    onError: (error) => {
      appendActivity("error", error?.response?.data?.detail || error?.message || "Generate failed.");
    },
  });

  const integrationsQuery = useQuery({
    queryKey: ["integrations-status"],
    queryFn: getIntegrationsStatus,
    refetchInterval: 15000,
  });

  const jobsQuery = useQuery({
    queryKey: ["jobs-list"],
    queryFn: () => listJobs(40),
    refetchInterval: 10000,
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

  const previewQuery = useQuery({
    queryKey: ["preview", batchId],
    queryFn: () => getPreview(batchId),
    enabled: Boolean(batchId),
    refetchInterval: 5000,
  });

  const approveMutation = useMutation({
    mutationFn: approveBatch,
    onMutate: () => {
      appendActivity("info", "Saving approval decisions to Apps Script.");
    },
    onSuccess: (data) => {
      appendActivity(
        "success",
        `Approval saved. approved=${data?.summary?.approved || 0}, skipped=${data?.summary?.skipped || 0}, edit_later=${data?.summary?.edit_later || 0}.`
      );
      if (batchId) {
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
        queryClient.invalidateQueries({ queryKey: ["job", batchId] });
        queryClient.invalidateQueries({ queryKey: ["preview", batchId] });
      }
    },
    onError: (error) => {
      appendActivity("error", error?.response?.data?.detail || error?.message || "Approval save failed.");
    },
  });

  const deployMutation = useMutation({
    mutationFn: deployBatch,
    onMutate: () => {
      appendActivity("info", "Deploying approved records to live Zendesk instance.");
    },
    onSuccess: (data) => {
      appendActivity(
        data?.status === "deployed" ? "success" : "error",
        `Deploy finished. deployed=${data?.summary?.deployed || 0}, failed=${data?.summary?.failed || 0}, skipped=${data?.summary?.skipped || 0}.`
      );
      if (batchId) {
        queryClient.invalidateQueries({ queryKey: ["jobs-list"] });
        queryClient.invalidateQueries({ queryKey: ["job", batchId] });
        queryClient.invalidateQueries({ queryKey: ["preview", batchId] });
      }
    },
    onError: (error) => {
      appendActivity("error", error?.response?.data?.detail || error?.message || "Deploy failed.");
    },
  });

  const zendeskValidateMutation = useMutation({
    mutationFn: validateZendeskCredentials,
    onSuccess: (data, variables) => {
      const valid = Boolean(data?.ok);
      setZendeskValidated(valid);
      appendActivity(valid ? "success" : "error", valid ? "Zendesk session credentials validated." : (data?.detail || "Zendesk credential validation failed."));
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
    if (!bootZendeskSession) {
      return;
    }
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

  const buildDecisionPayload = () => {
    return Object.entries(decisions)
      .filter(([, importDecision]) => ["approved", "skipped", "edit_later"].includes(importDecision))
      .map(([record_id, import_decision]) => ({ record_id, import_decision }));
  };

  const buildReviewDecisionPayload = () => {
    return (previewRecords || []).map((row) => {
      const local = decisions[row.record_id];
      const current = local || row.import_decision || "pending_review";
      let resolved = current;
      if (current === "pending_review") {
        resolved = row.deployable ? "approved" : "skipped";
      }
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

  const submitPrompt = (value) => {
    if (!zendeskValidated) return;
    if (dependencyMode === "force_existing_only" && selectedRelatedObjects.length === 0) {
      appendActivity(
        "error",
        "Dependency mode requires existing context. Select one or more groups/forms/brands/sections first."
      );
      return;
    }
    const localHistory = [...chatHistory.slice(-10), `user: ${value}`];
    setChatHistory(localHistory);
    setPrompt("");
    const referenceCatalog = contextCatalog || {};
    const recentBatchContext = localHistory.slice(-8);
    generateMutation.mutate({
      prompt: value,
      target_environment: "sandbox",
      mode: "generate_validate_preview",
      requester: "local-user",
      dependency_mode: dependencyMode,
      related_objects: selectedRelatedObjects,
      reference_catalog: referenceCatalog,
      recent_batch_context: recentBatchContext,
      context_notes: selectedRelatedObjects.length
        ? `Selected context objects: ${selectedRelatedObjects.map((obj) => `${obj.object_type}:${obj.name}`).join(", ")}`
        : "No explicit object selections were made. Use available context catalog and recent in-session context.",
    });
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
  ]
    .filter(Boolean)
    .map((err) => err?.response?.data?.detail || err?.message);

  const generatedData = generateMutation.data;
  const effectiveGenerateMetadata = generatedData?.metadata || jobQuery.data?.metadata || {};
  const previewData = previewQuery.data;
  const historyItems = jobsQuery.data?.jobs || [];
  const contextCatalog = zendeskContextQuery.data?.catalogs || null;
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
    setPrompt("");
    setDecisions({});
    setTestResult(null);
    setActivityLogs([]);
    setClarificationQuestions([]);
    setChatHistory([]);
    setHistorySearch("");
    setSelectedContext({});
    setDependencyMode("match_existing_or_create_new");
    setOnExistingMode("create_new");
    lastContextSyncRef.current = "";
    lastContextErrorRef.current = "";
    resetFlow();
    resetZendeskValidation();
  };

  const startNewChat = () => {
    setDecisions({});
    setClarificationQuestions([]);
    setActivityLogs([]);
    setHistorySearch("");
    setPrompt("");
    setChatHistory([]);
    resetFlow();
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
        error?.response?.data?.detail
          || error?.message
          || "Could not save approval decisions before deploy."
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
        appendActivity("error", "No approved rows available to deploy.");
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
        error?.response?.data?.detail
          || error?.message
          || "Could not complete approve-and-deploy action."
      );
    }
  };

  const isWorking =
    generateMutation.isPending || approveMutation.isPending || deployMutation.isPending || jobQuery.isFetching;

  const currentPhaseLabel = (() => {
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
              onClick={() => setShowSettings((prev) => !prev)}
            >
              {showSettings ? "Hide Settings" : "Settings"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => testMutation.mutate()}
              disabled={testMutation.isPending}
            >
              {testMutation.isPending ? "Testing..." : "Test APIs"}
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

        <PromptComposer
          onSubmitPrompt={submitPrompt}
          isLoading={generateMutation.isPending}
          defaultPrompt={prompt}
          isLocked={!zendeskValidated}
          lockReason="Validate Zendesk credentials first in Integration Diagnostics."
          dependencyMode={dependencyMode}
          onDependencyModeChange={setDependencyMode}
          onExistingMode={onExistingMode}
          onOnExistingModeChange={setOnExistingMode}
          selectedContextCount={selectedRelatedObjects.length}
        />

        <StatusRibbon
          batchId={batchId}
          status={currentStatus}
          testResult={testResult}
          jobData={jobQuery.data}
          generateMetadata={effectiveGenerateMetadata}
          approvalResult={approveMutation.data}
          deployEnabled={Boolean(integrationsQuery.data?.zendesk?.deploy_endpoint_enabled)}
          deployTarget={zendeskValidationResult?.base_url || ""}
          onExistingMode={onExistingMode}
        />

        <Card className="mb-6 border-[#7B1FFF]/30 bg-[#120522]/70 cx-soft-glow">
          <CardContent className="p-4 text-sm text-slate-200">
            <div className="flex items-center justify-between">
              <p className="font-semibold">Pipeline Activity</p>
              {isWorking ? <span className="animate-pulse text-sky-300">Processing...</span> : <span className="text-slate-400">Ready</span>}
            </div>
            <p className="mt-2 text-slate-300">{currentPhaseLabel}</p>
          </CardContent>
        </Card>

        {showSettings ? (
          <div className="mb-6 space-y-4">
            <IntegrationPanel
              integrationsStatus={integrationsQuery.data}
              integrationsLoading={integrationsQuery.isLoading}
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
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => zendeskContextQuery.refetch()}
                      disabled={zendeskContextQuery.isFetching}
                    >
                      {zendeskContextQuery.isFetching ? "Refreshing..." : "Refresh"}
                    </Button>
                  </div>
                  {!contextCatalog ? (
                    <p className="text-xs text-slate-500">
                      Context will appear here after Zendesk session validation.
                    </p>
                  ) : (
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

        {clarificationQuestions.length > 0 ? (
          <Card className="mb-6 border-amber-700/50 bg-amber-950/20">
            <CardContent className="space-y-3 p-4 text-sm text-amber-100">
              <p className="font-semibold">More details needed before generation</p>
              {clarificationQuestions.map((item) => (
                <div key={item.id} className="rounded border border-amber-800/40 bg-amber-950/30 p-3">
                  <p>{item.question}</p>
                  <p className="mt-1 text-xs text-amber-300">{item.reason}</p>
                  {item.examples?.length ? (
                    <p className="mt-1 text-xs text-amber-200">
                      Examples: {item.examples.join(" | ")}
                    </p>
                  ) : null}
                </div>
              ))}
              <p className="text-xs text-amber-200">
                Add answers in your next prompt and submit again.
              </p>
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
