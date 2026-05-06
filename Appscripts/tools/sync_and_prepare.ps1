param(
  [switch]$GenerateSecret
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

Write-Host 'Syncing FastAPI schema bundle...'
python "$PSScriptRoot\sync_fastapi_schemas.py"

Write-Host 'Validating generated contract...'
python "$PSScriptRoot\validate_contract.py"

if ($GenerateSecret) {
  Write-Host 'Generating local Apps Script API key...'
  python "$PSScriptRoot\generate_local_secret.py"
}

Write-Host 'Generating Excel template...'
python "$PSScriptRoot\generate_excel_template.py"

Write-Host 'Appscripts prep complete.'
