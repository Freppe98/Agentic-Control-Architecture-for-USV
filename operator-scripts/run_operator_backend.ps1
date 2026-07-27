# Launch the operator-station backend (FastAPI/uvicorn) on this laptop.
#
# Binds to 0.0.0.0 (all interfaces) by default: Scout's Local Agent posts status to
# this machine over the network, so the backend must be reachable from other hosts -
# not just localhost. Run from anywhere; it cd's to its own directory so `main:app`
# resolves. Nothing about the operator PC is hardcoded - see RUNBOOK.md for pointing
# Scout at whichever computer is running this.
#
# Prerequisites are installed by install_operator.ps1 (creates .venv, installs deps).
Set-Location -Path $PSScriptRoot

# --- Require the project virtual environment ---------------------------------------
# All Python runs through .venv\Scripts\python.exe so the station uses the pinned deps,
# never whatever happens to be on the machine's global Python.
$VenvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host ""
    Write-Host "[FAIL] Virtual environment not found (.venv\Scripts\python.exe is missing)." -ForegroundColor Red
    Write-Host "       Run the installer first, from this directory:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "           .\install_operator.ps1" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

# --- Configuration: port + host ----------------------------------------------------
# Defaults preserve the historical behaviour (0.0.0.0:8210). Optionally overridden by a
# local .env (created from .env.example by the installer) - a checkout without one
# behaves exactly as before. Read here in one place so the number is never duplicated.
$OperatorBackendPort = 8210
$OperatorBackendHost = '0.0.0.0'

$EnvFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $kv = $trimmed -split '=', 2
        if ($kv.Count -ne 2) { continue }
        $key = $kv[0].Trim()
        $val = $kv[1].Trim()
        switch ($key) {
            'OPERATOR_BACKEND_PORT' { if ($val) { $OperatorBackendPort = $val } }
            'OPERATOR_BACKEND_HOST' { if ($val) { $OperatorBackendHost = $val } }
        }
    }
}

# --- Show this PC's LAN addresses so you know what to set on Scout -------------------
# Scout's OPERATOR_URLS must point at one of these (whichever is on the same network
# as the vehicle/router). See RUNBOOK.md.
Write-Host ""
Write-Host "Operator backend -> binding ${OperatorBackendHost}:$OperatorBackendPort (reachable from the network)" -ForegroundColor Cyan
try {
    $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' } |
        Sort-Object InterfaceAlias
    if ($addrs) {
        Write-Host "This PC's addresses (set Scout OPERATOR_URLS to one of these):" -ForegroundColor Cyan
        foreach ($a in $addrs) {
            Write-Host ("   http://{0}:{1}   [{2}]" -f $a.IPAddress, $OperatorBackendPort, $a.InterfaceAlias)
        }
        Write-Host "On Scout, verify with:  curl http://<operator-ip>:$OperatorBackendPort/api/fleet/status" -ForegroundColor DarkGray
    } else {
        Write-Host "No non-local IPv4 address found. Run 'ipconfig' to find this PC's IP." -ForegroundColor Yellow
    }
} catch {
    # Get-NetIPAddress missing/blocked - fall back to the manual instruction.
    Write-Host "Could not enumerate addresses automatically. Run 'ipconfig' to find this PC's IPv4 address." -ForegroundColor Yellow
}
Write-Host ""

# --no-access-log: the operator backend is polled every 2-3s by the UI (per open page)
# and by Scout's status posts, so uvicorn's default per-request "GET/POST ... 200 OK"
# line floods the terminal. Startup/shutdown/error logging stays on (unaffected).
& $VenvPython -m uvicorn main:app --host $OperatorBackendHost --port $OperatorBackendPort --no-access-log
