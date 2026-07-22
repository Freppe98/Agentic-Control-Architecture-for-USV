# check_operator_baseline.ps1 — one command that answers "is the Operator Station a
# stable baseline for the next end-to-end feature (obstacle injection → mission revision
# → auto-refresh)?". Deliberately small: it runs the checks + focused suites that already
# exist and aggregates them into a single BASELINE PASS / BASELINE FAIL. It is NOT a CI
# framework and starts no long-running server.
#
# Three phases:
#   1. Runtime checks   scripts/_baseline_checks.py — backend import, duplicate-route /
#                       obsolete-endpoint check, read-only fleet + mission endpoint checks.
#   2. Backend tests    the focused unittest suite (TestClient) under tests/.
#   3. Frontend tests   the focused node:test suite (npm test → node --test).
#
# Usage:   ./scripts/check_operator_baseline.ps1
# Exit:    0 on BASELINE PASS, 1 on BASELINE FAIL (usable from another script/CI later).

$ErrorActionPreference = "Continue"
# Run from the operator-scripts directory (this script's parent) so `main`, `tests/` and
# package.json all resolve no matter where the caller invoked it from.
$OpDir = Split-Path -Parent $PSScriptRoot
Set-Location -Path $OpDir

$results = [ordered]@{}

function Write-Head($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}

# --- Phase 1: runtime checks -----------------------------------------------------
Write-Head "1/3  Runtime checks (import, routes, endpoints)"
python scripts/_baseline_checks.py
$results["Runtime checks"] = ($LASTEXITCODE -eq 0)

# --- Phase 2: focused backend tests ----------------------------------------------
Write-Head "2/3  Backend tests (unittest / TestClient)"
python -m unittest discover -s tests -p "test_*.py"
$results["Backend tests"] = ($LASTEXITCODE -eq 0)

# --- Phase 3: focused frontend tests ---------------------------------------------
Write-Head "3/3  Frontend tests (node --test)"
npm test
$results["Frontend tests"] = ($LASTEXITCODE -eq 0)

# --- Aggregate -------------------------------------------------------------------
Write-Head "Baseline summary"
$allPass = $true
foreach ($k in $results.Keys) {
    if ($results[$k]) {
        Write-Host ("  PASS  {0}" -f $k) -ForegroundColor Green
    } else {
        Write-Host ("  FAIL  {0}" -f $k) -ForegroundColor Red
        $allPass = $false
    }
}
Write-Host ""
if ($allPass) {
    Write-Host "BASELINE PASS" -ForegroundColor Green
    exit 0
} else {
    Write-Host "BASELINE FAIL" -ForegroundColor Red
    exit 1
}
