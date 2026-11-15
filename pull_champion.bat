@echo off
rem Push finished recordings to the box, then download the current league
rem champion into models\.
rem   pull_champion.bat           sync, then install the champion if it is new
rem   pull_champion.bat --check   sync, then only say what the box has
rem   pull_champion.bat --force   sync, then reinstall even if up to date
cd /d "%~dp0"
echo === 1/2  pushing recordings to the box ===
python -u remote_retrain.py --sync
if errorlevel 1 echo !! the sync failed - recordings stay here and go next time
echo.
echo === 2/2  current champion ===
python -u pull_champion.py %*
echo.
pause
