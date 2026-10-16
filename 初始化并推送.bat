@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Step 1/5 : commit this project to a local Git repository
echo             and point it at the GitHub remote.
echo             No network and no login needed for this step.
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 goto NOGIT

echo Using:
git --version
echo.

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo [1/5] git init ...
  git init -b main
) else (
  echo [1/5] already a repository, skipping init
)
echo.

echo [2/5] commit identity ^(this repo only^)
git config user.name "DragonSauce"
git config user.email "DragonSauce@users.noreply.github.com"
echo       user.name  = DragonSauce
echo       user.email = DragonSauce@users.noreply.github.com
echo.
echo       Want your real address instead? Run this first:
echo         git config user.email "you@example.com"
echo.

echo [3/5] git add -A ...
git add -A
echo.

echo [4/5] Files that WILL be committed:
echo ------------------------------------------------------------
git status --short
echo ------------------------------------------------------------
echo   Expected: about 23 files.
echo   These must NOT appear:
echo     releases\VideoSplitterStudio.zip   ^(145 MB - goes to Releases^)
echo     signing\vss-selfsigned.pfx         ^(private key^)
echo     dist\  build\  vendor\  bin\  testmedia\
echo ------------------------------------------------------------
echo.
choice /c YN /m "List above looks right - commit now"
if errorlevel 2 goto ABORT
echo.

echo [5/5] commit + set remote
git commit -m "Initial commit: Video Splitter Studio (PySide6 + FFmpeg)"
git remote remove origin >nul 2>nul
git remote add origin https://github.com/DragonSauce/video-splitter-studio.git

echo.
echo ============================================================
echo  Local part done. One command is left - run it in THIS window:
echo.
echo      git push -u origin main
echo.
echo  It will ask you to sign in to GitHub:
echo    - Git Credential Manager: authorise in the browser ^(easiest^)
echo    - or username + Personal Access Token ^(NOT your password^)
echo      create one at https://github.com/settings/tokens
echo      scope needed: Contents = Read and write
echo.
echo  PUSH HANGS OR FAILS TO CONNECT? Your proxy is at 127.0.0.1:7897.
echo  Run these, then push again:
echo      git config --global http.proxy  http://127.0.0.1:7897
echo      git config --global https.proxy http://127.0.0.1:7897
echo  And to undo after a successful push:
echo      git config --global --unset http.proxy
echo      git config --global --unset https.proxy
echo ============================================================
echo.
echo  Note: this .bat file itself gets committed. If you do not want it
echo  in the public repo, run this before pushing:
echo      git rm --cached "初始化并推送.bat"
echo.
pause
exit /b 0

:NOGIT
echo [ERROR] git was not found on PATH.
echo.
echo   Install Git for Windows:  https://git-scm.com/download/win
echo   or with winget:           winget install --id Git.Git -e
echo.
echo   Then CLOSE this window and double-click this file again
echo   ^(a freshly installed PATH is only visible to new terminals^).
echo.
pause
exit /b 1

:ABORT
echo.
echo Aborted - nothing was committed.
echo.
pause
exit /b 1
