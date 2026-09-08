$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup.ps1 first."
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m pip install -r "requirements-dev.txt"
& $Python -m PyInstaller --noconfirm --clean "MIA-Dev-Agent.spec"
$BuildDirectory = Join-Path $ProjectRoot "dist\MIA-Dev-Agent"
Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $BuildDirectory -Force
$VoiceModel = Join-Path $ProjectRoot "models\vosk-ru"
if (Test-Path -LiteralPath (Join-Path $VoiceModel "am\final.mdl")) {
    $BuildModels = Join-Path $BuildDirectory "models"
    New-Item -ItemType Directory -Force -Path $BuildModels | Out-Null
    Copy-Item -LiteralPath $VoiceModel -Destination $BuildModels -Recurse -Force
    Write-Host "Included the offline Russian Vosk model." -ForegroundColor Green
}
Write-Host "Build complete: dist\MIA-Dev-Agent" -ForegroundColor Green
Write-Host "Copy .env.example to .env inside that directory and add your API key." -ForegroundColor Yellow
