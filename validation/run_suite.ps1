# PowerShell runner for the h2s bot master validation suite.
# Usage:
#   ./validation/run_suite.ps1                  (runs the full automated suite)
#   ./validation/run_suite.ps1 -Tag v1.2.3      (archives the report under that tag)
param(
    [string]$Tag = ("dev-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not $env:TELEGRAM_BOT_TOKEN) {
    $env:TELEGRAM_BOT_TOKEN = "test-harness-token"
}
$env:LOG_LEVEL = "DEBUG"
$env:ALERT_PACING_SECONDS = "0"

$reportDir = Join-Path $ProjectRoot "validation/reports"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$reportFile = Join-Path $reportDir ("pytest_report_{0}.txt" -f $Tag)

Write-Host "Running automated validation suite for release tag: $Tag"
Write-Host "Report: $reportFile"
Write-Host "---"

pytest tests/ -v --tb=short 2>&1 | Tee-Object -FilePath $reportFile
$exit = $LASTEXITCODE

Write-Host "---"
if ($exit -eq 0) {
    Write-Host "All automated tests PASSED. Proceed with the manual sections of RELEASE_GATE.md." -ForegroundColor Green
} else {
    Write-Host "Automated suite FAILED. Release is held. See $reportFile." -ForegroundColor Red
}
exit $exit
