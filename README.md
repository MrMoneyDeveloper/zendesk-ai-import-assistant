# AI Zendesk Import Assistant

Natural-language Zendesk configuration assistant with a controlled pipeline:

`prompt -> generate -> stage -> validate -> preview -> approve -> deploy`

## Requirements

- Windows PowerShell
- Python 3.11+
- Node.js 20+
- `npm`

## Project Structure

- `backend/` FastAPI orchestration, AI/provider calls, validation, deploy logic
- `frontend/` React UI (session gate, generation flow, preview, approvals, deploy actions)
- `Appscripts/` Google Apps Script worker for sheet staging, validation, logs
- `run-local.ps1`, `stop-local.ps1` local process pipeline scripts
- `run-local.cmd`, `stop-local.cmd` execution-policy-safe wrappers

## Run Locally

From repo root:

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
.\stop-local.cmd
.\run-local.cmd
```

What this does:

- starts backend (default preferred `8016`, auto-fallback if busy)
- starts frontend (default preferred `5176`, auto-fallback if busy)
- sets `VITE_BACKEND_URL` automatically
- cleans listeners when stopped

To stop:

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
.\stop-local.cmd
```

## First Test Flow

1. Open the frontend URL printed by `run-local`.
2. Validate Zendesk credentials on the gate screen.
3. (Optional) click `Test APIs`.
4. Submit a prompt.
5. Review/adjust row decisions in Preview Workspace.
6. Click `Save Approval Decisions`.
7. Click `Deploy Approved to Zendesk`.

## Debug / Troubleshooting

### 1) PowerShell says scripts are disabled

Use the `.cmd` wrappers (recommended):

```powershell
.\run-local.cmd
```

Or temporary bypass for current shell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run-local.ps1
```

### 2) Port already in use / app won’t start

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
.\stop-local.cmd
.\run-local.cmd
```

Check listeners manually:

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 8000,8016,5173,5176 }
```

### 3) I don’t know which ports are active

```powershell
Get-Content .local-dev-state.json | ConvertFrom-Json
```

### 4) Backend/API health checks

```powershell
$state = Get-Content .local-dev-state.json | ConvertFrom-Json
$bp = $state.backend_port

Invoke-WebRequest "http://127.0.0.1:$bp/" -UseBasicParsing | Select-Object -ExpandProperty Content
Invoke-WebRequest "http://127.0.0.1:$bp/api/test-apis" -UseBasicParsing | Select-Object -ExpandProperty Content
Invoke-WebRequest "http://127.0.0.1:$bp/api/import-assistant/integrations/status" -UseBasicParsing | Select-Object -ExpandProperty Content
```

### 5) Apps Script health is failing (`... is not defined`)

Usually stale deployment code. Re-push and redeploy:

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1\Appscripts
cmd /c npx @google/clasp push
```

Then in Apps Script UI:

1. `Deploy` -> `Manage deployments`
2. Edit web app deployment
3. Select **New version**
4. Save

Re-test:

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
$state = Get-Content .local-dev-state.json | ConvertFrom-Json
$bp = $state.backend_port

Invoke-WebRequest "http://127.0.0.1:$bp/api/import-assistant/appscript/sync-schemas" -Method POST -UseBasicParsing | Select-Object -ExpandProperty Content
Invoke-WebRequest "http://127.0.0.1:$bp/api/import-assistant/integrations/status" -UseBasicParsing | Select-Object -ExpandProperty Content
```

### 6) CORS / network error from frontend

Confirm backend is running and frontend is pointing to it:

```powershell
$state = Get-Content .local-dev-state.json | ConvertFrom-Json
"Backend: $($state.backend_port) | Frontend: $($state.frontend_port)"
Invoke-WebRequest "http://127.0.0.1:$($state.backend_port)/api/test-apis" -UseBasicParsing | Select-Object -ExpandProperty Content
```

If backend responds but browser still fails, restart both with `stop-local.cmd` then `run-local.cmd`.

### 7) LLM/API key errors

Common failures:

- `Incorrect API key provided`
- `400 Bad Request` from provider endpoint

Check backend env values:

```powershell
Get-Content .\backend\.env
```

Then restart pipeline:

```powershell
.\stop-local.cmd
.\run-local.cmd
```

## Notes

- Keep secrets in env files only; do not hardcode API keys in frontend.
- This flow is sandbox-first for Zendesk deploy operations.
- Apps Script and backend schema sync should remain healthy before generation/approval/deploy.
