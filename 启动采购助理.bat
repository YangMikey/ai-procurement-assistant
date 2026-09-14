@echo off
cd /d "%~dp0"
set "PORT=8501"
set "URL=http://localhost:%PORT%"
set "PROBE=%TEMP%\ai_model_launch_probe.txt"

rem ===== 1) probe port: FREE / OURS / BUSY =====
powershell -NoProfile -Command "$b=$false; try{$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',%PORT%); $c.Close(); $b=$true} catch {}; if(-not $b){'FREE'} else {try{$h=(Invoke-WebRequest -UseBasicParsing 'http://localhost:%PORT%/_stcore/health' -TimeoutSec 3).Content; if(($h -as [string]).Trim() -eq 'ok'){'OURS'} else {'BUSY'}} catch {'BUSY'}}" > "%PROBE%" 2>nul
set /p STATUS=<"%PROBE%"
if /i "%STATUS%"=="BUSY" goto :busy
if /i "%STATUS%"=="OURS" goto :ours
goto :start

:ours
rem ===== 2) is the listener really our streamlit? get its pid =====
powershell -NoProfile -Command "$sp=(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess); if($sp){$cl=(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $sp)).CommandLine; if($cl -match 'streamlit'){$sp}else{'NOTOURS'}}else{'NOTOURS'}" > "%PROBE%" 2>nul
set /p SPID=<"%PROBE%"
if /i "%SPID%"=="NOTOURS" goto :busy

rem ===== 3) code changed after server start? =====
powershell -NoProfile -Command "$st=(Get-Process -Id %SPID% -ErrorAction SilentlyContinue).StartTime; $r=(Get-Location).Path; $n=(Get-ChildItem -Path $r -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in '.py','.bat' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime; if($st -and $n -and ($n -gt $st)){'STALE'}else{'FRESH'}" > "%PROBE%" 2>nul
set /p STALE=<"%PROBE%"
if /i "%STALE%"=="STALE" goto :restart

echo.
echo [Already running] Opening the page in your browser ...
echo   To restart manually: close the running console window first, then run this script again.
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
exit /b 0

:restart
echo.
echo [Update detected] Code changed since the server started. Restarting with the new code ...
powershell -NoProfile -Command "Stop-Process -Id %SPID% -Force -ErrorAction SilentlyContinue" >nul 2>&1
powershell -NoProfile -Command "for($i=0;$i -lt 20;$i++){ if(-not (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue)){break}; Start-Sleep -Milliseconds 300 }" >nul 2>&1
goto :start

:busy
echo.
echo [Port busy] Port %PORT% is used by another program. Close it and try again.
echo.
pause
exit /b 1

:start
echo ============================================================
echo  AI Procurement Assistant - starting ...
echo  *** KEEP THIS WINDOW OPEN ***  closing it stops the server
echo  URL: %URL%
echo ============================================================
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process '%URL%'"
py -m streamlit run app.py --server.port %PORT% --server.headless true --browser.gatherUsageStats false
echo.
echo [Stopped] If there is an error above, screenshot it and send to AI. Log: logs\app.log
pause
