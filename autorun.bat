@echo off
setlocal
chcp 65001 >nul
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

set "NEED_SETUP=0"
if not exist ".venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "models\vosk-ru\am\final.mdl" set "NEED_SETUP=1"
if not exist "models\whisper\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo\snapshots" set "NEED_SETUP=1"
if not exist "models\llm\qwen2.5-3b-instruct-q4_k_m.gguf" set "NEED_SETUP=1"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sounddevice, vosk" >nul 2>&1
  if errorlevel 1 set "NEED_SETUP=1"
  ".venv\Scripts\python.exe" -c "import llama_cpp" >nul 2>&1
  if errorlevel 1 set "NEED_SETUP=1"
)

if "%NEED_SETUP%"=="1" (
  echo Preparing MIA and the offline Russian voice model...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\setup.ps1" -WithVoice -WithLocalAI
  if errorlevel 1 (
    echo MIA setup failed. Please copy this window and send it for diagnostics.
    pause
    exit /b 1
  )
)

if not exist ".env" copy /Y ".env.example" ".env" >nul

set "GIT_PYTHON_REFRESH=quiet"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" "mia_dev_agent.py"
if errorlevel 1 pause
