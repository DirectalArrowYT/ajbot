@echo off
setlocal
cd /d "%~dp0.."

REM Publish the game-side files (ASI loader, UE4SS, the bot's mods, ajhook.asi)
REM as a GitHub release, so Setup > Install game files / Update now get them.
REM
REM   tools\release_gamefiles.bat              build, then ask before publishing
REM   tools\release_gamefiles.bat --no-build   reuse the last cargo builds
REM   tools\release_gamefiles.bat --dry-run    only build the zip in dist\
REM
REM Uses the GitHub login git push already uses (or GH_TOKEN if set).
REM Close the game first if it is running.

if not defined PYTHON set PYTHON=python
"%PYTHON%" tools\release_gamefiles.py %*
set RC=%ERRORLEVEL%
if not defined AJBOT_NO_PAUSE pause
exit /b %RC%
