@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "INTERACTIVE_PAUSE=0"
if "%~1"=="" set "INTERACTIVE_PAUSE=1"
if not "%~1"=="" if exist "%~1\" if "%~2"=="" set "INTERACTIVE_PAUSE=1"

set "RUNTIME_DIR=%LOCALAPPDATA%\Clase2PDF"
if "%LOCALAPPDATA%"=="" set "RUNTIME_DIR=%USERPROFILE%\AppData\Local\Clase2PDF"
set "VENV_PY=%RUNTIME_DIR%\venv\Scripts\python.exe"
if "%CLASE2PDF_REPO_URL%"=="" set "CLASE2PDF_REPO_URL=https://raw.githubusercontent.com/PabloDiaZzz/Clase2PDF/main"
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%"

:: 0. Auto-actualizacion desde GitHub (pipeline.py, dependencias y .bat) con fallback offline
if "%CLASE2PDF_NO_UPDATE%"=="1" goto :skip_update
set "BAT_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; $rt=$env:RUNTIME_DIR; $base=$env:CLASE2PDF_REPO_URL.TrimEnd('/'); $bat=$env:BAT_SELF; $pyDst=Join-Path $rt 'pipeline.py';" ^
    "function Get-Sha($p){[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([IO.File]::ReadAllBytes($p))).Replace('-','').ToLowerInvariant()};" ^
    "try { $ts=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $m=Invoke-RestMethod -Uri ($base+'/manifest.json?t='+$ts) -TimeoutSec 4 -UseBasicParsing } catch { if (Test-Path -LiteralPath $pyDst) { exit 0 }; Write-Host 'ERROR [UPDATE_OFFLINE]: Sin conexion y falta pipeline.py local.' -ForegroundColor Red; exit 2 };" ^
    "if ($m.requirements) { Set-Content -LiteralPath (Join-Path $rt '.req_pkgs') -Value ($m.requirements -join ' ') -Encoding ASCII };" ^
    "if ($m.req_hash) { Set-Content -LiteralPath (Join-Path $rt '.req_hash_want') -Value $m.req_hash -Encoding ASCII };" ^
    "$cur=''; if (Test-Path -LiteralPath $pyDst) { $cur=Get-Sha $pyDst };" ^
    "if ($cur -ne $m.pipeline_sha256.ToLowerInvariant()) {" ^
    "  $tmp=Join-Path $rt 'pipeline.py.tmp'; Invoke-WebRequest -Uri ($base+'/pipeline.py?t='+$ts) -OutFile $tmp -TimeoutSec 15 -UseBasicParsing;" ^
    "  if ((Get-Sha $tmp) -ne $m.pipeline_sha256.ToLowerInvariant()) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue; throw 'SHA-256 mismatch en pipeline.py' };" ^
    "  Move-Item -LiteralPath $tmp -Destination $pyDst -Force; Set-Content -LiteralPath (Join-Path $rt '.pipe_hash') -Value $m.pipeline_sha256.Substring(0,16) -Encoding ASCII" ^
    "};" ^
    "if ($m.instructions_sha256) {" ^
    "  try {" ^
    "    $batDir=Split-Path $bat -Parent; $insDst=Join-Path $batDir 'INSTRUCCIONES.md'; $icur=''; if (Test-Path -LiteralPath $insDst) { $icur=Get-Sha $insDst };" ^
    "    if ($icur -ne $m.instructions_sha256.ToLowerInvariant()) {" ^
    "      $itmp=Join-Path $rt 'INSTRUCCIONES.md.tmp'; Invoke-WebRequest -Uri ($base+'/INSTRUCCIONES.md?t='+$ts) -OutFile $itmp -TimeoutSec 15 -UseBasicParsing;" ^
    "      if ((Get-Sha $itmp) -eq $m.instructions_sha256.ToLowerInvariant()) { Move-Item -LiteralPath $itmp -Destination $insDst -Force } else { Remove-Item $itmp -Force -ErrorAction SilentlyContinue }" ^
    "    }" ^
    "  } catch {}" ^
    "};" ^
    "if ($env:CLASE2PDF_BAT_REEXEC -ne '1' -and $m.bat_sha256) {" ^
    "  $bcur=Get-Sha $bat; if ($bcur -ne $m.bat_sha256.ToLowerInvariant()) {" ^
    "    $btmp=Join-Path $rt 'procesar_clase_new.bat'; Invoke-WebRequest -Uri ($base+'/procesar_clase.bat?t='+$ts) -OutFile $btmp -TimeoutSec 15 -UseBasicParsing;" ^
    "    if ((Get-Sha $btmp) -ne $m.bat_sha256.ToLowerInvariant()) { Remove-Item $btmp -Force -ErrorAction SilentlyContinue; throw 'SHA-256 mismatch en procesar_clase.bat' };" ^
    "    exit 10" ^
    "  }" ^
    "}"
set "UPD_RC=!errorlevel!"
if "!UPD_RC!"=="10" (
    copy /y "%RUNTIME_DIR%\procesar_clase_new.bat" "%~f0" >nul
    set "CLASE2PDF_BAT_REEXEC=1"
    cmd /c ""%~f0" %*"
    exit /b !errorlevel!
)
if not "!UPD_RC!"=="0" (
    call :maybe_pause
    exit /b !UPD_RC!
)

:skip_update
if not exist "%RUNTIME_DIR%\pipeline.py" (
    echo ERROR [MISSING_PIPELINE]: No existe pipeline.py en %RUNTIME_DIR%.
    call :maybe_pause
    exit /b 1
)

:: Ruta rapida si el entorno y dependencias coinciden con manifest.json
set "REQ_WANT="
set "REQ_DONE="
if exist "%RUNTIME_DIR%\.req_hash_want" set /p REQ_WANT=<"%RUNTIME_DIR%\.req_hash_want"
if exist "%RUNTIME_DIR%\.req_hash_done" set /p REQ_DONE=<"%RUNTIME_DIR%\.req_hash_done"
if exist "%VENV_PY%" if not "!REQ_DONE!"=="" (
    if "!REQ_WANT!"=="" goto :run_pipeline
    if "!REQ_DONE!"=="!REQ_WANT!" goto :run_pipeline
)

:: 1. Verificar ffmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
    where winget >nul 2>&1
    if errorlevel 1 (
        echo ERROR [MISSING_WINGET]: Falta winget para instalar ffmpeg. Instala "App Installer" desde Microsoft Store o ffmpeg manualmente.
        call :maybe_pause
        exit /b 1
    )
    echo Instalando componentes de audio...
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    call :refresh_path
    where ffmpeg >nul 2>&1
    if errorlevel 1 (
        echo ERROR: No se pudo detectar ffmpeg tras instalar. Cierra y vuelve a abrir este script.
        call :maybe_pause
        exit /b 1
    )
)

:: 2. Verificar Python real (evitar alias de Microsoft Store)
set "PY_CMD="
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
) else (
    py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)

if "!PY_CMD!"=="" (
    where winget >nul 2>&1
    if errorlevel 1 (
        echo ERROR [MISSING_WINGET]: Falta winget para instalar Python 3.12. Instalalo desde python.org o Microsoft Store.
        call :maybe_pause
        exit /b 1
    )
    echo Instalando Python...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    call :refresh_path
    python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=python"
    ) else (
        py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
        if not errorlevel 1 set "PY_CMD=py -3"
    )
    if "!PY_CMD!"=="" (
        echo ERROR: No se pudo detectar Python tras instalar. Cierra y vuelve a abrir este script.
        call :maybe_pause
        exit /b 1
    )
)

:: 3. Entorno virtual y dependencias en %LOCALAPPDATA%\Clase2PDF\venv
if not exist "%VENV_PY%" (
    echo Preparando primera ejecucion...
    !PY_CMD! -m venv "%RUNTIME_DIR%\venv"
    if errorlevel 1 (
        echo ERROR [ENV_SETUP]: No se pudo crear el entorno ^(codigo !errorlevel!^).
        call :maybe_pause
        exit /b 1
    )
)

set "REQ_PKGS=google-genai rich questionary pypdf fpdf2"
if exist "%RUNTIME_DIR%\.req_pkgs" set /p REQ_PKGS=<"%RUNTIME_DIR%\.req_pkgs"
"%VENV_PY%" -c "import google.genai, rich, questionary, pypdf, fpdf" >nul 2>&1
if errorlevel 1 (
    echo Preparando dependencias...
    "%VENV_PY%" -m pip install -q --upgrade pip !REQ_PKGS!
    if errorlevel 1 (
        echo ERROR [ENV_SETUP]: No se pudieron instalar dependencias ^(codigo !errorlevel!^).
        call :maybe_pause
        exit /b 1
    )
)
if "!REQ_WANT!"=="" set "REQ_WANT=default"
>"%RUNTIME_DIR%\.req_hash_done" echo !REQ_WANT!

:run_pipeline
:: 4. Ejecutar pipeline
if "%INTERACTIVE_PAUSE%"=="1" cls
"%VENV_PY%" "%RUNTIME_DIR%\pipeline.py" %*
set "EXIT_CODE=%errorlevel%"

if "%INTERACTIVE_PAUSE%"=="1" (
    if not "%EXIT_CODE%"=="0" (
        echo.
        echo Proceso detenido con error ^(codigo %EXIT_CODE%^).
    )
    call :maybe_pause
)
exit /b %EXIT_CODE%

:maybe_pause
if "%INTERACTIVE_PAUSE%"=="1" (
    echo.
    echo Pulsa cualquier tecla para cerrar...
    pause >nul
)
exit /b 0

:refresh_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P"
exit /b 0
