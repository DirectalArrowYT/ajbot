@echo off
setlocal
cd /d "%~dp0"

REM Retrain the policy ON THE SERVER (AllMight), so this PC stays free for the
REM game. Digests new recordings here, copies code + digests over, runs the
REM combo/move/threat mining and train.py there, then pulls the new
REM models\policy.pkl (and the mined tables) back. The previous policy is kept
REM in models\prev\.
REM
REM   remote_retrain.bat                      the usual retrain (--cherrypick --quick)
REM   remote_retrain.bat --args "--cherrypick"   slower, compares candidate models
REM   remote_retrain.bat --sync               only copy recordings/code over
REM   remote_retrain.bat --pull               only fetch the server's latest policy
REM
REM Training refuses to start 03:30-05:00 (the server's nightly test run).

set PYTHON=python
where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo Could not find python on PATH.
    pause
    exit /b 1
)

%PYTHON% remote_retrain.py %*
if errorlevel 1 (
    echo.
    echo REMOTE RETRAIN FAILED - models\policy.pkl was left alone.
    pause
    exit /b 1
)

echo.
echo Done. New policy in models\policy.pkl - restart the bot to use it.
pause
