param(
  [string]$BaselineSession = "",
  [switch]$PurgeAll
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$liveRunsRoot = Join-Path $repoRoot "backend\\data\\live-prompt-runs"
$perfRoot = Join-Path $repoRoot "backend\\data\\perf-sessions"
$baselineRoot = Join-Path $repoRoot "backend\\data\\baselines\\generate-preview"

function Resolve-BaselineRun {
  param([string]$Root, [string]$Requested)
  if (-not (Test-Path $Root)) {
    return $null
  }
  $runs = Get-ChildItem -Path $Root -Directory | Sort-Object LastWriteTime -Descending
  if ($Requested) {
    $match = $runs | Where-Object { $_.Name -eq $Requested } | Select-Object -First 1
    return $match
  }

  foreach ($run in $runs) {
    $summaryPath = Join-Path $run.FullName "summary.json"
    $resultsPath = Join-Path $run.FullName "results.csv"
    if (-not (Test-Path $summaryPath) -or -not (Test-Path $resultsPath)) {
      continue
    }
    try {
      $summary = Get-Content $summaryPath -Raw | ConvertFrom-Json
      if ([double]$summary.success_rate_pct -ge 95) {
        return $run
      }
    } catch {
    }
  }

  return $runs | Select-Object -First 1
}

New-Item -ItemType Directory -Path $baselineRoot -Force | Out-Null

$baselineRun = Resolve-BaselineRun -Root $liveRunsRoot -Requested $BaselineSession
if (-not $baselineRun) {
  throw "No live prompt runs found in $liveRunsRoot."
}

$baselineTarget = Join-Path $baselineRoot $baselineRun.Name
if (Test-Path $baselineTarget) {
  Remove-Item -LiteralPath $baselineTarget -Recurse -Force
}
New-Item -ItemType Directory -Path $baselineTarget -Force | Out-Null

$sourceSummary = Join-Path $baselineRun.FullName "summary.json"
$sourceResults = Join-Path $baselineRun.FullName "results.csv"
if (-not (Test-Path $sourceSummary) -or -not (Test-Path $sourceResults)) {
  throw "Selected baseline run is missing summary/results files: $($baselineRun.FullName)"
}

Copy-Item -LiteralPath $sourceSummary -Destination (Join-Path $baselineTarget "summary.json") -Force
Copy-Item -LiteralPath $sourceResults -Destination (Join-Path $baselineTarget "results.csv") -Force

$manifest = @{
  baseline_session = $baselineRun.Name
  selected_at = (Get-Date).ToString("o")
  source_dir = $baselineRun.FullName
} | ConvertTo-Json -Depth 3
$manifest | Set-Content -Path (Join-Path $baselineRoot "latest.json") -Encoding UTF8

Write-Host "Preserved baseline run: $($baselineRun.Name)"

if ($PurgeAll) {
  if (Test-Path $liveRunsRoot) {
    Get-ChildItem -Path $liveRunsRoot -Directory | ForEach-Object {
      Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
  }
  if (Test-Path $perfRoot) {
    Get-ChildItem -Path $perfRoot -Directory | ForEach-Object {
      Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
  }
  $batchStore = Join-Path $repoRoot "backend\\data\\batches.json"
  if (Test-Path $batchStore) {
    Remove-Item -LiteralPath $batchStore -Force
  }
  Write-Host "Purged runtime-generated data (live runs, perf sessions, batches.json)."
}
