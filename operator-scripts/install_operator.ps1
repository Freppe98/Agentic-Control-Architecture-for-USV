# install_operator.ps1 - one-shot, repeatable Windows bootstrap for the Operator Station.
#
# A freshly cloned repo becomes runnable with:
#     Set-ExecutionPolicy -Scope Process Bypass
#     .\install_operator.ps1
#     .\run_operator_backend.ps1
#
# What it does (idempotent - safe to run again after pulling dependency changes):
#   1. verifies the toolchain is present and new enough (Git, Python, Node, npm);
#   2. creates a project-local .venv (only if one isn't already there);
#   3. installs the pinned Python deps from requirements.txt into that venv;
#   4. restores frontend/test deps with `npm ci` when a lockfile exists;
#   5. seeds .env from .env.example on first run (never overwrites an existing .env);
#   6. runs a bounded import smoke check inside the venv;
#   7. runs the backend + frontend test baselines (skip with -SkipTests).
#
# It installs NOTHING system-wide: if a prerequisite is missing it tells you what to
# install and exits non-zero. All Python work goes through .venv\Scripts\python.exe, so
# it never touches the machine's global site-packages.
#
# Usage:
#   .\install_operator.ps1              # full install + test baseline
#   .\install_operator.ps1 -SkipTests   # install only (faster; skips the suites)

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipTests
)

# 'Continue', not 'Stop': the installer drives native commands (python, pip, npm, the
# test runners) that legitimately write to stderr - pip download notes, unittest's own
# progress, harmless library DeprecationWarnings. Under 'Stop', PowerShell 5.1 turns any
# such stderr line into a terminating error and the install aborts on a non-error. Every
# native command below is instead checked explicitly via $LASTEXITCODE, and the cmdlet
# steps (venv/copy/job) are verified by their results, so nothing is silently skipped.
$ErrorActionPreference = 'Continue'

# Minimum supported toolchain versions. The station is developed and tested on
# Python 3.13 and Node 24; these floors are the oldest releases the pinned deps
# (numpy 2.2 / shapely 2.1 need Python >=3.10; node:test is stable from Node 18) and
# the code are known to work on.
$MinPythonMajor = 3
$MinPythonMinor = 11
$MinNodeMajor   = 18

# Everything is anchored to the script's own directory so the installer works no matter
# what the current directory is when it's launched.
$Root        = $PSScriptRoot
$VenvDir     = Join-Path $Root '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $Root 'requirements.txt'
$EnvExample  = Join-Path $Root '.env.example'
$EnvFile     = Join-Path $Root '.env'
$Lockfile    = Join-Path $Root 'package-lock.json'
$PackageJson = Join-Path $Root 'package.json'

# --- small output helpers ----------------------------------------------------------
function Write-Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    [OK]   $text" -ForegroundColor Green }
function Write-Info($text) { Write-Host "    $text" -ForegroundColor DarkGray }
function Write-Warn($text) { Write-Host "    [WARN] $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host ""
    Write-Host "[FAIL] $text" -ForegroundColor Red
    exit 1
}

Write-Host "Operator Station installer" -ForegroundColor White
Write-Info "root: $Root"

# --- 1. toolchain checks -----------------------------------------------------------
Write-Step "Checking prerequisites (Git, Python, Node, npm)"

# Git - needed to clone/pull; a working tree without it is a broken setup.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "Git was not found on PATH. Install Git for Windows from https://git-scm.com/download/win and reopen PowerShell."
}
Write-Ok ("git    $((git --version) -replace 'git version ','')")

# Python - try `python` then the `py` launcher. The Windows Store 'python' alias prints
# nothing usable for --version, so we parse the version and fall through if it doesn't match.
$PythonCmd = $null
$pyVer = $null
foreach ($cand in @('python', 'py')) {
    if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
    $out = (& $cand --version 2>&1) | Out-String
    if ($out -match 'Python\s+(\d+)\.(\d+)\.(\d+)') {
        $PythonCmd = $cand
        $pyVer = @{ Major = [int]$Matches[1]; Minor = [int]$Matches[2]; Patch = [int]$Matches[3] }
        break
    }
}
if (-not $PythonCmd) {
    Fail "Python was not found on PATH. Install Python $MinPythonMajor.$MinPythonMinor+ from https://www.python.org/downloads/windows/ and tick 'Add python.exe to PATH', then reopen PowerShell."
}
if ($pyVer.Major -lt $MinPythonMajor -or ($pyVer.Major -eq $MinPythonMajor -and $pyVer.Minor -lt $MinPythonMinor)) {
    Fail "Python $($pyVer.Major).$($pyVer.Minor).$($pyVer.Patch) is too old. This project needs Python $MinPythonMajor.$MinPythonMinor or newer (tested on 3.13)."
}
Write-Ok ("python $($pyVer.Major).$($pyVer.Minor).$($pyVer.Patch)  (via '$PythonCmd')")

# Node.
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Fail "Node.js was not found on PATH. Install the LTS build from https://nodejs.org/ (Node $MinNodeMajor+), then reopen PowerShell."
}
$nodeRaw = (node --version) 2>&1
if ($nodeRaw -match 'v(\d+)\.(\d+)\.(\d+)') {
    $nodeMajor = [int]$Matches[1]
    if ($nodeMajor -lt $MinNodeMajor) {
        Fail "Node $nodeRaw is too old. This project needs Node $MinNodeMajor or newer (tested on 24)."
    }
    Write-Ok "node   $nodeRaw"
} else {
    Fail "Could not read a version from 'node --version' (got: $nodeRaw)."
}

# npm.
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Fail "npm was not found on PATH. It ships with Node.js - reinstall Node from https://nodejs.org/ and reopen PowerShell."
}
Write-Ok "npm    $((npm --version) 2>&1)"

# --- 2. Python virtual environment -------------------------------------------------
Write-Step "Python virtual environment (.venv)"
if (Test-Path $VenvPython) {
    Write-Ok "reusing existing .venv (delete the .venv folder to rebuild from scratch)"
} else {
    Write-Info "creating .venv ..."
    & $PythonCmd -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Fail "Failed to create the virtual environment at $VenvDir."
    }
    Write-Ok "created .venv"
}

# --- 3. Python dependencies --------------------------------------------------------
Write-Step "Installing Python dependencies (requirements.txt)"
if (-not (Test-Path $Requirements)) {
    Fail "requirements.txt not found at $Requirements. This file is required to install the backend dependencies."
}
& $VenvPython -m pip install --disable-pip-version-check -r $Requirements
if ($LASTEXITCODE -ne 0) {
    Fail "pip failed to install the dependencies from requirements.txt. Check the output above (network access is required on first install)."
}
Write-Ok "Python dependencies installed into .venv"

# --- 4. Frontend / test dependencies ----------------------------------------------
Write-Step "Restoring frontend/test dependencies (npm)"
if (Test-Path $Lockfile) {
    npm ci
    if ($LASTEXITCODE -ne 0) {
        Fail "'npm ci' failed. Ensure package.json and package-lock.json are in sync, then re-run."
    }
    Write-Ok "npm ci complete"
} elseif (Test-Path $PackageJson) {
    Write-Warn "no package-lock.json found - running 'npm install' to create one"
    npm install
    if ($LASTEXITCODE -ne 0) { Fail "'npm install' failed." }
    Write-Ok "npm install complete (commit the generated package-lock.json)"
} else {
    Write-Warn "no package.json - skipping npm step"
}

# --- 5. local configuration (.env) -------------------------------------------------
Write-Step "Local configuration (.env)"
if (Test-Path $EnvFile) {
    Write-Ok ".env already present - left untouched"
} elseif (Test-Path $EnvExample) {
    Copy-Item -Path $EnvExample -Destination $EnvFile
    Write-Ok "created .env from .env.example (edit it to change the port/host)"
} else {
    Write-Warn ".env.example not found - skipping (the station runs on its built-in defaults)"
}

# --- 6. bounded import smoke check -------------------------------------------------
Write-Step "Smoke check (importing the backend inside .venv)"
# Run in a background job so a hang can't block the installer forever. The import pulls
# in main.py (which imports planning + mission_contract) plus the pinned third-party
# stack, so a broken/incomplete install surfaces here rather than at first launch.
# One-liner (no here-string) so the script stays parseable regardless of line endings.
$smokeMods = 'main mission_contract planning fastapi uvicorn requests httpx shapely pyproj numpy'
$smokeCode = "import importlib; [importlib.import_module(m) for m in '$smokeMods'.split()]; print('IMPORT-OK')"
$job = Start-Job -ScriptBlock {
    param($py, $dir, $code)
    Set-Location $dir
    & $py -c $code 2>&1
} -ArgumentList $VenvPython, $Root, $smokeCode

if (Wait-Job $job -Timeout 90) {
    $smokeOut = (Receive-Job $job) | Out-String
    Remove-Job $job
    if ($smokeOut -match 'IMPORT-OK') {
        Write-Ok "backend and dependencies import cleanly"
    } else {
        Write-Host $smokeOut -ForegroundColor Red
        Fail "Smoke import failed - the backend or one of its dependencies did not import."
    }
} else {
    Stop-Job $job; Remove-Job $job -Force
    Fail "Smoke import timed out after 90s (the import should be near-instant - investigate a hang in main.py or a broken dependency)."
}

# --- 7. test baselines (optional) --------------------------------------------------
if ($SkipTests) {
    Write-Step "Tests"
    Write-Info "skipped (-SkipTests). Run them later with:  .\install_operator.ps1  (without -SkipTests)"
} else {
    Write-Step "Backend tests (unittest / TestClient)"
    & $VenvPython -m unittest discover -s tests -p "test_*.py"
    if ($LASTEXITCODE -ne 0) { Fail "Backend test suite failed. See the output above." }
    Write-Ok "backend tests passed"

    Write-Step "Frontend tests (node --test)"
    npm test
    if ($LASTEXITCODE -ne 0) { Fail "Frontend test suite failed. See the output above." }
    Write-Ok "frontend tests passed"
}

# --- done --------------------------------------------------------------------------
Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "Launch the Operator Station with:" -ForegroundColor White
Write-Host ""
Write-Host "    .\run_operator_backend.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Info "Then open the UI at  http://127.0.0.1:8210/app/"
exit 0
