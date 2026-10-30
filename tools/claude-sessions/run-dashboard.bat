@echo off
rem Double-click to open the local Claude session dashboard. Close this window to stop it.
chcp 65001 >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0claude_sessions.py" %*
) else (
  python "%~dp0claude_sessions.py" %*
)
pause
