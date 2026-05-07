#!/usr/bin/env python
from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / 'backend'
CONTRACTS_DIR = REPO_ROOT / 'Appscripts' / 'contracts'

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models import schemas  # noqa: E402

MODEL_NAMES = [
    'GenerateRequest',
    'GenerateResponse',
    'ApiTestResponse',
    'ImportAssistantGenerateRequest',
    'ImportAssistantGenerateResponse',
    'JobStatusResponse',
    'PreviewRecord',
    'PreviewResponse',
    'ApprovalRequest',
    'ApprovalResponse',
    'AppScriptActionRequest',
    'AppScriptActionResponse',
    'IntegrationStatusResponse',
    'ZendeskCredentialValidationRequest',
    'ZendeskCredentialValidationResponse',
]

ROUTE_CONTRACTS = {
    'generate': {
        'method': 'POST',
        'path': '/api/import-assistant/generate',
        'request_model': 'ImportAssistantGenerateRequest',
        'response_model': 'ImportAssistantGenerateResponse',
    },
    'job_status': {
        'method': 'GET',
        'path': '/api/import-assistant/jobs/{batch_id}',
        'response_model': 'JobStatusResponse',
    },
    'preview': {
        'method': 'GET',
        'path': '/api/import-assistant/preview/{batch_id}',
        'response_model': 'PreviewResponse',
    },
    'approve': {
        'method': 'POST',
        'path': '/api/import-assistant/approve',
        'request_model': 'ApprovalRequest',
        'response_model': 'ApprovalResponse',
    },
    'integrations_status': {
        'method': 'GET',
        'path': '/api/import-assistant/integrations/status',
        'response_model': 'IntegrationStatusResponse',
    },
    'zendesk_validate': {
        'method': 'POST',
        'path': '/api/import-assistant/zendesk/validate',
        'request_model': 'ZendeskCredentialValidationRequest',
        'response_model': 'ZendeskCredentialValidationResponse',
    },
}


def build_bundle() -> dict:
    models = {}
    for name in MODEL_NAMES:
        model_type = getattr(schemas, name)
        models[name] = model_type.model_json_schema()

    return {
        'generated_at': datetime.now(UTC).isoformat(),
        'source': 'backend/app/models/schemas.py',
        'routes': ROUTE_CONTRACTS,
        'models': models,
    }


def main() -> int:
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle()
    out_file = CONTRACTS_DIR / 'fastapi_schema_bundle.json'
    out_file.write_text(json.dumps(bundle, indent=2), encoding='utf-8')
    print(f'Wrote schema bundle: {out_file}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
