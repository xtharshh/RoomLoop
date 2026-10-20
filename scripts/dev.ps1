# Start RoomLoop on Windows from a clean checkout.
#
#   .\scripts\dev.ps1              # install, seed, run
#   .\scripts\dev.ps1 -NoSeed      # keep the existing database
#   .\scripts\dev.ps1 -Port 8080
#
# Why this script exists: on Windows `uvicorn` is often not on PATH (the venv is
# not active), and there is no IANA timezone database, which makes every booking
# fail. Both are handled here.

param(
    [int]$Port = 8000,
    [switch]$NoSeed,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path '.venv')) {
    Write-Host '==> Creating virtual environment' -ForegroundColor Cyan
    python -m venv .venv
}

$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "Could not find $python. Is Python 3.11+ installed and on PATH?"
}

Write-Host '==> Installing dependencies' -ForegroundColor Cyan
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r requirements-dev.txt --quiet

# Belt and braces: tzdata is in requirements.txt, but an old venv may predate it.
& $python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '==> Installing tzdata (no IANA timezone database found)' -ForegroundColor Yellow
    & $python -m pip install tzdata --quiet
}

& $python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Berlin'); ZoneInfo('America/Denver')"
if ($LASTEXITCODE -ne 0) {
    throw 'Timezone data still unavailable. Run: .\.venv\Scripts\python.exe -m pip install tzdata'
}

if ($Test) {
    & $python -m pytest
    exit $LASTEXITCODE
}

if (-not $NoSeed) {
    Write-Host '==> Seeding demo data' -ForegroundColor Cyan
    & $python -m roomloop.seed --reset
}

Write-Host ''
Write-Host "==> RoomLoop console: http://localhost:$Port/console" -ForegroundColor Green
Write-Host "==> API docs:        http://localhost:$Port/docs" -ForegroundColor Green
Write-Host ''
& $python -m uvicorn roomloop.api:app --reload --port $Port
