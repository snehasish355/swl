$ErrorActionPreference = "Stop"
$ports = @(8765, 9080)

# Shut down app instances cleanly so their existing console windows can exit.
foreach ($port in $ports) {
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/shutdown" -TimeoutSec 2 | Out-Null
    } catch {
        # Older helper versions do not expose the shutdown endpoint.
    }
}

Start-Sleep -Milliseconds 400
$listenerIds = @()
foreach ($port in $ports) {
    $listenerIds += Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess
}

# Older versions have no shutdown endpoint. Only stop Python listeners running server.py.
foreach ($processId in ($listenerIds | Sort-Object -Unique)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (!$process) { continue }
    if ($process.Name -match '^pythonw?\.exe$' -and $process.CommandLine -match '(?i)server\.py') {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    } else {
        throw "Port 9080 or the previous SWL port is occupied by another application (PID $processId). It was left running. Close it, then start this launcher again."
    }
}

Start-Sleep -Milliseconds 500

