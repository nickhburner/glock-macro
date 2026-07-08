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
rem Built from the checked-in spec (same flags as the old CLI call, plus the
rem cv2 FFmpeg-DLL size trim -- see the comment inside the spec).
python -m PyInstaller --noconfirm "A2 Macro Controller.spec"
if errorlevel 1 goto fail

echo.
echo === Building the updater .exe ===
rem Stdlib-only (plus widgets.py), so no hidden imports or collect-all needed.
python -m PyInstaller --noconfirm --onefile --windowed --name "A2 Updater" ^
    updater.py
if errorlevel 1 goto fail

echo.
echo === Building the remote companion .exe ===
rem Stdlib-only like the updater. The remote\ sources (worker + web page) are
rem for the USER to deploy on their own accounts and are NOT copied into
rem dist; the per-machine remote_token.txt / remote_status.json /
rem remote_cmd.json files are created at runtime and must never ship either
rem (nothing below copies them).
python -m PyInstaller --noconfirm --onefile --windowed --name "A2 Remote" ^
    remote_agent.py
if errorlevel 1 goto fail

echo.
echo === Bundling image folders next to the .exe ===
rem Wipe the copied folders first: xcopy never deletes, so refs removed from
rem the repo would otherwise linger in dist/ and ship forever.
if exist "dist\skills" rmdir /S /Q "dist\skills"
if exist "dist\ref" rmdir /S /Q "dist\ref"
if exist "dist\lang" rmdir /S /Q "dist\lang"
xcopy /E /I /Y skills "dist\skills" >nul
xcopy /E /I /Y ref "dist\ref" >nul
xcopy /E /I /Y lang "dist\lang" >nul
rem Custom button captures are per-machine user data: ship the folder empty.
if exist "dist\ref\custom" rmdir /S /Q "dist\ref\custom"
mkdir "dist\ref\custom"
rem Localized ref captures (ref\fr, ref\de) are per-machine user data too (the
rem user captures them via the GUI language ref wizard): never ship the
rem captures, but ship the folders empty (like ref\custom) for consistency.
if exist "dist\ref\fr" rmdir /S /Q "dist\ref\fr"
if exist "dist\ref\de" rmdir /S /Q "dist\ref\de"
mkdir "dist\ref\fr"
mkdir "dist\ref\de"
rem Remote-control runtime files are per-machine secrets/state. They are never
rem copied here, but ALSO scrub any that running the app from dist\ left
rem behind, so they can never end up inside a release zip.
if exist "dist\remote_token.txt" del /Q "dist\remote_token.txt"
if exist "dist\remote_status.json" del /Q "dist\remote_status.json"
if exist "dist\remote_cmd.json" del /Q "dist\remote_cmd.json"
if exist "dist\remote_heartbeat.json" del /Q "dist\remote_heartbeat.json"
if exist "dist\remote_agent.log" del /Q "dist\remote_agent.log"
if exist "dist\remote_agent.log.1" del /Q "dist\remote_agent.log.1"
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
