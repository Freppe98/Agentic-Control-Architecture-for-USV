# Launch the operator-station backend (FastAPI/uvicorn) on 0.0.0.0:8200.
#
# Binds to 0.0.0.0 (all interfaces) on purpose: Scout's Local Agent posts status to
# this machine over the network, so the backend must be reachable from other hosts —
# not just localhost. Run from anywhere; it cd's to its own directory so `main:app`
# resolves. Nothing about the operator PC is hardcoded — see RUNBOOK.md for pointing
# Scout at whichever computer is running this.
Set-Location -Path $PSScriptRoot

$port = 8200

# --- Show this PC's LAN addresses so you know what to set on Scout -------------------
# Scout's OPERATOR_URLS must point at one of these (whichever is on the same network
# as the vehicle/router). See RUNBOOK.md.
Write-Host ""
Write-Host "Operator backend -> binding 0.0.0.0:$port (reachable from the network)" -ForegroundColor Cyan
try {
    $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' } |
        Sort-Object InterfaceAlias
    if ($addrs) {
        Write-Host "This PC's addresses (set Scout OPERATOR_URLS to one of these):" -ForegroundColor Cyan
        foreach ($a in $addrs) {
            Write-Host ("   http://{0}:{1}   [{2}]" -f $a.IPAddress, $port, $a.InterfaceAlias)
        }
        Write-Host "On Scout, verify with:  curl http://<operator-ip>:$port/api/fleet/status" -ForegroundColor DarkGray
    } else {
        Write-Host "No non-local IPv4 address found. Run 'ipconfig' to find this PC's IP." -ForegroundColor Yellow
    }
} catch {
    # Get-NetIPAddress missing/blocked — fall back to the manual instruction.
    Write-Host "Could not enumerate addresses automatically. Run 'ipconfig' to find this PC's IPv4 address." -ForegroundColor Yellow
}
Write-Host ""

python -m uvicorn main:app --host 0.0.0.0 --port $port
