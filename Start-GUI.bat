@echo off
setlocal
set "STT_PYTHON_ARGS="
pushd "%~dp0"
if errorlevel 1 goto root_error

rem STT_PYTHON is an optional full path to the preferred python.exe.
if defined STT_PYTHON goto launch
if exist "%~dp0.venv\Scripts\python.exe" (
    set "STT_PYTHON=%~dp0.venv\Scripts\python.exe"
    goto launch
)
if exist "%~dp0venv\Scripts\python.exe" (
    set "STT_PYTHON=%~dp0venv\Scripts\python.exe"
    goto launch
)
if exist "%~dp0backend\.venv\Scripts\python.exe" (
    set "STT_PYTHON=%~dp0backend\.venv\Scripts\python.exe"
    goto launch
)
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
    set "STT_PYTHON=%VIRTUAL_ENV%\Scripts\python.exe"
    goto launch
)
where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "STT_PYTHON=py.exe"
        set "STT_PYTHON_ARGS=-3"
        goto launch
    )
)
where python.exe >nul 2>nul
if not errorlevel 1 (
    set "STT_PYTHON=python.exe"
    goto launch
)
rem Per-user installations may exist without PATH or a working py registry.
if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if exist "%%~fD\python.exe" (
        "%%~fD\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
        if not errorlevel 1 (
            set "STT_PYTHON=%%~fD\python.exe"
            goto launch
        )
    )
)
echo Python was not found. Install Python 3.10 or newer, then install the project dependencies.
echo See docs\GUI_STARTUP.md for setup instructions.
set "STT_EXIT_CODE=1"
goto finish

:launch
"%STT_PYTHON%" %STT_PYTHON_ARGS% "%~dp0scripts\launch_gui.py" %*
set "STT_EXIT_CODE=%ERRORLEVEL%"
goto finish

:root_error
echo Could not open the project folder.
set "STT_EXIT_CODE=1"
goto report

:finish
popd
:report
if "%STT_EXIT_CODE%"=="0" goto done
echo.
echo GUI startup failed. Read the error above and docs\GUI_STARTUP.md.
if /I "%~1"=="--check" goto done
pause
:done
endlocal & exit /b %STT_EXIT_CODE%
