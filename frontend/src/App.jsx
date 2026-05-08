import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import PromptComposer from "./components/chat/PromptComposer";
import PreviewWorkspace from "./components/chat/PreviewWorkspace";
import Sidebar from "./components/chat/Sidebar";
import StatusRibbon from "./components/chat/StatusRibbon";
import IntegrationPanel from "./components/chat/IntegrationPanel";
import ZendeskSessionGate from "./components/chat/ZendeskSessionGate";
import { Button } from "./components/ui/button";
import { Card, CardContent } from "./components/ui/card";
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
  const [selectedContext, setSelectedContext] = useState({});
  const [dependencyMode, setDependencyMode] = useState("match_existing_or_create_new");
  const [onExistingMode, setOnExistingMode] = useState("create_new");
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

  const { batchId, setBatchId, activeTab, setActiveTab, prompt, setPrompt, resetFlow } =
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

  const saveApproval = () => {
    if (!batchId) return;
    const records = Object.entries(decisions)
      .filter(([, import_decision]) => ["approved", "skipped", "edit_later"].includes(import_decision))
      .map(([record_id, import_decision]) => ({ record_id, import_decision }));

    if (!records.length) return;
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
    setPrompt(value);
    const referenceCatalog = contextCatalog || {};
    const recentBatchContext = (historyItems || []).slice(0, 8).map((item) => (
      `${item.batch_id} | ${item.status} | ${item.prompt_preview}`
    ));
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
        : "No explicit object selections were made. Use available context catalog and recent batch context.",
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
  const previewData = previewQuery.data;
  const historyItems = jobsQuery.data?.jobs || [];
  const contextCatalog = zendeskContextQuery.data?.catalogs || null;

  const validateZendeskSession = (credentials) => {
    setZendeskCredentials(credentials);
    mutateZendeskValidation(credentials);
  };

  const signOutZendeskSession = () => {
    clearZendeskSessionCredentials();
    setZendeskValidated(false);
    setZendeskCredentials({ subdomain: "", email: "", api_token: "" });
    setDecisions({});
    setTestResult(null);
    setActivityLogs([]);
    setHistorySearch("");
    setSelectedContext({});
    setDependencyMode("match_existing_or_create_new");
    setOnExistingMode("create_new");
    lastContextSyncRef.current = "";
    lastContextErrorRef.current = "";
    resetFlow();
    resetZendeskValidation();
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

  const deployToZendesk = () => {
    if (!batchId || !zendeskCredentials?.subdomain || !zendeskCredentials?.email || !zendeskCredentials?.api_token) {
      appendActivity("error", "Cannot deploy: missing validated Zendesk session credentials.");
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
    <div className="min-h-screen bg-[#10131a] text-slate-100">
      <div className="flex min-h-screen">
        <Sidebar
          productName="CX Experts Assistant"
          historyItems={historyItems}
          currentBatchId={batchId}
          historySearch={historySearch}
          onHistorySearchChange={setHistorySearch}
          onSelectBatch={selectHistoryBatch}
          contextCatalog={contextCatalog}
          contextLoading={zendeskContextQuery.isFetching}
          selectedContextKeys={selectedContext}
          onToggleContext={toggleContextSelection}
          onRefreshContext={() => zendeskContextQuery.refetch()}
        />
        <main className="flex-1 px-4 py-5 lg:px-10">
          <div className="mx-auto max-w-6xl">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-sm text-slate-500">CX Experts Assistant</span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={signOutZendeskSession}
                >
                  Sign Out Session
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => testMutation.mutate()}
                  disabled={testMutation.isPending}
                >
                  {testMutation.isPending ? "Testing..." : "Test APIs"}
                </Button>
              </div>
            </div>

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
              generateMetadata={generatedData?.metadata}
              approvalResult={approveMutation.data}
              deployEnabled={Boolean(integrationsQuery.data?.zendesk?.deploy_endpoint_enabled)}
              deployTarget={zendeskValidationResult?.base_url || ""}
              onExistingMode={onExistingMode}
            />
            <IntegrationPanel
              integrationsStatus={integrationsQuery.data}
              integrationsLoading={integrationsQuery.isLoading}
              generateMetadata={generatedData?.metadata}
              approvalMetadata={approveMutation.data?.metadata}
              onValidateZendesk={(payload) => mutateZendeskValidation(payload)}
              isValidatingZendesk={zendeskValidationPending}
              zendeskValidationResult={zendeskValidationResult}
              zendeskValidated={zendeskValidated}
              contextStatus={zendeskContextQuery.data}
              contextLoading={zendeskContextQuery.isFetching}
              selectedContextCount={selectedRelatedObjects.length}
            />

            {activityLogs.length > 0 ? (
              <Card className="mb-6 border-slate-800 bg-slate-900/80">
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

            <Card className="mb-6 border-slate-800 bg-slate-900/80">
              <CardContent className="p-4 text-sm text-slate-200">
                <div className="flex items-center justify-between">
                  <p className="font-semibold">Pipeline Activity</p>
                  {isWorking ? <span className="animate-pulse text-sky-300">Processing...</span> : <span className="text-slate-400">Ready</span>}
                </div>
                <p className="mt-2 text-slate-300">{currentPhaseLabel}</p>
                <p className="mt-2 text-xs text-slate-400">
                  Dependency mode: {dependencyMode} | Existing object behavior: {onExistingMode} | Selected context objects: {selectedRelatedObjects.length}
                </p>
              </CardContent>
            </Card>

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
              activeTab={activeTab}
              onActiveTabChange={setActiveTab}
              decisions={decisions}
              onDecisionChange={(recordId, decision) =>
                setDecisions((prev) => ({ ...prev, [recordId]: decision }))
              }
              onApproveDecisions={saveApproval}
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
