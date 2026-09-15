@echo off
cd /d "%~dp0"
set "PORT=8501"
set "URL=http://localhost:%PORT%"
set "PROBE=%TEMP%\ai_model_launch_probe.txt"
set "MARK=logs\launch_marker.txt"
if not exist "logs" md "logs"
echo launch marker > "%MARK%"

rem ===== fast: is anything LISTENING on the port? (netstat ~0.05s) =====
rem (a TCP probe against a dead port costs ~2.3s on this machine: firewall drops SYN and retries)
netstat -ano | findstr /i "LISTENING" | findstr /c:":%PORT% " >nul 2>nul
if errorlevel 1 goto :start

rem ===== port busy: ours (REUSE / RESTART) or another program (NOTOURS)? =====
powershell -NoProfile -Command "$p=%PORT%; $ok=$false; try{$h=(Invoke-WebRequest -UseBasicParsing ('http://localhost:'+$p+'/_stcore/health') -TimeoutSec 2).Content; if(($h -as [string]).Trim() -eq 'ok'){$ok=$true}}catch{}; if(-not $ok){'NOTOURS'; exit}; $sp=(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess); $st=(Get-Process -Id $sp -ErrorAction SilentlyContinue).StartTime; $n=(Get-ChildItem -Path (Get-Location).Path -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in '.py','.bat' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime; if($st -and $n -and ($n -gt $st)){'RESTART ' + $sp}else{'REUSE'}" > "%PROBE%" 2>nul
set /p STATUS=<"%PROBE%"

if /i "%STATUS%"=="NOTOURS" goto :busy
if /i "%STATUS%"=="REUSE" goto :reuse
if /i "%STATUS:~0,7%"=="RESTART" goto :restart
goto :busy

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
powershell -NoProfile -Command "for($i=0;$i -lt 20;$i++){ if(-not (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue)){break}; Start-Sleep -Milliseconds 250 }" >nul 2>&1
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
echo [Waiting until the server is ready, then opening the browser ...]
start "" powershell -NoProfile -WindowStyle Hidden -Command "$p=%PORT%; $dir=Join-Path (Get-Location).Path 'logs'; $mk=Join-Path $dir 'launch_marker.txt'; $ok=$false; for($i=0;$i -lt 120;$i++){ $ports=([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()).GetActiveTcpListeners() | ForEach-Object { $_.Port }; if($ports -contains $p){ $ok=$true; break }; Start-Sleep -Milliseconds 150 }; $tag='ready'; if(-not $ok){ $tag='timeout' }; $el='?'; if(Test-Path $mk){ $el=[math]::Round(((Get-Date) - (Get-Item $mk).LastWriteTime).TotalSeconds,1) }; $line='[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] browser opened (' + $tag + ') ' + $el + 's after launcher start'; Add-Content -Path (Join-Path $dir 'startup.log') -Value $line -Encoding UTF8; Start-Process '%URL%'"
py -m streamlit run app.py --server.port %PORT% --server.headless true --browser.gatherUsageStats false
echo.
echo [Stopped] If there is an error above, screenshot it and send to AI. Log: logs\app.log
pause