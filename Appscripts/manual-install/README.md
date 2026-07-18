# Manual Apps Script Upgrade

This upgrade is additive for the spreadsheet and does not rotate keys, clear tabs, delete rows from other
batches, or deploy anything to Zendesk.

## Files to copy

| Order | Apps Script editor action | Repository source | Why |
| --- | --- | --- | --- |
| 1 | Add a new file named `OperatingModel.gs` | `Appscripts/src/OperatingModel.gs` | Adds batch metadata, progress events, operational-state reads, and the safe installer. |
| 2 | Replace `Config.gs` | `Appscripts/src/Config.gs` | Registers groups, automations, categories, sections, articles, metadata, and progress tabs. |
| 3 | Replace `Utils.gs` | `Appscripts/src/Utils.gs` | Adds header-aware writes, request caching, and batch-scoped idempotent row replacement. |
| 4 | Replace `SheetWriter.gs` | `Appscripts/src/SheetWriter.gs` | Writes every operating-model type and preserves chunk, department, model, and record keys. |
| 5 | Replace `Validation.gs` | `Appscripts/src/Validation.gs` | Validates automations, fields, forms, sections, and article bodies instead of passing them silently. |
| 6 | Replace `Approval.gs` | `Appscripts/src/Approval.gs` | Prevents failed or blocked records from being approved. |
| 7 | Replace `Api.gs` | `Appscripts/src/Api.gs` | Exposes the combined pipeline, metadata/progress actions, and API-version health check. |
| 8 | Replace `ExecutionLog.gs` | `Appscripts/src/ExecutionLog.gs` | Recommended: reports only the latest deployment result per record. |

Do not replace `Secrets.gs`. Do not run `initializeFromScratchFromEditor()` and do not rotate the API key.
The four supplied `Validation.gs` attachments are identical, so only one replacement is required.

## Install safely

1. Leave the currently deployed Web App version unchanged while editing. It remains the rollback version.
2. Copy the files in the order above and save the Apps Script project.
3. Select and run `inspectOperatingModelUpgradeFromEditor`. Review `missing_tabs` and `missing_headers`.
4. Select and run `installOperatingModelUpgradeFromEditor` once. It creates missing tabs and appends missing
   headers only. Expected result: `ready: true`.
5. Run `inspectOperatingModelUpgradeFromEditor` again. Expected result: no missing tabs or headers.
6. Open **Deploy > Manage deployments**, edit the existing Web App, select **New version**, and deploy. Editing
   the source without this step leaves the old API active.
7. Open `<existing-web-app-url>?action=health`. Confirm:
   - `api_version` is `2026-07-18-operating-model-v2`;
   - `capabilities` includes `stage_validate_preview` and `write_batch_metadata`.
8. Restart the local backend after setting `APPS_SCRIPT_TIMEOUT_SECONDS=45`. The URL and API key do not need
   to change when the existing deployment is updated.

## Expected spreadsheet changes

New tabs may be created for `Brands`, `Groups`, `Automations`, `Categories`, `Sections`, `Articles`,
`Batch Metadata`, and `Progress Log`. Missing managed columns are appended to existing object tabs. Existing
rows, formulas, formatting, approvals, execution logs, and the `Integration Secrets` tab are retained.

## Rollback

In **Deploy > Manage deployments**, select the prior Web App version. No spreadsheet rollback is required:
the added tabs and columns are ignored by the prior code, and no existing data is removed during installation.
