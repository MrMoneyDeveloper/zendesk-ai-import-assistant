param(
  [int]$BackendPort = 8016,
  [int]$FrontendPort = 5176
)

$ErrorActionPreference = "SilentlyContinue"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateFile = Join-Path $repoRoot ".local-dev-state.json"
$perfSessionDir = ""

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

    Start-Sleep -Milliseconds 350
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

$portsToStop = New-Object System.Collections.Generic.List[int]
$portsToStop.Add($BackendPort)
$portsToStop.Add($FrontendPort)
$portsToStop.Add(8000)
$portsToStop.Add(8016)
$portsToStop.Add(5173)
$portsToStop.Add(5176)

if (Test-Path $stateFile) {
  try {
    $state = Get-Content $stateFile -Raw | ConvertFrom-Json
    if ($state.backend_port) { $portsToStop.Add([int]$state.backend_port) }
    if ($state.frontend_port) { $portsToStop.Add([int]$state.frontend_port) }
    $perfSessionDir = $state.perf_session_dir
  } catch {
  }
}

$uniquePorts = $portsToStop | Select-Object -Unique
foreach ($port in $uniquePorts) {
  Stop-ListeningProcessByPort -Port $port
}

Stop-ProcessByCommandPattern -Pattern "*uvicorn app.main:app*"
Stop-ProcessByCommandPattern -Pattern "*vite*--host 127.0.0.1*"

if (Test-Path $stateFile) {
  Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
}

Write-Host "Stopped listeners on known local dev ports (backend/frontend) and cleared pipeline state."
if ($perfSessionDir) {
  try {
    $summaryScript = Join-Path $repoRoot "backend\\summarize-perf-session.py"
    if (Test-Path $summaryScript) {
      python $summaryScript --session-dir $perfSessionDir | Out-Null
    }
  } catch {
  }
  Write-Host "Latest performance telemetry folder: $perfSessionDir"
}
