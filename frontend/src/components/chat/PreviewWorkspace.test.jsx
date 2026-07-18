import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import PreviewWorkspace from "./PreviewWorkspace";

const helpCenterRecords = [
  {
    record_id: "REC-CATEGORY",
    object_type: "categories",
    title: "Claims Support",
    validation_status: "passed",
    import_decision: "approved",
    deployment_status: "pending",
    deployable: true,
    warnings: [],
  },
  {
    record_id: "REC-SECTION",
    object_type: "sections",
    title: "Claims",
    validation_status: "passed",
    import_decision: "approved",
    deployment_status: "pending",
    deployable: true,
    warnings: [],
  },
  {
    record_id: "REC-ARTICLE",
    object_type: "articles",
    title: "Submit a Claim",
    validation_status: "passed",
    import_decision: "approved",
    deployment_status: "pending",
    deployable: true,
    warnings: [],
  },
];

test("renders an exact update as a readable before-and-after comparison", () => {
  render(
    <PreviewWorkspace
      previewData={{
        records: [
          {
            record_id: "REC-UPDATE-1",
            object_type: "triggers",
            title: "Route Claims",
            operation_mode: "update",
            target_zendesk_id: "44",
            validation_status: "passed",
            import_decision: "pending_review",
            deployment_status: "pending",
            deployable: true,
            warnings: [],
            change_summary: [{ field: "actions", label: "Actions", changed: true }],
            before_configuration: {
              active: true,
              conditions: [{ scope: "all", field: "status", operator: "is", value: "new" }],
              actions: [{ field: "group_id", value: "Claims" }],
            },
            after_configuration: {
              active: true,
              conditions: [{ scope: "all", field: "status", operator: "is", value: "new" }],
              actions: [
                { field: "group_id", value: "Claims" },
                { field: "add_tags", value: "vip_claim" },
              ],
            },
          },
        ],
        planning_summary: { object_type: "triggers", operation_mode: "update" },
        generated_counts: { triggers: 1 },
        validation_summary: { passed: 1, warnings: 0, blocked: 0 },
      }}
      generatedData={null}
      decisions={{}}
      onDecisionChange={vi.fn()}
      onApproveDecisions={vi.fn()}
      onApproveAndDeploy={vi.fn()}
      isApproving={false}
      onDeployToZendesk={vi.fn()}
      isDeploying={false}
      deployResult={null}
      deploymentMetadata={{}}
    />
  );

  expect(screen.getByText("Review exact update")).toBeInTheDocument();
  expect(screen.getByText("Current in Zendesk")).toBeInTheDocument();
  expect(screen.getByText("Proposed final state")).toBeInTheDocument();
  expect(screen.getByText("Actions changed")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Apply Approved Update" })).toBeInTheDocument();
  expect(screen.getAllByText("Status").length).toBeGreaterThan(0);
  expect(screen.getByText("vip claim")).toBeInTheDocument();
});

test("shows the verified Help Center deployment phase after Support deployment", () => {
  const onVerify = vi.fn();
  const onDeploy = vi.fn();
  const onUrlChange = vi.fn();
  render(
    <PreviewWorkspace
      previewData={{
        records: helpCenterRecords,
        planning_summary: { object_type: "operating_model" },
        generated_counts: { categories: 1, sections: 1, articles: 1 },
        validation_summary: { passed: 3, warnings: 0, blocked: 0 },
      }}
      generatedData={null}
      decisions={{}}
      onDecisionChange={vi.fn()}
      onApproveDecisions={vi.fn()}
      onApproveAndDeploy={vi.fn()}
      isApproving={false}
      onDeployToZendesk={vi.fn()}
      isDeploying={false}
      deployResult={null}
      deploymentMetadata={{ phases: { support: { state: "deployed_partial" } } }}
      helpCenterUrl="https://acme.zendesk.com/hc/en-us"
      onHelpCenterUrlChange={onUrlChange}
      helpCenterReadiness={{
        ready: true,
        state: "ready",
        detail: "Help Center is ready.",
        locale: "en-us",
        brand: { id: "7", name: "Main brand" },
        checks: [{ name: "guide_api", detail: "Authenticated Help Center API is available." }],
      }}
      onVerifyHelpCenter={onVerify}
      isVerifyingHelpCenter={false}
      helpCenterArticleMode="draft"
      onHelpCenterArticleModeChange={vi.fn()}
      onDeployHelpCenter={onDeploy}
    />
  );

  expect(screen.getByText("Create Help Center content?")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: /Create the approved Help Center hierarchy/i }));
  fireEvent.click(screen.getByRole("button", { name: "Verify" }));
  fireEvent.click(screen.getByRole("button", { name: "Create hierarchy and drafts" }));

  expect(onVerify).toHaveBeenCalledOnce();
  expect(onDeploy).toHaveBeenCalledOnce();
});
