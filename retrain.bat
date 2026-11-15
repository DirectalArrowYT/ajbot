@echo off
setlocal
cd /d "%~dp0"

REM Retrain the policy from every recording on disk and save it to models\.
REM
REM   retrain.bat                 cherry-picked bot runs + all human runs (quick:
REM                               fits the usual winning models, ~2 min once caches are warm)
REM   retrain.bat --cherrypick    the same, but compares every candidate model on a
REM                               held-out match first (slow; run now and then)
REM   retrain.bat --cherrypick --no-cache   rebuild every feature cache from scratch
REM   retrain.bat --cherrypick all      filter the human runs too
REM   retrain.bat --cherrypick off      old behaviour, every tick trained on
REM   retrain.bat --mode frames         label every tick, not just changes
REM
REM Anything passed in is handed straight to train.py, so its flags all work
REM (--min-damage, --top-frac, --lead-s, --runs, --no-cache ...).
REM
REM The previous models\policy.pkl is moved to models\prev\ rather than
REM overwritten, so a retrain that turns out worse can be put back - and
REM play2.py --model-b models\prev\<file> runs the new policy against the old
REM one to see which actually wins.

set PYTHON=python
where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo Could not find python on PATH.
    pause
    exit /b 1
)

echo.
echo === digesting new recordings (cache\ticks\, parsed once, reused by every step) ===
%PYTHON% -m ajbot.tickcache

echo.
echo === archiving structurally broken recordings (archive\junk\) ===
%PYTHON% tools\archive_junk.py --apply
if errorlevel 1 (
    echo.
    echo tools\archive_junk.py failed - carrying on to training anyway.
)

echo.
echo === what the bot has learned so far ===
%PYTHON% mine.py --quiet --top 5
if errorlevel 1 (
    echo.
    echo mine.py failed - carrying on to the training step anyway.
)

echo.
echo === mining combos (models\combobook.json) ===
%PYTHON% combobook.py
if errorlevel 1 (
    echo.
    echo combobook.py failed - the bot will run without the combo book.
)

echo.
echo === mining move reach / commitment (models\moveinfo.json) ===
%PYTHON% -m ajbot.moveinfo
if errorlevel 1 (
    echo.
    echo moveinfo failed - training without move knowledge features.
)

echo.
echo === game frame data (models\framedata.json, from the FModel export) ===
%PYTHON% -m ajbot.framedata

echo.
echo === game move data (models\movedata.json, from the FModel export) ===
%PYTHON% -m ajbot.movedata
echo === game CPU combo routes (modelsi_routes.json, from the same export) ===
%PYTHON% -m ajbot.aicombos

echo.
echo === threat / escape / resource tables (models\threat.json) ===
%PYTHON% -m ajbot.threat
if errorlevel 1 (
    echo.
    echo threat mining failed - defence and escape fall back to fixed rules.
)

echo.
echo === retraining ===
if "%~1"=="" (
    %PYTHON% train.py --include-bot --cherrypick --quick --save models
) else (
    %PYTHON% train.py --include-bot --save models %*
)

if errorlevel 1 (
    echo.
    echo TRAINING FAILED - models\policy.pkl was left alone.
    pause
    exit /b 1
)

echo.
echo === results so far, per policy version (tools\benchmark.py) ===
%PYTHON% tools\benchmark.py --since 2026-09-21

echo.
echo Done. New policy in models\policy.pkl
echo Compare it against the previous one with:
echo    python play2.py --a Bakugo,Dabi,Nomu --b DekuFinal,Todoroki,AllMight --model-b models\prev\policy-DATE.pkl
echo    (pick the newest file in models\prev for DATE)
pause
