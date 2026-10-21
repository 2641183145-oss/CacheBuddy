@echo off
rem ccb [claude args...] -- run Claude Code through Cache Buddy's proxy.
rem Starts the buddy panel if it isn't up; if it can't, runs plain claude.
setlocal
set "CCB_SETTINGS="
for /f "usebackq delims=" %%s in (`python "%~dp0cc_buddy.py" --ensure`) do set "CCB_SETTINGS=%%s"
if defined CCB_SETTINGS (
  claude --settings "%CCB_SETTINGS%" %*
) else (
  claude %*
)
