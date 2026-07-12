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
- runs in **Lean Prod** mode by default (diagnostics/perf capture off)

To run with diagnostics/perf capture enabled:

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
.\stop-local.cmd
.\run-local.cmd -Diagnostics
```

To stop:

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
.\stop-local.cmd
```

Diagnostics telemetry output (when `-Diagnostics` is enabled) is stored in:

- `backend/data/perf-sessions/<timestamp>/events.jsonl`
- `backend/data/perf-sessions/<timestamp>/summary.csv`
- `backend/data/perf-sessions/<timestamp>/bottlenecks_top30.csv`
- `backend/data/perf-sessions/<timestamp>/session_verdict.json`

The active runtime ports and optional telemetry path are written to `.local-dev-state.json`.

## First Test Flow

1. Open the frontend URL printed by `run-local`.
2. Validate Zendesk credentials on the gate screen.
3. Submit prompt(s) and review Preview Workspace.
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

### 8) Run the 30-prompt quality suite (Generate→Preview)

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1\backend
python .\run-generate-preview-suite.py --diagnostics --rerun-on-rate-limit --pace-seconds 2 --rerun-wait-seconds 45
```

Outputs:

- `backend/data/live-prompt-runs/<timestamp>/results.csv`
- `backend/data/live-prompt-runs/<timestamp>/summary.json`

### 9) Preserve baseline + purge runtime data

```powershell
cd C:\Workspace\zendesk-ai-import-assistant-1
powershell -NoProfile -ExecutionPolicy Bypass -File .\cleanup-runtime-data.ps1 -BaselineSession <session-id> -PurgeAll
```

If `-BaselineSession` is omitted, the script auto-selects the latest passing run (`>=95%`) when available.

## Notes

- Keep secrets in env files only; do not hardcode API keys in frontend.
- This flow is sandbox-first for Zendesk deploy operations.
- Apps Script and backend schema sync should remain healthy before generation/approval/deploy.

## LLM Routing and Reliability

Set these in `backend/.env` to tune model behavior:

- `LLM_MODEL_PLANNER` task model for planning/classification
- `LLM_MODEL_GENERATOR` task model for record generation
- `LLM_MODEL_CLARIFIER` task model for clarification follow-ups
- `LLM_STRICT_SCHEMA_MODE=true|false` enable JSON-schema constrained output
- `LLM_JSON_SCHEMA_SUPPORTED_MODELS=openai/gpt-oss-20b,grok-4.3` models allowed to use `json_schema` response format
- `LLM_FALLBACK_TO_JSON_OBJECT=true|false` fallback if strict schema is rejected
- `LLM_CIRCUIT_BREAKER_ENABLED=true|false`
- `LLM_CIRCUIT_BREAKER_FAILURES=2`
- `LLM_CIRCUIT_BREAKER_WINDOW_SECONDS=300`
- `LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS=300`
- `LLM_PREWAIT_MAX_SECONDS_PLANNER=6`
- `LLM_PREWAIT_MAX_SECONDS_GENERATOR=12`
- `LLM_AMBIGUITY_THRESHOLD=0.58` score above this triggers clarification flow
- `LLM_PLANNER_MAX_OUTPUT_TOKENS=900`
- `LLM_GENERATOR_MAX_OUTPUT_TOKENS=1800`
- `LLM_CLARIFIER_MAX_OUTPUT_TOKENS=700`
- `GEMINI_SUPERVISOR_ENABLED=true` enable the Gemini review/patch lane
- `GEMINI_API_KEY=...` Gemini key, stored only in local env files such as `backend/.env.local`
- `GEMINI_SUPERVISOR_MODEL=gemini-3.1-flash-lite`
- `GEMINI_SUPERVISOR_MAX_CONCURRENCY=2`
- `GEMINI_SUPERVISOR_MIN_REQUEST_INTERVAL_SECONDS=4.2` space request starts for free-tier quotas while retaining two in-flight review lanes
- `GEMINI_SUPERVISOR_RATE_LIMIT_RETRIES=3` honor provider retry delays before falling back to deterministic gates
- `GEMINI_SUPERVISOR_AUTO_APPLY_PATCHES=true`
- `GEMINI_SUPERVISOR_STRICT_MODE=false` keep generation alive if Gemini times out or rate-limits
- `GEMINI_SUPERVISOR_APPROVAL_THRESHOLD=0.80` minimum effective score after deterministic gates
- `GEMINI_SUPERVISOR_MAX_REGENERATION_RETRIES=1` retry only failed source chunks once
- `GEMINI_SUPERVISOR_REVIEW_GROUPING=department` consolidate heavy operating-model reviews by topic/department
- `APPS_SCRIPT_HEALTH_TIMEOUT_SECONDS=3`
- `INTEGRATIONS_HEALTH_CACHE_SECONDS=45`
- `ZENDESK_FALLBACK_404_COOLDOWN_SECONDS=1800`

Default behavior is safe for free-tier PoC usage: inference-first generation, single-item planner bypass, compatibility-first single-item generator, multi-item chunking, Gemini safe-patch supervision when configured, strict deploy safety, and lean metadata retention.
