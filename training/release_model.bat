@echo off
setlocal
cd /d "%~dp0.."

REM Publish the model in models\ as a GitHub release, so everyone's GUI can
REM install it with Train > Get latest model.
REM
REM   training\release_model.bat                          asks before publishing
REM   training\release_model.bat --notes "beats gen 258"  a line for the release page
REM   training\release_model.bat --dry-run                only build the zip in dist\
REM
REM Uses the GitHub login git push already uses (or GH_TOKEN if set).

if not defined PYTHON set PYTHON=python
"%PYTHON%" training\release_model.py %*
set RC=%ERRORLEVEL%
if not defined AJBOT_NO_PAUSE pause
exit /b %RC%
