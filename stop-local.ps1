param(
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173
)

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

Stop-ListeningProcessByPort -Port $BackendPort
Stop-ListeningProcessByPort -Port $FrontendPort

Stop-ProcessByCommandPattern -Pattern "*uvicorn app.main:app*--port $BackendPort*"
Stop-ProcessByCommandPattern -Pattern "*vite*--port $FrontendPort*"

Write-Host "Stopped listeners on ports $BackendPort and $FrontendPort (if any)."
