#!/usr/bin/env python
from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT / 'secrets'
ENV_TEMPLATE_FILE = ROOT / '.env'
ENV_FILE = ROOT / '.env.local'


def ensure_env_line(lines: list[str], key: str, value: str) -> list[str]:
    prefix = f'{key}='
    replaced = False
    out = []
    for line in lines:
        if line.startswith(prefix):
            out.append(f'{prefix}{value}')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'{prefix}{value}')
    return out


def main() -> int:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    api_key = 'as_' + secrets.token_urlsafe(36)

    key_file = SECRETS_DIR / 'apps_script_api_key.txt'
    key_file.write_text(api_key + '\n', encoding='utf-8')

    deploy_file = SECRETS_DIR / 'apps_script_deployment_url.txt'
    if not deploy_file.exists():
        deploy_file.write_text('PASTE_APPS_SCRIPT_EXEC_URL_HERE\n', encoding='utf-8')

    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding='utf-8').splitlines()
    elif ENV_TEMPLATE_FILE.exists():
        lines = ENV_TEMPLATE_FILE.read_text(encoding='utf-8').splitlines()

    lines = ensure_env_line(lines, 'APPS_SCRIPT_API_KEY', api_key)
    lines = ensure_env_line(lines, 'APPS_SCRIPT_WEB_APP_URL', '')
    lines = ensure_env_line(lines, 'APPS_SCRIPT_TIMEOUT_SECONDS', '20')
    ENV_FILE.write_text('\n'.join(lines).strip() + '\n', encoding='utf-8')

    print(f'Generated Apps Script API key at: {key_file}')
    print(f'Created deployment URL placeholder at: {deploy_file}')
    print(f'Updated env file: {ENV_FILE}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
