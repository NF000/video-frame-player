@echo off
REM ============================================================
REM  Build script: Windows Desktop Video Frame Player
REM  Requires: conda env "video-player" + vendor\vlc\ runtime files
REM  Output:   dist\VideoFramePlayer\VideoFramePlayer.exe  (onedir)
REM ============================================================
setlocal
set "PROJ=%~dp0.."
set "PY=D:\software\miniconda3\envs\video-player\python.exe"

if not exist "%PY%" (
  echo [ERROR] Python not found: %PY%
  echo Edit the PY variable in this script if your env path differs.
  pause
  exit /b 1
)
if not exist "%PROJ%\vendor\vlc\libvlc.dll" (
  echo [ERROR] Missing vendor\vlc\libvlc.dll
  pause
  exit /b 1
)
if not exist "%PROJ%\vendor\vlc\plugins" (
  echo [ERROR] Missing vendor\vlc\plugins directory
  pause
  exit /b 1
)

echo Building with PyInstaller (onedir) ...
"%PY%" -m PyInstaller --noconfirm --clean --distpath "%PROJ%\dist" --workpath "%PROJ%\output" "%~dp0build_exe.spec"
if errorlevel 1 (
  echo [ERROR] Build failed. See the log above.
  pause
  exit /b 1
)
echo.
echo Done: dist\VideoFramePlayer\VideoFramePlayer.exe
echo NOTE: distribute the WHOLE folder, including _internal and plugins.
echo       A single-file exe will break VLC plugin discovery.
pause
endlocal
