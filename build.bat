@echo off
setlocal

echo.
echo === Installing dependencies ===
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
python -m pip install pyinstaller
if errorlevel 1 goto fail

echo.
echo === Building the .exe ===
python -m PyInstaller --noconfirm --onefile --windowed --name "A2 Macro Controller" gui.py
if errorlevel 1 goto fail

echo.
echo === Bundling image folders next to the .exe ===
xcopy /E /I /Y skills "dist\skills" >nul
xcopy /E /I /Y ref "dist\ref" >nul
if exist README.md copy /Y README.md "dist\README.md" >nul

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
