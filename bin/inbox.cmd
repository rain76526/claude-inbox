: << 'CMDBLOCK'
@echo off
REM Cross-platform entry (cmd + sh) for hooks and PowerShell/cmd. Keep this block ASCII-only:
REM cmd.exe parses batch files in the OEM code page (cp950), not UTF-8.
REM Windows: try python, then py -3. Unix: sh skips this heredoc and runs the tail.
setlocal
set "SCRIPT=%~dp0..\inbox\inbox.py"
set PYTHONIOENCODING=utf-8
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    python "%SCRIPT%" %*
    exit /b %ERRORLEVEL%
)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py -3 "%SCRIPT%" %*
    exit /b %ERRORLEVEL%
)
echo inbox: Python 3.9+ not found 1>&2
exit /b 1
CMDBLOCK

exec sh "$(dirname "$0")/inbox" "$@"
