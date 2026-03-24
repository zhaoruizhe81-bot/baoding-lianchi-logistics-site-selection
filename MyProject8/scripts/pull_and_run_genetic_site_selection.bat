@echo off
setlocal

set "REPO_DIR=D:\project\baoding-lianchi-logistics-site-selection"
set "PROPY=C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat"

if not exist "%REPO_DIR%" (
    echo [ERROR] Repo not found: %REPO_DIR%
    exit /b 1
)

if not exist "%PROPY%" (
    echo [ERROR] ArcGIS Pro Python runner not found: %PROPY%
    exit /b 1
)

cd /d "%REPO_DIR%"
echo [INFO] Pulling latest code...
git pull --ff-only || exit /b 1

echo [INFO] Running genetic site selection...
call "%PROPY%" MyProject8\scripts\genetic_site_selection.py --project-dir MyProject8 %*
exit /b %errorlevel%
