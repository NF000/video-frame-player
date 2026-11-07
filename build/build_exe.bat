@echo off
REM ============================================================
REM  Build script: Windows Desktop Video Frame Player
REM  Requires: conda env (PySide6 + python-vlc + PyInstaller) + VLC runtime
REM  Output:   dist\VideoFramePlayer\VideoFramePlayer.exe  (onedir)
REM
REM  Python interpreter is chosen by priority, no hard-coded local path:
REM    1) first argument:      build_exe.bat "D:\your\env\python.exe"
REM    2) env var PYTHON_EXE:  set PYTHON_EXE=D:\your\env\python.exe
REM    3) active conda env:    %CONDA_PREFIX%\python.exe
REM    4) python on PATH
REM
REM  VLC runtime dir: env var VLC_LIB_DIR wins, else project vendor\vlc
REM  (same lookup order as build_exe.spec and player\vlc_runtime.py)
REM
REM  All messages are ASCII on purpose: cmd uses the OEM code page by
REM  default, non-ASCII text in a UTF-8 .bat would render as garbage.
REM ============================================================
setlocal
set "PROJ=%~dp0.."

set "PY=%~1"
if not defined PY set "PY=%PYTHON_EXE%"
if not defined PY if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "PY=%CONDA_PREFIX%\python.exe"
if not defined PY set "PY=python"

set "VLCDIR=%PROJ%\vendor\vlc"
if defined VLC_LIB_DIR set "VLCDIR=%VLC_LIB_DIR%"

echo Python: %PY%
echo VLC   : %VLCDIR%

"%PY%" -c "import sys, PySide6, PyInstaller, importlib.util; assert importlib.util.find_spec('vlc'); print('  ' + sys.version.split()[0])" 2>nul
if errorlevel 1 (
  echo [ERROR] Python not usable or missing PySide6 / python-vlc / PyInstaller: %PY%
  echo Activate the env and run: pip install -r requirements.txt
  echo Or pass an interpreter:  build_exe.bat "D:\your\env\python.exe"
  pause
  exit /b 1
)
if not exist "%VLCDIR%\libvlc.dll" (
  echo [ERROR] Missing %VLCDIR%\libvlc.dll
  echo Put libvlc.dll / libvlccore.dll / plugins from the 64-bit VLC zip there,
  echo or set VLC_LIB_DIR to a directory that contains them.
  pause
  exit /b 1
)
if not exist "%VLCDIR%\libvlccore.dll" (
  echo [ERROR] Missing %VLCDIR%\libvlccore.dll
  pause
  exit /b 1
)
if not exist "%VLCDIR%\plugins\" (
  echo [ERROR] Missing plugins directory: %VLCDIR%\plugins
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
