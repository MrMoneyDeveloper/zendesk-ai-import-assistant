import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import PromptComposer from "./components/chat/PromptComposer";
import PreviewWorkspace from "./components/chat/PreviewWorkspace";
import Sidebar from "./components/chat/Sidebar";
import StatusRibbon from "./components/chat/StatusRibbon";
import IntegrationPanel from "./components/chat/IntegrationPanel";
import { Button } from "./components/ui/button";
import { Card, CardContent } from "./components/ui/card";
import {
  approveBatch,
  generateBatch,
  getIntegrationsStatus,
  getJob,
  getPreview,
  testApis,
  validateZendeskCredentials,
} from "./services/api";
import { useImportAssistantStore } from "./store/importAssistantStore";

function App() {
  const queryClient = useQueryClient();
  const [testResult, setTestResult] = useState(null);
  const [decisions, setDecisions] = useState({});

  const { batchId, setBatchId, activeTab, setActiveTab, prompt, setPrompt } = useImportAssistantStore();

  const testMutation = useMutation({
    mutationFn: testApis,
    onSuccess: (data) => setTestResult(data),
    onError: (err) => {
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail || err?.message || "API test failed.";
      setTestResult({ status: "error", detail: status ? `HTTP ${status}: ${detail}` : detail });
    },
  });

  const generateMutation = useMutation({
    mutationFn: generateBatch,
    onSuccess: (data) => {
      setBatchId(data.batch_id);
      queryClient.invalidateQueries({ queryKey: ["job", data.batch_id] });
      queryClient.invalidateQueries({ queryKey: ["preview", data.batch_id] });
    },
  });

  const integrationsQuery = useQuery({
    queryKey: ["integrations-status"],
    queryFn: getIntegrationsStatus,
    refetchInterval: 15000,
  });

  const jobQuery = useQuery({
    queryKey: ["job", batchId],
    queryFn: () => getJob(batchId),
    enabled: Boolean(batchId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status) return 2500;
      if (["preview_ready", "failed", "approved", "partially_approved"].includes(status)) {
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
    onSuccess: () => {
      if (batchId) {
        queryClient.invalidateQueries({ queryKey: ["job", batchId] });
        queryClient.invalidateQueries({ queryKey: ["preview", batchId] });
      }
    },
  });

  const zendeskValidateMutation = useMutation({
    mutationFn: validateZendeskCredentials,
  });

  const currentStatus = jobQuery.data?.status || generateMutation.data?.status || null;

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
    setPrompt(value);
    generateMutation.mutate({
      prompt: value,
      target_environment: "sandbox",
      mode: "generate_validate_preview",
      requester: "local-user",
    });
  };

  const errors = [
    generateMutation.error,
    jobQuery.error,
    previewQuery.error,
    approveMutation.error,
    integrationsQuery.error,
    zendeskValidateMutation.error,
  ]
    .filter(Boolean)
    .map((err) => err?.response?.data?.detail || err?.message);

  const generatedData = generateMutation.data;
  const previewData = previewQuery.data;

  return (
    <div className="min-h-screen bg-[#10131a] text-slate-100">
      <div className="flex min-h-screen">
        <Sidebar />
        <main className="flex-1 px-4 py-5 lg:px-10">
          <div className="mx-auto max-w-6xl">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-sm text-slate-500">AI Zendesk Import Assistant</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => testMutation.mutate()}
                disabled={testMutation.isPending}
              >
                {testMutation.isPending ? "Testing..." : "Test APIs"}
              </Button>
            </div>

            <StatusRibbon batchId={batchId} status={currentStatus} testResult={testResult} />
            <IntegrationPanel
              integrationsStatus={integrationsQuery.data}
              integrationsLoading={integrationsQuery.isLoading}
              generateMetadata={generatedData?.metadata}
              approvalMetadata={approveMutation.data?.metadata}
              onValidateZendesk={(payload) => zendeskValidateMutation.mutate(payload)}
              isValidatingZendesk={zendeskValidateMutation.isPending}
              zendeskValidationResult={zendeskValidateMutation.data}
            />

            <PromptComposer
              onSubmitPrompt={submitPrompt}
              isLoading={generateMutation.isPending}
              defaultPrompt={prompt}
            />

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
            />
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
