@echo off
setlocal

echo.
echo === Installing dependencies ===
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
python -m pip install pyinstaller
if errorlevel 1 goto fail

echo.
echo === Writing version.txt ===
rem Take the version from the latest git tag; if git/tags are unavailable,
rem keep whatever version.txt already says (bump it by hand before a release).
set "VERSION="
for /f "delims=" %%v in ('git describe --tags --abbrev^=0 2^>nul') do set "VERSION=%%v"
if defined VERSION (
    >version.txt echo %VERSION%
) else (
    if not exist version.txt >version.txt echo 1.0.0
    for /f "usebackq delims=" %%v in ("version.txt") do set "VERSION=%%v"
)
echo Version: %VERSION%

echo.
echo === Building the .exe ===
python -m PyInstaller --noconfirm --onefile --windowed --name "A2 Macro Controller" ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    --collect-all av ^
    gui.py
if errorlevel 1 goto fail

echo.
echo === Building the updater .exe ===
rem Stdlib-only (plus widgets.py), so no hidden imports or collect-all needed.
python -m PyInstaller --noconfirm --onefile --windowed --name "A2 Updater" ^
    updater.py
if errorlevel 1 goto fail

echo.
echo === Bundling image folders next to the .exe ===
xcopy /E /I /Y skills "dist\skills" >nul
xcopy /E /I /Y ref "dist\ref" >nul
copy /Y version.txt "dist\version.txt" >nul
if exist README.md copy /Y README.md "dist\README.md" >nul
if exist settings.example.json copy /Y settings.example.json "dist\settings.json" >nul

echo.
echo === Build complete ===
goto done

:fail
echo.
echo *** BUILD FAILED - see the messages above. ***
echo If it failed on a missing module, re-run PyInstaller with an extra
echo   --hidden-import ^<module^>   or   --collect-all ^<package^>   flag.
exit /b 1

:done
endlocal
