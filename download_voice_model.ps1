param([switch]$Force)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath
$ModelDirectory = Join-Path $ProjectRoot "models\vosk-ru"
# Official model catalog: https://alphacephei.com/vosk/models
$ModelUrl = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"

function Test-VoskModel([string]$Path) {
    foreach ($RelativePath in @(
        "am\final.mdl",
        "conf\model.conf",
        "graph\HCLr.fst",
        "graph\Gr.fst"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $RelativePath))) {
            return $false
        }
    }
    return $true
}

if ((Test-VoskModel $ModelDirectory) -and -not $Force) {
    Write-Host "Russian Vosk model is already installed: $ModelDirectory" -ForegroundColor Green
    exit 0
}

$TempBase = [System.IO.Path]::GetTempPath().TrimEnd('\')
$TempDirectory = Join-Path $TempBase ("mia-vosk-" + [guid]::NewGuid().ToString("N"))
$ArchivePath = Join-Path $TempDirectory "vosk-model-small-ru-0.22.zip"
$ExtractDirectory = Join-Path $TempDirectory "extracted"

New-Item -ItemType Directory -Force -Path $ExtractDirectory | Out-Null
try {
    Write-Host "Downloading the official Russian Vosk model (about 45 MB)..." -ForegroundColor Yellow
    Invoke-WebRequest -UseBasicParsing -Uri $ModelUrl -OutFile $ArchivePath
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractDirectory -Force

    $ExtractedModel = Join-Path $ExtractDirectory "vosk-model-small-ru-0.22"
    if (-not (Test-VoskModel $ExtractedModel)) {
        throw "The downloaded archive does not contain a valid Vosk model."
    }

    New-Item -ItemType Directory -Force -Path $ModelDirectory | Out-Null
    Copy-Item -Path (Join-Path $ExtractedModel "*") -Destination $ModelDirectory -Recurse -Force
    if (-not (Test-VoskModel $ModelDirectory)) {
        throw "The Russian Vosk model failed validation after extraction."
    }
    Write-Host "Russian Vosk model installed: $ModelDirectory" -ForegroundColor Green
} finally {
    $ResolvedTemp = [System.IO.Path]::GetFullPath($TempDirectory)
    if ($ResolvedTemp.StartsWith($TempBase, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $ResolvedTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
