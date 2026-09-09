param(
    [string]$OpenRouterKey = "",
    [string]$DeepSeekKey = "",
    [switch]$WithVoice,
    [switch]$WithLocalAI,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { (Get-Location).Path }
Set-Location -LiteralPath $ProjectRoot

Write-Host "MIA Assistant - setup" -ForegroundColor Cyan
Write-Host "Project directory: $ProjectRoot"

foreach ($directory in @("logs", "memory", "models", "ui")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $directory) | Out-Null
}

$PythonCommand = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        & py -3.12 --version | Out-Null
        if ($LASTEXITCODE -eq 0) { $PythonCommand = @("py", "-3.12") }
    } catch {}
}
if (-not $PythonCommand -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $PythonCommand = @("python")
}
if (-not $PythonCommand) {
    throw "Python 3.10+ was not found. Install it from https://www.python.org/downloads/"
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating the .venv virtual environment..." -ForegroundColor Yellow
    if ($PythonCommand.Count -eq 2) {
        & $PythonCommand[0] $PythonCommand[1] -m venv ".venv"
    } else {
        & $PythonCommand[0] -m venv ".venv"
    }
}

Write-Host "Installing dependencies..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r "requirements.txt"
if ($WithVoice) {
    Write-Host "Installing local speech-to-text..." -ForegroundColor Yellow
    & $VenvPython -m pip install -r "requirements-voice.txt"
    & (Join-Path $ProjectRoot "download_voice_model.ps1")
    & $VenvPython -c "from vosk import Model, SetLogLevel; from mia_voice import native_model_path; SetLogLevel(-1); Model(native_model_path('models/vosk-ru')); print('Russian Vosk model: OK')"
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    & $VenvPython -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8', download_root='models/whisper'); print('Whisper large-v3-turbo: OK')"
}

if ($WithLocalAI) {
    Write-Host "Installing the keyless local AI fallback..." -ForegroundColor Yellow
    & $VenvPython -m pip install -r "requirements-local.txt"
    & (Join-Path $ProjectRoot "download_local_model.ps1")
    & $VenvPython -c "from mia_core import local_model_ready; assert local_model_ready(); print('Local Qwen model: OK')"
}

$EnvPath = Join-Path $ProjectRoot ".env"
$ExamplePath = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path -LiteralPath $EnvPath)) {
    Copy-Item -LiteralPath $ExamplePath -Destination $EnvPath
    Write-Host "Created .env from the safe template." -ForegroundColor Green
}

function Set-EnvValue([string]$Name, [string]$Value) {
    $Lines = Get-Content -LiteralPath $EnvPath
    $Found = $false
    $Updated = foreach ($Line in $Lines) {
        if ($Line -match ("^" + [regex]::Escape($Name) + "=")) {
            $Found = $true
            "$Name=$Value"
        } else {
            $Line
        }
    }
    if (-not $Found) { $Updated += "$Name=$Value" }
    $Updated | Set-Content -LiteralPath $EnvPath -Encoding UTF8
}

if (-not [string]::IsNullOrWhiteSpace($OpenRouterKey)) {
    Set-EnvValue "OPENROUTER_API_KEY" $OpenRouterKey
    Write-Host "Saved the OpenRouter API key in the local .env file." -ForegroundColor Green
}
if (-not [string]::IsNullOrWhiteSpace($DeepSeekKey)) {
    Set-EnvValue "DEEPSEEK_API_KEY" $DeepSeekKey
    Write-Host "Saved the DeepSeek API key in the local .env file." -ForegroundColor Green
}

Write-Host "Checking the application import..." -ForegroundColor Yellow
$env:QT_QPA_PLATFORM = "offscreen"
& $VenvPython -c "import mia_dev_agent; print('MIA import: OK')"
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue

Write-Host "Setup complete. Run autorun.bat to start MIA." -ForegroundColor Green
if (
    [string]::IsNullOrWhiteSpace($OpenRouterKey) -and
    [string]::IsNullOrWhiteSpace($DeepSeekKey)
) {
    Write-Host "Cloud access is optional: add a fresh DEEPSEEK_API_KEY or OPENROUTER_API_KEY to .env." -ForegroundColor Yellow
}

if ($Launch) {
    & $VenvPython "mia_dev_agent.py"
}
