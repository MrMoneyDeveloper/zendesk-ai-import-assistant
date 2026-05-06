param(
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendJob = $null
$frontendJob = $null

function Stop-ListeningProcessByPort {
  param([int]$Port)

  $connections = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
  if (-not $connections) {
    return
  }

  $pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($procId in $pids) {
    if ($procId -and $procId -ne $PID) {
      Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
  }
}

function Stop-ProcessByCommandPattern {
  param([string]$Pattern)

  $matches = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like $Pattern }

  foreach ($proc in $matches) {
    if ($proc.ProcessId -and $proc.ProcessId -ne $PID) {
      Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
  }
}

function Cleanup {
  if ($backendJob) {
    Stop-Job -Job $backendJob -ErrorAction SilentlyContinue
    Remove-Job -Job $backendJob -Force -ErrorAction SilentlyContinue
  }
  if ($frontendJob) {
    Stop-Job -Job $frontendJob -ErrorAction SilentlyContinue
    Remove-Job -Job $frontendJob -Force -ErrorAction SilentlyContinue
  }

  Stop-ListeningProcessByPort -Port $BackendPort
  Stop-ListeningProcessByPort -Port $FrontendPort
  Stop-ProcessByCommandPattern -Pattern "*uvicorn app.main:app*--port $BackendPort*"
  Stop-ProcessByCommandPattern -Pattern "*vite*--port $FrontendPort*"
}

function Wait-ForBackend {
  param(
    [int]$Port,
    [int]$TimeoutSeconds = 30
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
      if ($response.StatusCode -eq 200) {
        return $true
      }
    } catch {
    }
    Start-Sleep -Milliseconds 700
  }

  return $false
}

function Assert-PortIsFree {
  param(
    [int]$Port,
    [string]$Label
  )

  $remaining = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
  if ($remaining) {
    $owners = ($remaining | Select-Object -ExpandProperty OwningProcess -Unique) -join ","
    throw "$Label port $Port is still in use (PID(s): $owners). Run stop-local.ps1 or pick another port."
  }
}

try {
  Write-Host "Cleaning existing listeners on ports $BackendPort and $FrontendPort..."
  Stop-ListeningProcessByPort -Port $BackendPort
  Stop-ListeningProcessByPort -Port $FrontendPort
  Stop-ProcessByCommandPattern -Pattern "*uvicorn app.main:app*--port $BackendPort*"
  Stop-ProcessByCommandPattern -Pattern "*vite*--port $FrontendPort*"

  Assert-PortIsFree -Port $BackendPort -Label "Backend"
  Assert-PortIsFree -Port $FrontendPort -Label "Frontend"

  Write-Host "Starting backend on http://127.0.0.1:$BackendPort"
  $backendJob = Start-Job -Name "backend-dev" -ArgumentList $repoRoot, $BackendPort -ScriptBlock {
    param($root, $port)
    Set-Location (Join-Path $root "backend")
    python -m uvicorn app.main:app --host 127.0.0.1 --port $port --reload
  }

  if (-not (Wait-ForBackend -Port $BackendPort -TimeoutSeconds 35)) {
    throw "Backend did not become healthy on port $BackendPort."
  }

  Write-Host "Starting frontend on http://127.0.0.1:$FrontendPort"
  $frontendJob = Start-Job -Name "frontend-dev" -ScriptBlock {
    param($root, $port, $backendPort)
    Set-Location (Join-Path $root "frontend")
    $env:VITE_BACKEND_URL = "http://127.0.0.1:$backendPort"
    npm.cmd run dev -- --host 127.0.0.1 --port $port --strictPort
  } -ArgumentList $repoRoot, $FrontendPort, $BackendPort

  Write-Host "Pipeline running. Press Ctrl+C to stop both services."

  while ($true) {
    Receive-Job -Job $backendJob -Keep -ErrorAction SilentlyContinue
    Receive-Job -Job $frontendJob -Keep -ErrorAction SilentlyContinue

    if ($backendJob.State -notin @("Running", "NotStarted")) {
      throw "Backend job stopped unexpectedly with state '$($backendJob.State)'."
    }
    if ($frontendJob.State -notin @("Running", "NotStarted")) {
      throw "Frontend job stopped unexpectedly with state '$($frontendJob.State)'."
    }

    Start-Sleep -Milliseconds 700
  }
}
finally {
  Cleanup
}
