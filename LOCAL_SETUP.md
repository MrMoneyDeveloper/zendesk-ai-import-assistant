# Local Setup and Pipeline

## Config already wired in repo

- Apps Script Script ID: `1OPGR2Mgko9HkcIVZavv_1bWFEmlBCe6LEsl9WNCOZkOhRV-6lyLxXzCG`
- Apps Script Web App URL: `https://script.google.com/macros/s/AKfycbyH3PhAfX6FiDM91PUIGocXngSb_IDyrQOa5ERuAFF2osU0hZl_XL4hyHE98YL2kbtydA/exec`
- Google Sheet ID: `1rO40M82yloS6Cz_h_2RJCBQbXxNR9WMSOWB8bShaxWo`
- Backend defaults to Groq:
  - `XAI_BASE_URL=https://api.groq.com/openai/v1`
  - `XAI_MODEL=openai/gpt-oss-20b`

These are synced in:

- `backend/.env`
- `Appscripts/.env`
- `Appscripts/.env.local`
- `Appscripts/.clasp.json`

## Mandatory Apps Script deploy step

If backend health shows `getSchemaBundleInfo is not defined` or `syncSchemaBundle is not defined`, your web app is running stale code.

After any script update:

1. `cd Appscripts`
2. `cmd /c npx @google/clasp login` (first-time on this machine)
3. `cmd /c npx @google/clasp push`
4. Open Apps Script Editor.
5. `Deploy` -> `Manage deployments` -> edit Web App deployment.
6. In **Version**, select **New version**.
7. Save the deployment.

## Run local pipeline

From repo root:

```powershell
.\stop-local.cmd
.\run-local.cmd
```

Notes:

- `run-local.cmd` bypasses PowerShell execution policy automatically.
- Default ports are `8016` (backend) and `5176` (frontend).
- If preferred ports are blocked, the script auto-falls back to open ports.
- Closing the pipeline or running `stop-local.cmd` kills listeners and clears local run state.

## Verify integrations

```powershell
$backendPort = 8016  # replace with the backend port printed by run-local
Invoke-WebRequest "http://127.0.0.1:$backendPort/api/import-assistant/integrations/status" -UseBasicParsing | Select-Object -ExpandProperty Content
Invoke-WebRequest "http://127.0.0.1:$backendPort/api/import-assistant/appscript/sync-schemas" -Method POST -UseBasicParsing | Select-Object -ExpandProperty Content
Invoke-WebRequest "http://127.0.0.1:$backendPort/api/import-assistant/integrations/status" -UseBasicParsing | Select-Object -ExpandProperty Content
```

Expected:

- `appscript.health = ok`
- sync response `status = ok`

## First end-to-end test

1. Open the frontend URL printed by `run-local` (for example `http://127.0.0.1:5176`).
2. Validate Zendesk credentials on the gate screen.
3. Submit prompt.
4. Approve records in preview.
5. Click `Save Approval Decisions`.
6. Click `Deploy Approved to Zendesk`.
7. Check sheet tabs:
   - `Requests`
   - object tab (`Triggers`, `Macros`, `Views`, etc.)
   - `Validation Log`
   - `Approval Log`
   - `Execution Log`
