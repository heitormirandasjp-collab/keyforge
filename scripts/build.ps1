# Gera o executável do backend (PyInstaller) e, opcionalmente, o instalador (Inno Setup).
#
#   .\scripts\build.ps1              # frontend + backend (.exe em .\dist)
#   .\scripts\build.ps1 -Installer   # + instalador em .\dist_installer
#
param(
  [switch]$SkipFrontend,
  [switch]$SkipBackend,
  [switch]$Installer
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $SkipFrontend) {
  Write-Host "==> build do frontend" -ForegroundColor Cyan
  Push-Location frontend
  if (-not (Test-Path node_modules)) {
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install falhou (exit $LASTEXITCODE)" }
  }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "npm run build falhou (exit $LASTEXITCODE)" }
  Pop-Location
  if (-not (Test-Path "frontend\dist\index.html")) {
    throw "frontend/dist/index.html não foi gerado"
  }
}

if (-not $SkipBackend) {
  Write-Host "==> ambiente Python" -ForegroundColor Cyan
  if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
  }
  & .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  if ($LASTEXITCODE -ne 0) { throw "pip upgrade falhou (exit $LASTEXITCODE)" }
  & .\.venv\Scripts\python.exe -m pip install --quiet pyinstaller
  if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller falhou (exit $LASTEXITCODE)" }

  Write-Host "==> PyInstaller (onefile)" -ForegroundColor Cyan
  # Caminho absoluto: --add-data é resolvido relativo a --specpath (build\), não ao CWD.
  $frontendDist = Join-Path $root "frontend\dist"
  & .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm --clean --onefile `
    --name keyforge-backend `
    --paths backend `
    --add-data "${frontendDist};frontend_dist" `
    --distpath dist `
    --workpath build\pyinstaller `
    --specpath build `
    backend\keyforge_launcher.py
  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller falhou (exit $LASTEXITCODE). Se o .exe estiver em uso, feche o KeyForge e tente novamente."
  }

  if (-not (Test-Path "dist\keyforge-backend.exe")) {
    throw "dist\keyforge-backend.exe não foi gerado"
  }
  Write-Host "OK: dist\keyforge-backend.exe" -ForegroundColor Green
}

if ($Installer) {
  Write-Host "==> Inno Setup" -ForegroundColor Cyan
  $iscc = $null
  foreach ($candidate in @(
      "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
      "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
      "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )) {
    if (Test-Path $candidate) { $iscc = $candidate; break }
  }
  if (-not $iscc) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $iscc = $command.Source }
  }
  if (-not $iscc) {
    throw "ISCC.exe (Inno Setup 6) não encontrado. Instale com: winget install --id JRSoftware.InnoSetup -e"
  }

  & $iscc "installer\keyforge.iss"
  if ($LASTEXITCODE -ne 0) { throw "ISCC falhou (exit $LASTEXITCODE)" }
  Write-Host "OK: dist_installer\KeyForge-Setup-*.exe" -ForegroundColor Green
}
