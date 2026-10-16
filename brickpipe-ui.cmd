@echo off
rem Starts the brickpipe web page and opens it in your browser.
rem Double-click this file, or run it from a terminal in the brickpipe folder.
cd /d "%~dp0"
rem Experimental features, comma-separated (see README): e.g.  set BRICKPIPE_FEATURES=prices
rem set BRICKPIPE_FEATURES=
where py >nul 2>nul && (py -3 -m brickpipe.web %*) || (python -m brickpipe.web %*)
pause
