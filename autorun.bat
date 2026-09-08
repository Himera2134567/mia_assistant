@echo off
setlocal
chcp 65001 >nul
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск MIA: устанавливаю зависимости…
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\setup.ps1"
  if errorlevel 1 (
    echo Установка завершилась с ошибкой.
    pause
    exit /b 1
  )
)

if not exist ".env" copy /Y ".env.example" ".env" >nul

set "GIT_PYTHON_REFRESH=quiet"
".venv\Scripts\python.exe" "mia_dev_agent.py"
if errorlevel 1 pause
