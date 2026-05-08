param(
  [int]$BackendPort = 8016,
  [int]$FrontendPort = 5176,
  [switch]$DisableAutoPortFallback
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateFile = Join-Path $repoRoot ".local-dev-state.json"
$backendJob = $null
$frontendJob = $null
$activeBackendPort = $BackendPort
$activeFrontendPort = $FrontendPort
$autoPortFallback = -not $DisableAutoPortFallback

function Get-ListeningPidsByPort {
  param([int]$Port)

  $pidSet = New-Object System.Collections.Generic.HashSet[int]

  try {
    $connections = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($connections) {
      foreach ($conn in $connections) {
        [void]$pidSet.Add([int]$conn.OwningProcess)
      }
    }
  } catch {
  }

  try {
    $lines = netstat -ano -p tcp | Select-String -Pattern "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($line in $lines) {
      $match = [regex]::Match($line.ToString(), "LISTENING\s+(\d+)\s*$")
      if ($match.Success) {
        [void]$pidSet.Add([int]$match.Groups[1].Value)
      }
    }
  } catch {
  }

  return @($pidSet)
}

function Stop-ProcessTreeById {
  param([int]$ProcessId)

  if (-not $ProcessId -or $ProcessId -eq $PID) {
    return
  }

  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
  try {
    Start-Process -FilePath "taskkill.exe" -ArgumentList "/PID $ProcessId /T /F" -WindowStyle Hidden -Wait -ErrorAction SilentlyContinue | Out-Null
  } catch {
  }
}

function Stop-ListeningProcessByPort {
  param([int]$Port)

  for ($attempt = 1; $attempt -le 3; $attempt++) {
    $pids = Get-ListeningPidsByPort -Port $Port
    if (-not $pids -or $pids.Count -eq 0) {
      return
    }

    foreach ($procId in $pids) {
      Stop-ProcessTreeById -ProcessId $procId
    }

    Start-Sleep -Milliseconds 400
  }
}

function Stop-ProcessByCommandPattern {
  param([string]$Pattern)

  $matches = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like $Pattern }

  foreach ($proc in $matches) {
    if ($proc.ProcessId -and $proc.ProcessId -ne $PID) {
      Stop-ProcessTreeById -ProcessId $proc.ProcessId
    }
  }
}

function Is-PortFree {
  param([int]$Port)

  $remaining = Get-ListeningPidsByPort -Port $Port
  return (-not $remaining -or $remaining.Count -eq 0)
}

function Resolve-UsablePort {
  param(
    [int]$PreferredPort,
    [int[]]$Candidates,
    [string]$Label
  )

  Stop-ListeningProcessByPort -Port $PreferredPort
  if (Is-PortFree -Port $PreferredPort) {
    return $PreferredPort
  }

  if (-not $autoPortFallback) {
    $pids = (Get-ListeningPidsByPort -Port $PreferredPort) -join ","
    throw "$Label port $PreferredPort is in use (PID(s): $pids). Run .\stop-local.ps1 or select another port."
  }

  foreach ($candidate in ($Candidates | Select-Object -Unique)) {
    if ($candidate -eq $PreferredPort) {
      continue
    }

    Stop-ListeningProcessByPort -Port $candidate
    if (Is-PortFree -Port $candidate) {
      Write-Host "$Label port $PreferredPort is unavailable. Using fallback port $candidate."
      return $candidate
    }
  }

  throw "No available $Label port found. Tried: $($Candidates -join ', ')."
}

function Save-State {
  param(
    [int]$Backend,
    [int]$Frontend
  )

  $state = @{
    backend_port = $Backend
    frontend_port = $Frontend
    started_at = (Get-Date).ToString("o")
  }

  $state | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
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

  if ($activeBackendPort) {
    Stop-ListeningProcessByPort -Port $activeBackendPort
  }
  if ($activeFrontendPort) {
    Stop-ListeningProcessByPort -Port $activeFrontendPort
  }

  Stop-ProcessByCommandPattern -Pattern "*uvicorn app.main:app*--port $activeBackendPort*"
  Stop-ProcessByCommandPattern -Pattern "*vite*--port $activeFrontendPort*"

  if (Test-Path $stateFile) {
    Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
  }
}

function Wait-ForBackend {
  param(
    [int]$Port,
    [int]$TimeoutSeconds = 40
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

function Wait-ForListener {
  param(
    [int]$Port,
    [int]$TimeoutSeconds = 30
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (-not (Is-PortFree -Port $Port)) {
      return $true
    }
    Start-Sleep -Milliseconds 500
  }

  return $false
}

try {
  Write-Host "Cleaning existing listeners on preferred ports $BackendPort and $FrontendPort..."
  Stop-ListeningProcessByPort -Port $BackendPort
  Stop-ListeningProcessByPort -Port $FrontendPort
  Stop-ProcessByCommandPattern -Pattern "*uvicorn app.main:app*--port $BackendPort*"
  Stop-ProcessByCommandPattern -Pattern "*vite*--port $FrontendPort*"

  $activeBackendPort = Resolve-UsablePort -PreferredPort $BackendPort -Candidates @($BackendPort, 8016, 8000, 8020, 8080) -Label "Backend"
  $activeFrontendPort = Resolve-UsablePort -PreferredPort $FrontendPort -Candidates @($FrontendPort, 5176, 5173, 5180, 5273) -Label "Frontend"

  Save-State -Backend $activeBackendPort -Frontend $activeFrontendPort

  Write-Host "Starting backend on http://127.0.0.1:$activeBackendPort"
  $backendJob = Start-Job -Name "backend-dev" -ArgumentList $repoRoot, $activeBackendPort -ScriptBlock {
    param($root, $port)
    Set-Location (Join-Path $root "backend")
    python -m uvicorn app.main:app --host 127.0.0.1 --port $port --reload
  }

  if (-not (Wait-ForBackend -Port $activeBackendPort -TimeoutSeconds 40)) {
    throw "Backend did not become healthy on port $activeBackendPort."
  }

  Write-Host "Starting frontend on http://127.0.0.1:$activeFrontendPort"
  $frontendJob = Start-Job -Name "frontend-dev" -ScriptBlock {
    param($root, $port, $backendPort)
    Set-Location (Join-Path $root "frontend")
    $env:VITE_BACKEND_URL = "http://127.0.0.1:$backendPort"
    npm.cmd run dev -- --host 127.0.0.1 --port $port --strictPort
  } -ArgumentList $repoRoot, $activeFrontendPort, $activeBackendPort

  if (-not (Wait-ForListener -Port $activeFrontendPort -TimeoutSeconds 35)) {
    throw "Frontend did not start listening on port $activeFrontendPort."
  }

  Write-Host "Pipeline running. Backend: $activeBackendPort | Frontend: $activeFrontendPort"
  Write-Host "Press Ctrl+C to stop both services."

  while ($true) {
    Receive-Job -Job $backendJob -ErrorAction SilentlyContinue
    Receive-Job -Job $frontendJob -ErrorAction SilentlyContinue

    $backendListening = -not (Is-PortFree -Port $activeBackendPort)
    $frontendListening = -not (Is-PortFree -Port $activeFrontendPort)
    if (-not $backendListening) {
      throw "Backend listener on port $activeBackendPort stopped unexpectedly."
    }
    if (-not $frontendListening) {
      throw "Frontend listener on port $activeFrontendPort stopped unexpectedly."
    }

    Start-Sleep -Milliseconds 700
  }
}
finally {
  Cleanup
}
