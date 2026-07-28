# Stop the Operator Station backend cleanly.
#
# This script:
#   1. Reads the saved PID from .operator.pid.
#   2. Kills the process tree (the hidden powershell.exe running run_operator_backend.ps1,
#      which in turn runs python.exe / uvicorn).
#   3. Removes the PID file and shows a confirmation.
#
# Called via "Stop Operator Station.vbs" (which runs it via wscript -> powershell hidden).

Set-Location -Path $PSScriptRoot

$PidFile = Join-Path $PSScriptRoot '.operator.pid'

# --- Helper: show MessageBox (hidden window can't show text otherwise) ----
function Show-MessageBox($title, $message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($message, $title, [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
}

# --- Check if PID file exists ----
if (-not (Test-Path $PidFile)) {
    Show-MessageBox "Not Running" "The Operator Station backend is not currently running."
    exit 0
}

# --- Read the PID ----
$pidContent = (Get-Content $PidFile).Trim()
if (-not $pidContent -or -not ($pidContent -match '^\d+$')) {
    Show-MessageBox "Invalid PID" "Could not read a valid process ID from $PidFile. The server may not be running."
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

$pid = [int]$pidContent

# --- Check if the process is still alive ----
$proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
if ($null -eq $proc) {
    Show-MessageBox "Already Stopped" "The Operator Station backend is not running (process $pid not found)."
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

# --- Kill the process tree ----
# /T kills the tree (this powershell.exe and its child python.exe);
# /F forces termination (equivalent to -Force in PowerShell).
taskkill /PID $pid /T /F 2>&1 | Out-Null

# Wait a moment for the kill to complete.
Start-Sleep -Milliseconds 500

# --- Clean up the PID file ----
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue

Show-MessageBox "Stopped" "The Operator Station backend has been stopped."
exit 0
