# AI Zendesk Import Assistant — Dependencies and Configuration

Companion to [HANDOVER.md](HANDOVER.md). Based on repository documentation and configuration/source inspected on 7 October 2026; the live deployment must be reconciled before sign-off. Package manifests and lockfiles remain authoritative for exact transitive versions; this is the operational dependency list, not a frozen software bill of materials.

| Dependency | Required setup / configuration | Source or handover action |
| --- | --- | --- |
| Runtime/build | Python 3.11+; Node.js 20+; FastAPI, uvicorn, httpx, Google API/auth libraries; React/Vite | backend/requirements.txt; frontend/package.json and lockfile; run-local.cmd |
| Zendesk | ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN, ZENDESK_TARGET_ENVIRONMENT | Backend environment; receiving account must have appropriate sandbox/target access. |
| AI providers | GEMINI_API_KEY; XAI_API_KEY and configured SECONDARY/TERTIARY or WAVE3/WAVE4 keys; LLM_PROVIDER and provider/model configuration | backend/app/core/settings.py is authoritative. Groq-compatible mode uses the configured key slots; rotate every configured fallback as well as primary. |
| Apps Script | APPS_SCRIPT_WEB_APP_URL; APPS_SCRIPT_API_KEY; Appscripts/ worker and staging workbook | Rotate the shared key on backend and worker; transfer script execution identity, deployment, triggers and Google consent. |
| Optional direct Sheets access | GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEETS_SCOPE | Transfer GCP/service-account administration and Sheet grants; replace service-account private keys if this path is enabled. |
| Hosting/data | Render backend; Vercel frontend; FRONTEND_ORIGIN and VITE_API_BASE_URL; batch/conversation files | DEPLOY_FREE_VERCEL_RENDER.md and render.yaml; free-host files can be ephemeral. |

## API and OAuth completion requirements

For **every enabled API/OAuth integration**, record its accountable owner, provider/project, credential name, scopes, secret-store location, endpoint/redirect URI, expiry/renewal behavior and dependent consumers in the private operations register. Rotate/reissue all applicable keys, client secrets, tokens, grants and deployment credentials; configure each consumer; test the new identity; then revoke the superseded credentials. See the ordered procedure in [HANDOVER.md](HANDOVER.md).

Never put secret values in this file. If the live environment has additional integrations, add their non-secret dependency details before handover sign-off. Items absent from inspected source are unverified, not automatically unnecessary. This documentation update does not perform credential rotation or modify runtime settings.
