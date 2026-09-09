$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath
$ModelDirectory = Join-Path $ProjectRoot "models\llm"
$ModelPath = Join-Path $ModelDirectory "qwen2.5-3b-instruct-q4_k_m.gguf"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ((Test-Path -LiteralPath $ModelPath) -and (Get-Item -LiteralPath $ModelPath).Length -gt 1GB) {
    Write-Host "Local Qwen model is already installed: $ModelPath" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment was not found. Run setup.ps1 first."
}

New-Item -ItemType Directory -Force -Path $ModelDirectory | Out-Null
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
Write-Host "Downloading the official Qwen2.5 3B local model (about 2 GB)..." -ForegroundColor Yellow
& $Python -c "from huggingface_hub import hf_hub_download; print(hf_hub_download('Qwen/Qwen2.5-3B-Instruct-GGUF', 'qwen2.5-3b-instruct-q4_k_m.gguf', local_dir=r'$ModelDirectory'))"
if ($LASTEXITCODE -ne 0) {
    throw "The local Qwen model download failed."
}
if (-not (Test-Path -LiteralPath $ModelPath) -or (Get-Item -LiteralPath $ModelPath).Length -le 1GB) {
    throw "The downloaded local Qwen model failed validation."
}
Write-Host "Local Qwen model installed: $ModelPath" -ForegroundColor Green
