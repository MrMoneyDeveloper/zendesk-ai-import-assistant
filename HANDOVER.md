# AI Zendesk Import Assistant — Technical Handover

Documentation review: 7 October 2026. Live deployment, secret inventory and transfer completion remain to be verified by the receiving owner.

## Scope and operational boundary

FastAPI backend, React frontend and Apps Script/Google Sheet staging worker. The operator workflow is prompt → generate → stage → validate → preview → approve → deploy.

See [README.md](README.md) for the operator/developer guide and [DEPENDENCIES.md](DEPENDENCIES.md) for setup and integration requirements.

## Setup and release handover

Use [DEPLOY_FREE_VERCEL_RENDER.md](DEPLOY_FREE_VERCEL_RENDER.md) and [LOCAL_SETUP.md](LOCAL_SETUP.md). Transfer workbook and script access, configure replacement provider and Zendesk credentials, redeploy the worker if needed, and update the backend URL/key pair. Match CORS to the exact frontend origin. Preserve the explicit preview/approval gate.

## Required credential and ownership transition

All API keys/tokens, OAuth client secrets and grants, service-account keys, webhook/shared secrets and deployment credentials actually used by this project must be inventoried, rotated or reissued under the receiving organization, configured and tested during handover. This document records requirements; it does not certify that rotation or ownership transfer has happened. Session-authenticated integrations require successor access and fresh authorization rather than an invented application secret. Public IDs, URLs and public keys are configuration, not confidential values.

1. Assign an accountable technical owner and backup. Record the live environment, release commit, host/project, deployment identity, billing owner, scheduled jobs and recovery contact in the private operations register.
2. Inventory every enabled integration, including fallback providers, CI/CD tokens, webhook senders and receivers. Record credential **names**, provider/project, scope, storage location and expiry; never record secret values in Git, screenshots or this document.
3. Grant successor access and preserve backups before changes. Create replacement credentials with the required least privilege and configure the company's secret store, backend environment or Apps Script Script Properties. Keep permanent credentials out of frontend bundles and Google Sheet cells.
4. For each OAuth integration in use, transfer/recreate the registered client under company ownership, confirm consent branding, scopes, authorized origins and exact redirect URIs; replace client secrets where applicable and obtain fresh grants under the receiving account. Test renewal if supported, or document expiry and reauthorization where it is not. Apps Script authorization must be renewed for the execution identity; inspect and recreate installable triggers under the receiving owner as needed.
5. Coordinate sender/receiver shared-secret changes and rebuild/redeploy consumers when configuration changes require it. Test in the correct sandbox or with a controlled non-destructive example before cutover. Do not rotate data-encryption keys by simply overwriting them: migrate protected data and retain recovery access until verified.
6. Verify the project-specific checks below and a scheduled cycle where applicable. Then revoke superseded keys/grants and departing-user access, confirm new access still works, and record non-secret evidence and completion date privately. If cutover fails, pause affected jobs and restore the last compatible deployment/configuration without restoring a compromised credential.

## Acceptance checklist

- [ ] Primary and backup owners accept the service, repository, hosting and billing responsibilities.
- [ ] Dependencies and actual live configuration reconciled with [DEPENDENCIES.md](DEPENDENCIES.md).
- [ ] All enabled API/OAuth/shared/deployment credentials replaced, configured and tested; inapplicable items marked with a reason.
- [ ] Source, data, configuration and recovery access are backed up; restore procedure checked.
- [ ] Successor can build/release and operate without the departing account.
- [ ] Old credentials/grants revoked after successful cutover; scheduled jobs observed where applicable.
- [ ] Outstanding issues, evidence and sign-off recorded in the private operations register.

## Project-specific verification

Check backend health, `/api/test-apis`, integrations status, schema sync and a small sandbox generate/validate/preview flow. Deploy only an approved sandbox example. Confirm logs redact keys and no provider credentials appear in browser configuration.

## Recovery and open risks

Do not restart during an active generation/deploy run. Preserve staged work and batch identifiers; inspect target state before retrying a partial deploy. Confirm the deployed worker version independently of repository source.
