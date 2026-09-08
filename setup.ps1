param(
  [string]$OpenRouterKey = ""
)

$ErrorActionPreference = 'Stop'

# --- TLS ---
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor `
                                               [Net.SecurityProtocolType]::Tls13
} catch {}

# --- ROOT корректно и для скрипта, и для интерактива ---
$ROOT = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { (Get-Location).Path }
Set-Location $ROOT
Write-Host "ROOT: $ROOT"

# --- Структура директорий ---
$dirs = @(
  "assets",
  "tts",
  "ui",
  "ui\web",
  "models",
  "models\whisper",
  "memory",
  "logs"
)
foreach($d in $dirs){ New-Item -ItemType Directory -Force -Path (Join-Path $ROOT $d) | Out-Null }

# --- requirements.txt (создадим, если отсутствует) ---
$reqPath = Join-Path $ROOT "requirements.txt"
if (!(Test-Path $reqPath)) {
@"
PySide6==6.7.2
requests>=2.32.0
python-dotenv>=1.0.1
ddgs>=9.7.0
numpy>=1.26.4
sounddevice>=0.4.6
faster-whisper>=1.2.0
pyttsx3>=2.99
lxml>=6.0.0
readability-lxml>=0.8.1
"@ | Out-File -Encoding UTF8 $reqPath
  Write-Host "Создан $reqPath"
}

# --- .env (обновим/создадим) ---
$envPath = Join-Path $ROOT ".env"
$envMap = @{}
if (Test-Path $envPath) {
  Get-Content $envPath | ForEach-Object {
    if ($_ -match "^\s*#") { return }
    if ($_ -match "^\s*$") { return }
    $kv = $_ -split "=", 2
    if ($kv.Count -eq 2) { $envMap[$kv[0]] = $kv[1] }
  }
}
if (-not $envMap.ContainsKey("OPENROUTER_MODEL")) { $envMap["OPENROUTER_MODEL"] = "deepseek/deepseek-chat" }
if ($OpenRouterKey -and $OpenRouterKey.Trim() -ne "") { $envMap["OPENROUTER_API_KEY"] = $OpenRouterKey }
$envMap.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" } | Out-File -Encoding UTF8 $envPath
Write-Host "Обновлён $envPath"

# --- Пересоздаём venv ---
if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
$pyCmd = "py"
try { & $pyCmd --version | Out-Null } catch { $pyCmd = "python" }
& $pyCmd -3.12 -m venv ".venv"

$venvPy  = Join-Path $ROOT ".venv\Scripts\python.exe"
if (!(Test-Path $venvPy)) { throw "Не найден интерпретатор venv: $venvPy" }

# --- Обновляем инструменты и ставим зависимости (строго через venv) ---
& $venvPy -m pip install --upgrade pip setuptools wheel
& $venvPy -m pip install -r $reqPath

# --- Piper (exe) ---
$ttsDir   = Join-Path $ROOT "tts"
$piperExe = Join-Path $ttsDir "piper.exe"
if (!(Test-Path $piperExe)) {
  $zipUrl  = "https://github.com/rhasspy/piper/releases/latest/download/piper_windows_amd64.zip"
  $zipPath = Join-Path $ttsDir "piper_windows_amd64.zip"
  Write-Host "Скачиваю Piper: $zipUrl"
  Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $ttsDir, $true)
  Remove-Item $zipPath -Force
  $found = Get-ChildItem -Path $ttsDir -Recurse -Filter "piper.exe" -File | Select-Object -First 1
  if ($found -and ($found.FullName -ne $piperExe)) { Copy-Item $found.FullName $piperExe -Force }
  if (!(Test-Path $piperExe)) { throw "Не удалось найти piper.exe после распаковки." }
  Write-Host "Piper готов: $piperExe"
}

# --- Русский голос для Piper ---
$voiceBase = "ru_RU-irina-medium"
$onnxPath  = Join-Path $ttsDir "$voiceBase.onnx"
$jsonPath  = Join-Path $ttsDir "$voiceBase.onnx.json"
if (!(Test-Path $onnxPath)) {
  $onnxUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx"
  Write-Host "Скачиваю голос (.onnx): $onnxUrl"
  Invoke-WebRequest -Uri $onnxUrl -OutFile $onnxPath
}
if (!(Test-Path $jsonPath)) {
  $jsonUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json"
  Write-Host "Скачиваю голос (.json): $jsonUrl"
  Invoke-WebRequest -Uri $jsonUrl -OutFile $jsonPath
}

# --- 3D-аватар (GLB) ---
$assetsDir = Join-Path $ROOT "assets"
$avatarGlb = Join-Path $assetsDir "avatar.glb"
if (!(Test-Path $avatarGlb)) {
  $glbUrl = "https://github.com/KhronosGroup/glTF-Sample-Models/raw/master/2.0/Fox/glTF-Binary/Fox.glb"
  Write-Host "Скачиваю 3D-аватар: $glbUrl"
  Invoke-WebRequest -Uri $glbUrl -OutFile $avatarGlb
}

# --- style.qss (если нет) ---
$stylePath = Join-Path $ROOT "ui\style.qss"
if (!(Test-Path $stylePath)) {
@"
QMainWindow { background:#0d1117; color:#c9d1d9; }
QTextEdit, QLineEdit { background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:6px; }
QPushButton { background:#238636; color:white; border:0; border-radius:6px; padding:8px 12px; }
QPushButton:hover { background:#2ea043; }
QSplitter::handle { background:#161b22; width:6px; }
QProgressBar { background:#161b22; border:1px solid #30363d; border-radius:4px; }
"@ | Out-File -Encoding UTF8 $stylePath
}

# --- ui\web\avatar.html (Three.js холдер) ---
$webDir = Join-Path $ROOT "ui\web"
$avatarHtml = Join-Path $webDir "avatar.html"
if (!(Test-Path $avatarHtml)) {
@"
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<style>html,body{margin:0;height:100%;background:#0d1117;overflow:hidden}#c{width:100%;height:100%;display:block}</style>
</head><body><canvas id="c"></canvas>
<script type="module">
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.160/build/three.module.js";
import {OrbitControls} from "https://cdn.jsdelivr.net/npm/three@0.160/examples/jsm/controls/OrbitControls.js";
import {GLTFLoader} from "https://cdn.jsdelivr.net/npm/three@0.160/examples/jsm/loaders/GLTFLoader.js";
const canvas=document.getElementById('c');
const renderer=new THREE.WebGLRenderer({canvas,antialias:true});renderer.setSize(innerWidth,innerHeight);renderer.setPixelRatio(Math.min(2,devicePixelRatio));
const scene=new THREE.Scene();scene.background=new THREE.Color(0x0d1117);
const camera=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,0.1,100);camera.position.set(0,1.4,2.2);
const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.target.set(0,1,0);
addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
scene.add(new THREE.HemisphereLight(0xffffff,0x222244,1.0));const dir=new THREE.DirectionalLight(0xffffff,1.2);dir.position.set(1,2,2);scene.add(dir);
const url="file:///${(Join-Path $assetsDir "avatar.glb").Replace('\','/')}";
const loader=new GLTFLoader();
loader.load(url,(g)=>{const r=g.scene;r.traverse(o=>{if(o.isMesh){o.castShadow=true;o.receiveShadow=true}});scene.add(r);animate();},undefined,()=>{addCube();animate();});
function addCube(){const m=new THREE.Mesh(new THREE.BoxGeometry(1,1,1),new THREE.MeshStandardMaterial({color:0x6ea8fe}));m.rotation.y=Math.PI/4;scene.add(m);}
function animate(){requestAnimationFrame(animate);controls.update();renderer.render(scene,camera);}
</script></body></html>
"@ | Out-File -Encoding UTF8 $avatarHtml
}

# --- Санити-чек через временный .py (без Bash-хердоков) ---
Write-Host "`nПроверяю окружение..."
$pyTmp = Join-Path $env:TEMP "mia_sanity_check.py"
@"
import os, sys
print("PY:", sys.executable)
print("CWD:", os.getcwd())
from ddgs import DDGS
print("ddgs OK")
from faster_whisper import WhisperModel
m = WhisperModel("tiny", device="cpu", compute_type="int8")
print("Whisper OK (tiny loaded)")
print("ENV OPENROUTER_MODEL =", os.getenv("OPENROUTER_MODEL"))
print("ENV OPENROUTER_API_KEY set? ->", bool(os.getenv("OPENROUTER_API_KEY")))
"@ | Out-File -Encoding UTF8 $pyTmp

& $venvPy $pyTmp
Remove-Item $pyTmp -Force

Write-Host "`nГОТОВО. Активируй venv и запускай GUI:"
Write-Host " .\.venv\Scripts\activate"
Write-Host " python app.py"
