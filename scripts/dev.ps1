# Sobe o backend (127.0.0.1:8787) e o Vite (5173) em janelas separadas.
#
#   .\scripts\dev.ps1
#
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  python -m venv .venv
}
if (-not (Test-Path "frontend\node_modules")) {
  Push-Location frontend
  npm install --no-audit --no-fund
  Pop-Location
}

Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "Set-Location '$root'; .\.venv\Scripts\python.exe -m agent_core.app --port 8787 --no-browser"
) -WorkingDirectory "$root\backend"

Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "Set-Location '$root\frontend'; npm run dev"
)

Write-Host "Backend: http://127.0.0.1:8787/api/health" -ForegroundColor Green
Write-Host "UI dev : http://127.0.0.1:5173" -ForegroundColor Green
