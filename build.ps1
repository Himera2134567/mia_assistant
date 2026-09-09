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
$WhisperModels = Join-Path $ProjectRoot "models\whisper"
if (Test-Path -LiteralPath $WhisperModels) {
    $BuildWhisper = Join-Path $BuildDirectory "models\whisper"
    New-Item -ItemType Directory -Force -Path $BuildWhisper | Out-Null
    Copy-Item -Path (Join-Path $WhisperModels "*") -Destination $BuildWhisper -Recurse -Force
    Write-Host "Included the accurate Whisper models." -ForegroundColor Green
}
$LocalModel = Join-Path $ProjectRoot "models\llm\qwen2.5-3b-instruct-q4_k_m.gguf"
if (Test-Path -LiteralPath $LocalModel) {
    $BuildLocal = Join-Path $BuildDirectory "models\llm"
    New-Item -ItemType Directory -Force -Path $BuildLocal | Out-Null
    Copy-Item -LiteralPath $LocalModel -Destination $BuildLocal -Force
    Write-Host "Included the keyless local Qwen model." -ForegroundColor Green
}
Write-Host "Build complete: dist\MIA-Dev-Agent" -ForegroundColor Green
Write-Host "Cloud keys are optional. Copy .env.example to .env only for DeepSeek or OpenRouter." -ForegroundColor Yellow
