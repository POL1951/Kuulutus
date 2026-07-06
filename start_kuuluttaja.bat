@echo off
cd /d C:\juoksu

rem =====================================================
rem  MUUTA TAMA PER KISA - kansio jossa KILP.DAT ja
rem  KilpSrj.xml sijaitsevat
rem =====================================================
set KISA=C:\kisa\data\Mikkeli.1
rem =====================================================

rem Start HkMaali.exe elevated (UAC prompt) as a separate non-blocking process
powershell -Command "Start-Process -FilePath 'C:\juoksu\HkMaali.exe' -ArgumentList 'cfg=C:\juoksu\KU.cfg' -WorkingDirectory '%KISA%' -Verb RunAs"

rem Give HkMaali.exe time to initialize before the UDP listener starts
timeout /t 2 >nul

rem Open the announcer page in the default browser after a short delay
start "" /b cmd /c "timeout /t 3 >nul & start http://localhost:8081/"

python announcer_display.py --sarjat-xml "%KISA%\KilpSrj.xml" --kilp-dat "%KISA%\KILP.DAT" --lahestyminen 2

echo.
echo ====================================================
echo  Ohjelma paattyi. Ikkuna jaa auki - paina nappainta.
echo ====================================================
pause