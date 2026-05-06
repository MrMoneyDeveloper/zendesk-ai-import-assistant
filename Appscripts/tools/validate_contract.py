#!/usr/bin/env python
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FILE = ROOT / 'contracts' / 'fastapi_schema_bundle.json'

REQUIRED_MODELS = {
    'ImportAssistantGenerateRequest',
    'ImportAssistantGenerateResponse',
    'JobStatusResponse',
    'PreviewResponse',
    'ApprovalRequest',
    'ApprovalResponse',
    'AppScriptActionRequest',
    'AppScriptActionResponse',
}


def main() -> int:
    if not CONTRACT_FILE.exists():
        print(f'ERROR: Contract file not found: {CONTRACT_FILE}')
        return 1

    payload = json.loads(CONTRACT_FILE.read_text(encoding='utf-8'))
    models = set((payload.get('models') or {}).keys())
    missing = sorted(REQUIRED_MODELS - models)

    if missing:
        print('ERROR: Missing required models in contract bundle:')
        for model in missing:
            print(f'  - {model}')
        return 2

    print('Contract validation passed.')
    print(f'Models available: {len(models)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
