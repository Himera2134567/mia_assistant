@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" py -3.12 -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\pip.exe" install -r requirements.txt

if not exist ".env" (
  > ".env" (
    echo OPENROUTER_API_KEY=
    echo OPENROUTER_MODEL=deepseek/deepseek-chat
  )
)

set "GIT_PYTHON_REFRESH=quiet"
if exist "%LocalAppData%\GitHubDesktop\app-3.5.3\resources\app\git\mingw64\bin\git.exe" set "GIT_PYTHON_GIT_EXECUTABLE=%LocalAppData%\GitHubDesktop\app-3.5.3\resources\app\git\mingw64\bin\git.exe"

for %%F in (run_mia.py mia_dev_agent.py) do (
  if exist "%%F" set "TARGET=%%F"
)
if not defined TARGET set "TARGET=mia_dev_agent.py"

powershell -NoProfile -Command ^
"$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'; ^
 if (!(Test-Path $startup)) { throw 'Startup folder not found: ' + $startup }; ^
 $w = New-Object -ComObject WScript.Shell; ^
 $lnk = Join-Path $startup 'MIA-Dev-Agent.lnk'; ^
 $s = $w.CreateShortcut($lnk); ^
 $s.TargetPath = (Resolve-Path '.\.venv\Scripts\python.exe').Path; ^
 $s.Arguments = '%TARGET%'; ^
 $s.WorkingDirectory = (Resolve-Path '.\').Path; ^
 $s.WindowStyle = 7; ^
 $s.Save()"

".venv\Scripts\python.exe" "%TARGET%"
