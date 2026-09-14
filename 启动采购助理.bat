@echo off
cd /d "%~dp0"
set "PORT=8501"
set "URL=http://localhost:%PORT%"
set "PROBE=%TEMP%\ai_model_launch_probe.txt"

rem ===== ??????FREE / NOTOURS / REUSE / RESTART|pid????? PowerShell?=====
powershell -NoProfile -Command "$p=%PORT%; $busy=$false; try{$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $c.Close(); $busy=$true}catch{}; if(-not $busy){'FREE'; exit}; $ok=$false; try{$h=(Invoke-WebRequest -UseBasicParsing ('http://localhost:'+$p+'/_stcore/health') -TimeoutSec 2).Content; if(($h -as [string]).Trim() -eq 'ok'){$ok=$true}}catch{}; if(-not $ok){'NOTOURS'; exit}; $sp=(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess); $st=(Get-Process -Id $sp -ErrorAction SilentlyContinue).StartTime; $n=(Get-ChildItem -Path (Get-Location).Path -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in '.py','.bat' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime; if($st -and $n -and ($n -gt $st)){'RESTART ' + $sp}else{'REUSE'}" > "%PROBE%" 2>nul
set /p STATUS=<"%PROBE%"

if /i "%STATUS%"=="NOTOURS" goto :busy
if /i "%STATUS%"=="FREE" goto :start
if /i "%STATUS%"=="REUSE" goto :reuse
if /i "%STATUS:~0,7%"=="RESTART" goto :restart
goto :start

:reuse
echo.
echo [Already running] Opening the page in your browser ...
echo   To restart manually: close the running console window first, then run this script again.
start "" "%URL%"
exit /b 0

:restart
for /f "tokens=2" %%P in ("%STATUS%") do set "SPID=%%P"
echo.
echo [Update detected] Code changed since the server started. Restarting with the new code ...
powershell -NoProfile -Command "Stop-Process -Id %SPID% -Force -ErrorAction SilentlyContinue" >nul 2>&1
powershell -NoProfile -Command "for($i=0;$i -lt 10;$i++){ if(-not (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue)){break}; Start-Sleep -Milliseconds 250 }" >nul 2>&1
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
