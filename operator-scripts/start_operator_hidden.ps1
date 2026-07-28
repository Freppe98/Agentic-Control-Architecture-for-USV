# Start the Operator Station backend silently in the background and open the browser.
#
# This script:
#   1. Checks if the server is already running (if so, just opens the browser).
#   2. Guards against missing venv (shows a MessageBox, doesn't hang).
#   3. Starts uvicorn via run_operator_backend.ps1 in a hidden child process.
#   4. Polls until the backend is ready (or timeout), then opens the browser.
#
# Called via "Start Operator Station.vbs" (which runs it via wscript -> powershell hidden).
# Target the same port as run_operator_backend.ps1 (read from .env if present).

Set-Location -Path $PSScriptRoot

# --- Configuration: port + host (match run_operator_backend.ps1 exactly) -----
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

# Local checks always use 127.0.0.1 (regardless of bind host).
$LocalCheckAddr = '127.0.0.1'
$BrowserUrl = "http://$LocalCheckAddr`:$OperatorBackendPort/app/"

# --- Helper: show MessageBox (hidden window can't show error text otherwise) ----
function Show-MessageBox($title, $message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($message, $title, [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
}

# --- Helper: test if the server is listening on $addr:$port ----
function Test-ServerReady($addr, $port, $timeoutSec = 1) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.ConnectAsync($addr, $port).Wait($timeoutSec * 1000) | Out-Null
        $result = $tcp.Connected
        $tcp.Dispose()
        return $result
    } catch {
        return $false
    }
}

# --- 1. Check if already running ----
if (Test-ServerReady $LocalCheckAddr $OperatorBackendPort) {
    # Server is up; just open the browser.
    Start-Process $BrowserUrl
    exit 0
}

# --- 2. Guard: venv must exist ----
$VenvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Show-MessageBox "Operator Station Setup Required" @"
Virtual environment not found.

Please run the installer first:
  .\install_operator.ps1
"@
    exit 1
}

# --- 3. Start the backend in a hidden child process ----
$RunScript = Join-Path $PSScriptRoot 'run_operator_backend.ps1'
$LogDir = Join-Path $PSScriptRoot 'logs'
# Start-Process rejects the same path for both streams, so stdout and stderr go to
# separate files. uvicorn logs to stderr, so operator.err.log is the interesting one.
$OutLogFile = Join-Path $LogDir 'operator.log'
$ErrLogFile = Join-Path $LogDir 'operator.err.log'
$PidFile = Join-Path $PSScriptRoot '.operator.pid'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

try {
    $proc = Start-Process powershell.exe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$RunScript`"") `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLogFile `
        -RedirectStandardError $ErrLogFile `
        -PassThru `
        -ErrorAction Stop
} catch {
    $proc = $null
    $startError = $_.Exception.Message
}

if ($null -eq $proc -or $proc.Id -le 0) {
    Show-MessageBox "Startup Failed" @"
Could not start the Operator Station backend.

$startError

Logs: $LogDir
"@
    exit 1
}

# Save PID for the stop script.
$proc.Id | Out-File -FilePath $PidFile -NoNewline -Encoding ASCII

# --- 4. Poll for readiness (up to ~20s) ----
$maxAttempts = 20
$attempt = 0
while ($attempt -lt $maxAttempts) {
    if (Test-ServerReady $LocalCheckAddr $OperatorBackendPort 1) {
        # Server is ready; open the browser and exit success.
        Start-Process $BrowserUrl
        exit 0
    }
    $attempt++
    Start-Sleep -Seconds 1
}

# Timeout: server didn't come up in time.
Show-MessageBox "Startup Timeout" @"
The Operator Station backend did not become ready within 20 seconds.

Check the logs:
  $ErrLogFile
  $OutLogFile
"@
exit 1
