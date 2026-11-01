@echo off
REM ---------------------------------------------------------------
REM  System 32
REM  Authored by SoullessScythe
REM  Built over many late nights, one iteration at a time.
REM ---------------------------------------------------------------

chcp 65001 >nul
title System 32
color 0A
setlocal

set "SCRIPT_DIR=%~dp0"
set PYTHON=

rem 1. System 32's own portable Python (always first)
if exist "%SCRIPT_DIR%python\python.exe" goto :own_python

rem    Not there yet (a fresh copy, e.g. from GitHub): download it. System 32 runs on its
rem    OWN Python on the drive, so nothing gets installed into this computer's Python.
echo  [..] Getting System 32's own portable Python 3.12 (one-time, about 10 MB)...
set "S32PY=%SCRIPT_DIR%python"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $d=$env:S32PY; New-Item -ItemType Directory -Force -Path $d | Out-Null; $z=Join-Path $d 'python.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip' -OutFile $z; Expand-Archive -Force -Path $z -DestinationPath $d; Remove-Item $z; Get-ChildItem -Path $d -Filter '*._pth' | ForEach-Object { (Get-Content $_.FullName) -replace '^#import site','import site' | Set-Content $_.FullName }"
if exist "%SCRIPT_DIR%python\python.exe" (
    echo  [OK] Python ready.
) else (
    echo  [??] Couldn't download Python - trying this computer's Python instead.
)

:own_python
if exist "%SCRIPT_DIR%python\python.exe" (
    set "PYTHON=%SCRIPT_DIR%python\python.exe"
    goto :found_python
)

rem 2. ComfyUI Documents venv
if exist "%USERPROFILE%\Documents\ComfyUI\.venv\Scripts\python.exe" (
    set "PYTHON=%USERPROFILE%\Documents\ComfyUI\.venv\Scripts\python.exe"
    goto :found_python
)

rem 3. uv-managed Python
if exist "%APPDATA%\uv\python\" (
    for /d %%D in ("%APPDATA%\uv\python\cpython-3*") do (
        if exist "%%D\python.exe" (
            set "PYTHON=%%D\python.exe"
            goto :found_python
        )
    )
)

rem 4. Python on PATH (skipping the Microsoft Store stand-in, which isn't Python)
for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | find /i "WindowsApps" >nul || (
        set "PYTHON=%%P"
        goto :found_python
    )
)

echo  [ERROR] Python not found, and it couldn't be downloaded.
echo          Check the internet connection and start again, or install Python 3.12
echo          from python.org.
pause
exit /b 1

:found_python
cd /d "%SCRIPT_DIR%"
"%PYTHON%" launch.py %*

echo.
echo  [System 32] Stopped. Press any key to close.
pause
