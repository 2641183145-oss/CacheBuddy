@echo off
rem devb [devin args...] -- run Devin CLI through Cache Buddy's proxy.
rem Starts the buddy panel if it isn't up; if it can't, runs plain devin.
setlocal
set "DEVB_URL="
for /f "usebackq delims=" %%s in (`python "%~dp0devin_cli.py" --ensure`) do set "DEVB_URL=%%s"
set "DEVIN_EXE=devin"
where devin >nul 2>nul || set "DEVIN_EXE=%LOCALAPPDATA%\devin\cli\bin\devin.exe"
if defined DEVB_URL set "WINDSURF_API_SERVER_URL=%DEVB_URL%"
"%DEVIN_EXE%" %*
