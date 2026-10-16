@echo off
rem ===========================================================================
rem  Video Splitter Studio - launcher
rem
rem  Finds a *working* Python (PATH first, then the DSH bundled runtime), then
rem  starts main.py, which bootstraps PySide6 in .\vendor on first run.
rem
rem  Note: C:\...\WindowsApps\python.exe is a Microsoft Store stub that exists
rem  even when Python is not installed, so every candidate is test-run before
rem  it is accepted.
rem ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYEXE="

for %%C in (python.exe py.exe) do (
    if not defined PYEXE (
        for /f "delims=" %%P in ('where %%C 2^>nul') do (
            if not defined PYEXE (
                "%%P" -c "import sys" >nul 2>nul
                if !errorlevel! equ 0 set "PYEXE=%%P"
            )
        )
    )
)

if not defined PYEXE (
    set "DSHPY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if exist "!DSHPY!" (
        "!DSHPY!" -c "import sys" >nul 2>nul
        if !errorlevel! equ 0 set "PYEXE=!DSHPY!"
    )
)

if not defined PYEXE (
    echo [ERROR] No usable Python interpreter found.
    echo         Install Python 3.10+ ^(python.org^) and try again, or edit this
    echo         file and set PYEXE to the full path of a python.exe.
    pause
    exit /b 1
)

echo Using Python: %PYEXE%
"%PYEXE%" "%~dp0main.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo The program exited with code %RC%.
    pause
)
exit /b %RC%
